#!/usr/bin/env python3
"""
Tests for the theranostic luminogen workflow.

Offline except where marked: no network, no GPU, no model weights. Run with

    uv run --python 3.12 --with rdkit --with pytest --no-project \
        python -m pytest skills/chemistry/molagent-workflow/tests -q

The band-calibration test is the one that matters most. The thresholds in
`photophysics.spectral_band` were fitted to those reference dyes, so this
test does not prove the proxy generalises — it proves a change to the
fragment vocabulary or the formula has not silently moved a known dye into
the wrong window.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS)

import goalspec  # noqa: E402
import photophysics  # noqa: E402
import scaffolds  # noqa: E402
import step1_literature as step1  # noqa: E402
import step3_design as step3  # noqa: E402
from rdkit import Chem  # noqa: E402


# ---------------------------------------------------------------------------
# Step 0 — GoalSpec
# ---------------------------------------------------------------------------


def test_chinese_question_resolves_every_field():
    spec = goalspec.parse("设计一个用于乏氧肿瘤光动力治疗的近红外二区AIE分子，要能同时做荧光成像和光热")
    assert set(spec["modalities"]) == {"FLI", "PDT", "PTT"}
    assert spec["band"] == "NIR-II"
    assert spec["constraints"]["hypoxia_tolerant"]
    assert spec["constraints"]["aggregate"]
    assert spec["unresolved"] == []


def test_hypoxia_plus_pdt_implies_type_one():
    """Type I exists because Type II fails in hypoxia; the parse should know that."""
    spec = goalspec.parse("a photodynamic agent for hypoxic tumors")
    assert spec["ros_type"] == "I"
    assert spec["targets"]["ros_type"] == "I"


def test_explicit_type_two_is_not_overridden_by_hypoxia():
    spec = goalspec.parse("singlet oxygen generator for hypoxic tumours")
    assert spec["ros_type"] == "II"


def test_missing_window_is_reported_not_guessed():
    spec = goalspec.parse("design a photosensitizer")
    assert spec["band"] is None
    assert any("optical window" in gap for gap in spec["unresolved"])


def test_queries_lead_with_the_most_distinctive_concept():
    spec = goalspec.parse("NIR-II AIE photothermal agent with fluorescence imaging")
    assert spec["keywords"]["queries"][0].startswith("NIR-II fluorophore")


def test_parse_is_deterministic():
    question = "设计一个近红外二区光热剂"
    first, second = goalspec.parse(question), goalspec.parse(question)
    assert first["modalities"] == second["modalities"]
    assert first["keywords"]["queries"] == second["keywords"]["queries"]


# ---------------------------------------------------------------------------
# Scaffold library
# ---------------------------------------------------------------------------


def test_every_scaffold_smiles_parses():
    assert scaffolds.validate() == []


def test_nir_ii_goal_puts_the_nir_ii_acceptor_first():
    goal = goalspec.parse("NIR-II fluorophore for photothermal therapy")
    acceptors = scaffolds.select(goal, roles=["acceptor"])
    assert acceptors, "no acceptors selected for a NIR-II goal"
    assert acceptors[0]["band"] == "NIR-II"


# ---------------------------------------------------------------------------
# Photophysics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", photophysics.calibration_report(), ids=lambda r: r["name"])
def test_reference_dye_lands_in_its_known_band(row):
    assert row["predicted"] == row["expected"], f"{row['name']}: score {row['score']}"


def test_stronger_acceptor_outranks_weaker_at_equal_substitution():
    """
    The discrimination the whole NIR-II design task rests on. An earlier
    version of the proxy scored TPA-BTD-TPA above TPA-BBTD-TPA because the
    extra backbone atoms outweighed the acceptor identity.
    """
    tpa = "c1ccc(N(c2ccccc2)c2ccccc2)cc1"
    scores = {}
    for name, core in (("BTD", "c1ccc2nsnc2c1"), ("BBTD", "c1cc2nsnc2c2nsnc12")):
        smiles = step3.join(core, tpa, 2)
        assert smiles, f"could not build TPA-{name}-TPA"
        scores[name] = photophysics.profile(smiles)["spectral_band"]["score"]
    assert scores["BBTD"] > scores["BTD"]


def test_cyanine_bridge_is_counted():
    cy5 = "CC1(C)c2ccccc2N(C)/C1=C/C=C/C=C1\\N(C)c2ccccc2C1(C)C"
    assert photophysics.profile(cy5)["donor_acceptor"]["polymethine_length"] >= 5


def test_benzene_has_no_polymethine_bridge():
    assert photophysics.profile("c1ccccc1")["donor_acceptor"]["polymethine_length"] == 0


def test_iodine_registers_an_isc_route_and_a_saturated_chain_does_not():
    iodinated = photophysics.profile("Ic1ccc2nsnc2c1")
    assert iodinated["intersystem_crossing"]["heavy_atom_weight"] > 0
    assert any("heavy-atom" in route for route in iodinated["intersystem_crossing"]["routes"])

    alkane = photophysics.profile("CCCCCCCC")
    assert alkane["intersystem_crossing"]["routes"] == ["no structural ISC route identified"]


def test_conjugation_path_separates_fused_from_linear():
    """Size alone cannot tell a compact fused system from an extended one."""
    perylene = photophysics.profile("c1cc2cccc3c2c2c1cccc2c1cccc3c1")["conjugation"]
    hexatriene = photophysics.profile("C=CC=CC=C")["conjugation"]
    assert perylene["size"] > hexatriene["size"]
    assert perylene["path"] <= hexatriene["path"] + 2


def test_bad_smiles_returns_an_error_not_an_exception():
    assert "error" in photophysics.profile("not-a-molecule")


def test_gate_rejects_a_molecule_with_no_pi_system():
    goal = goalspec.parse("NIR-II fluorescence imaging agent")
    result = photophysics.gate(photophysics.profile("CCCCCCCCO"), goal)
    assert not result["pass"]
    assert any(c["rule"] == "conjugated_system" and not c["pass"] for c in result["checks"])


def test_gate_failures_carry_a_reason():
    goal = goalspec.parse("NIR-II photodynamic agent")
    for check in photophysics.gate(photophysics.profile("CCO"), goal)["checks"]:
        assert check["detail"], f"{check['rule']} failed without a reason"


def test_channel_shares_are_normalised():
    shares = photophysics.profile("O=C1c2ccccc2C(=O)c2ccccc21")["channel_balance"]["share"]
    assert abs(sum(shares.values()) - 1.0) < 1e-6


def test_type_i_never_invents_the_energies_it_cannot_compute():
    profile = photophysics.profile("C[n+]1ccc(-c2ccc3nsnc3c2)cc1")
    assert profile["type_i"]["requires"], "the unmeasurable criteria must still be named"
    assert "T1 energy" in " ".join(profile["type_i"]["requires"])


# ---------------------------------------------------------------------------
# Step 1 helpers (offline)
# ---------------------------------------------------------------------------


def test_window_definitions_are_not_mistaken_for_measurements():
    """"NIR-II (1000-1700 nm)" is a definition, not somebody's emission peak."""
    paper = {
        "title": "t",
        "abstract": "Dyes emitting in the NIR-II window (1000-1700 nm) are promising.",
        "doi": "10.0/x",
    }
    assert [f for f in step1.extract(paper) if f["quantity"] == "emission_nm"] == []


