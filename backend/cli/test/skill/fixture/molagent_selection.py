"""Exercise shortlist explanations using real RDKit checks and fingerprints offline."""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills/chemistry/molagent-workflow/scripts"))
import goalspec
import photophysics
import step2_database as step2
import step3_design as step3

goal = goalspec.parse("NIR-II fluorescence imaging agent")
pool = [
    {"id": "benzene", "smiles": "c1ccccc1"},
    {"id": "ethanol", "smiles": "CCO"},
    {"id": "amine", "smiles": "CCN"},
]
step2.evaluate(pool, goal)
assert all(not molecule["gate"]["pass"] for molecule in pool)
chosen = step2.diversify(copy.deepcopy(pool), 2)
assert len(chosen) == 2
assert all("Fallback" in molecule["selection_reason"] for molecule in chosen)
assert all("hard checks failed" in molecule["selection_reason"] for molecule in chosen)
assert "no earlier selected molecule" in chosen[0]["selection_details"][1]
assert "Tanimoto similarity" in chosen[1]["selection_details"][1]

donor = "c1ccc(N(c2ccccc2)c2ccccc2)cc1"
first = step3.join("c1ccc2nsnc2c1", donor, 2)
second = step3.join("c1cc2nsnc2c2nsnc12", donor, 2)
assert first and second
evaluated = step3.evaluate([
    {"id": "first", "smiles": first, "rationale": "First scaffold"},
    {"id": "second", "smiles": second, "rationale": "Second scaffold"},
], goal, {})
assert len(evaluated) == 2 and all(molecule["gate"]["pass"] for molecule in evaluated)
for molecule in evaluated:
    terms = molecule["ranking"]["components"]
    assert "target_channels" in terms and "unmet_preferences" in terms
    assert round(max(0, sum(terms.values())), 4) == photophysics.rank_score(molecule["photophysics"], goal)
    assert terms["unmet_preferences"] == -0.1 * molecule["gate"]["soft_failures"]
diverse = step2.diversify(copy.deepcopy(evaluated), 2)
assert all("Passed hard structural checks" in molecule["selection_reason"] for molecule in diverse)
assert diverse[0]["score"] == max(molecule["score"] for molecule in evaluated)

similar = step3.apply_edit(first, "brominate")[0]
near = step3.evaluate([{"smiles": first}, {"smiles": similar}, {"smiles": "CCO"}], goal, {})
for molecule, score in zip(near, (0.9, 0.8, 0.1)):
    molecule["score"] = score
thinned = step2.diversify(near, 2)
assert [molecule["smiles"] for molecule in thinned] == [first, "CCO"]
assert near[1]["thinned_as_similar"] > 0.6
assert "rank #3 of 3" in thinned[1]["selection_details"][0]

normal = copy.deepcopy(evaluated)
for molecule in normal:
    molecule["synthesizability"] = {"available": True, "sa_score": 3.0}
selected = step3.select(normal, 1, 6.0)
assert selected[0]["score"] == max(molecule["score"] for molecule in normal)
assert "Passed hard checks and the SA filter" in selected[0]["selection_reason"]
assert "3.00; requested maximum 6.00" in selected[0]["selection_details"][1]
assert "of 2" in selected[0]["selection_reason"]

high_sa = copy.deepcopy(normal)
for molecule in high_sa:
    molecule["synthesizability"]["sa_score"] = 8.0
assert "retained despite high SA" in step3.select(high_sa, 1, 6.0)[0]["selection_reason"]

missing_sa = copy.deepcopy(normal)
for molecule in missing_sa:
    molecule["synthesizability"] = {"available": False}
unknown = step3.select(missing_sa, 1, 6.0)[0]
assert "SA unavailable" in unknown["selection_reason"]
assert "not confirmed easy to synthesize" in unknown["selection_details"][1]

failed = copy.deepcopy(pool)
for molecule in failed:
    molecule["synthesizability"] = {"available": True, "sa_score": 2.0}
assert "no design passed hard checks" in step3.select(failed, 1, 6.0)[0]["selection_reason"]

aqueous = {"constraints": {"aqueous": True}}
soluble = photophysics.gate(photophysics.profile("O=S(=O)([O-])c1ccccc1"), aqueous)
handle = next(check for check in soluble["checks"] if check["rule"] == "aqueous_handle")
assert handle["pass"] and handle["detail"] == "ionic or PEG solubilising group detected"

assert json.loads(json.dumps(selected))[0]["selection_reason"] == selected[0]["selection_reason"]
print("Validated literature-independent selection reasons, diversity thinning, SA fallbacks and real gate details")