def test_a_real_measurement_is_kept_and_marked_unverified():
    paper = {
        "title": "t",
        "abstract": "The probe shows a quantum yield of 14.8 % in water.",
        "doi": "10.0/x",
    }
    findings = step1.extract(paper)
    assert any(f["quantity"] == "plqy" and f["value"] == "14.8" for f in findings)
    assert all(f["verified"] is False for f in findings)


def test_deduplication_prefers_the_fuller_abstract():
    merged = step1.deduplicate(
        [
            {"title": "A Dye", "doi": "10.1/x", "abstract": "short", "source": "openalex"},
            {"title": "A Dye", "doi": "10.1/x", "abstract": "a much longer abstract", "source": "europepmc"},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["abstract"] == "a much longer abstract"
    assert set(merged[0]["found_by"]) == {"openalex", "europepmc"}


def test_deduplication_matches_on_title_when_doi_is_absent():
    merged = step1.deduplicate(
        [
            {"title": "A Dye!", "doi": None, "abstract": "x", "source": "arxiv"},
            {"title": "a dye", "doi": None, "abstract": "x", "source": "openalex"},
        ]
    )
    assert len(merged) == 1


# ---------------------------------------------------------------------------
# Step 3 — generation and edits
# ---------------------------------------------------------------------------


def test_join_produces_a_valid_molecule_with_both_fragments():
    product = step3.join("c1ccc2nsnc2c1", "c1ccsc1", 2)
    assert product
    mol = Chem.MolFromSmiles(product)
    assert mol is not None
    assert mol.HasSubstructMatch(Chem.MolFromSmiles("c1ccc2nsnc2c1"))
    assert len(mol.GetSubstructMatches(Chem.MolFromSmiles("c1ccsc1"))) >= 2


def test_join_returns_none_when_there_is_nowhere_to_attach():
    assert step3.join("CCCC", "c1ccsc1", 1) is None


@pytest.mark.parametrize("move", sorted(step3.EDITS))
def test_every_edit_either_declines_or_returns_valid_structures(move):
    """A move may not apply to a given parent, but it must never emit junk."""
    parent = "O=Cc1ccc2nsnc2c1-c1ccncc1"
    for product in step3.apply_edit(parent, move):
        assert Chem.MolFromSmiles(product) is not None, f"{move} produced {product}"


def test_iodination_actually_adds_iodine():
    products = step3.apply_edit("c1ccc2nsnc2c1", "iodinate")
    assert products
    assert all("I" in p for p in products)


def test_thionation_swaps_the_carbonyl():
    products = step3.apply_edit("O=Cc1ccccc1", "thionate")
    assert products
    assert any(Chem.MolFromSmiles(p).HasSubstructMatch(Chem.MolFromSmarts("[CX3]=[SX1]")) for p in products)


def test_type_one_goal_avoids_the_heavy_atom_moves():
    """Type I wants electron transfer, not a heavier atom."""
    goal = goalspec.parse("type I photosensitizer for hypoxic tumors")
    moves = step3.moves_for(goal)
    assert "cationise" in moves
    assert "iodinate" not in moves and "brominate" not in moves


def test_type_two_goal_takes_the_heavy_atom_route():
    goal = goalspec.parse("singlet oxygen photosensitizer")
    assert "iodinate" in step3.moves_for(goal)


def test_scaffold_assembly_yields_distinct_valid_designs():
    goal = goalspec.parse("NIR-II photothermal and photodynamic agent")
    designed = step3.generate_scaffold_guided(goal, limit=12)
    assert designed
    assert len({d["smiles"] for d in designed}) == len(designed)
    for entry in designed:
        assert Chem.MolFromSmiles(entry["smiles"]) is not None
        assert entry["rationale"] and entry["implied_chemistry"]


def test_reinvent_is_reported_missing_rather_than_faked():
    goal = goalspec.parse("NIR-II agent")
    if step3.reinvent_available():
        with pytest.raises(NotImplementedError):
            step3.generate_reinvent(goal, [], 5)
    else:
        assert step3.generate_reinvent(goal, [], 5) == []


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------


def test_output_directory_cannot_escape_the_working_directory():
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "goalspec.py"), "--question", "x", "--output-dir", "../escape"],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "escapes" in result.stderr


def test_goalspec_stage_writes_a_readable_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "goalspec.py"),
             "--question", "NIR-II photothermal agent", "--output-dir", "./out"],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        with open(os.path.join(tmp, "out", "goal_spec.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["artifact"] == "goal"
        assert payload["schema_version"] == 1
        assert payload["band"] == "NIR-II"


def test_downstream_stage_refuses_to_run_without_its_upstream_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "step2_database.py"), "--output-dir", "./out"],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "goal_spec.json" in result.stderr


def test_pipeline_modes_cover_the_documented_chain():
    import pipeline

    assert pipeline.MODES["full"] == ["goal", "step1", "step2", "step3"]
    for stages in pipeline.MODES.values():
        assert stages[0] == "goal", "every mode must establish a GoalSpec first"


# ---------------------------------------------------------------------------
# Optional ML predictor
# ---------------------------------------------------------------------------


def test_predictor_is_absent_rather_than_fabricated():
    """No trained model must mean no prediction, never a default number."""
    import predictor

    if not predictor.available():
        assert predictor.predict("c1ccccc1") is None
        assert "no trained model" in predictor.describe()


def test_blend_leaves_the_proxy_alone_when_the_model_has_no_opinion():
    import predictor

    goal = goalspec.parse("NIR-II fluorophore")
    result = predictor.blend(0.42, None, goal)
    assert result["score"] == 0.42
    assert result["ml_used"] is False


def test_band_fit_rewards_a_prediction_inside_the_target_window():
    import predictor

    goal = goalspec.parse("NIR-II fluorophore")
    assert predictor.band_fit({"emission_nm": 1100}, goal) == 1.0
    near = predictor.band_fit({"emission_nm": 900}, goal)
    far = predictor.band_fit({"emission_nm": 500}, goal)
    assert 0 < near < 1.0
    assert far < near


def test_band_fit_has_no_opinion_without_a_target_band():
    import predictor

    goal = goalspec.parse("design a photosensitizer")
    assert goal["band"] is None
    assert predictor.band_fit({"emission_nm": 700}, goal) is None


def test_blend_keeps_the_proxy_in_the_majority():
    import predictor

    goal = goalspec.parse("NIR-II fluorophore")
    perfect = predictor.blend(0.0, {"emission_nm": 1100, "in_domain": True}, goal)
    # A model that loves a structurally hopeless candidate cannot carry it.
    assert perfect["score"] <= 0.31
    assert perfect["ml_used"] is True
    assert perfect["proxy_score"] == 0.0
