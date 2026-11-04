"""
The assay vocabulary shared by the experiment plan (step 5) and the
wet-lab ingest (step 6).

One table says, for every measurement this workflow knows: its canonical
unit and the units it converts from, the physically possible range, which
direction is better, the control that makes a value interpretable, and the
protocol the plan hands to the lab. Step 5 writes plans from it and step 6
checks results against it, so a plan can never ask for a measurement the
ingest cannot read.

The protocols name the method and what must be reported with it. They are
not lab SOPs; the lab's own protocol ID goes in the results file.
"""

from __future__ import annotations

import math

import predictor

ASSAYS: dict[str, dict] = {
    "absorption_max": {
        "label": "Absorption maximum",
        "unit": "nm",
        "units": {"nm": 1.0},
        "range": (200.0, 2000.0),
        "better": None,
        "control": "solvent or vehicle blank",
        "protocol": "UV-vis-NIR absorption in the state of use (solvent, aggregate or nanoparticle dispersion); report lambda_max.",
    },
    "emission_max": {
        "label": "Emission maximum",
        "unit": "nm",
        "units": {"nm": 1.0},
        "range": (200.0, 2000.0),
        "better": "band",
        "control": "solvent or vehicle blank",
        "protocol": "Steady-state emission under the same conditions as absorption; report lambda_max and the excitation wavelength.",
    },
    "plqy": {
        "label": "Photoluminescence quantum yield",
        "unit": "fraction",
        "units": {"fraction": 1.0, "%": 0.01, "percent": 0.01},
        "range": (0.0, 1.0),
        "better": "higher",
        "control": "reference dye with a stated literature quantum yield",
        "protocol": "Relative method against a reference dye, or absolute with an integrating sphere. For NIR-II, state which IR-26 value was used; the literature spans 0.05% to 0.5%.",
    },
    "singlet_oxygen_yield": {
        "label": "Singlet oxygen quantum yield",
        "unit": "fraction",
        "units": {"fraction": 1.0, "%": 0.01, "percent": 0.01},
        "range": (0.0, 1.0),
        "better": "higher",
        "control": "reference photosensitizer with a stated singlet oxygen yield",
        "protocol": "ABDA or DPBF bleaching relative to a reference photosensitizer; SOSG for confirmation. Report light dose and oxygenation.",
    },
    "ros_type_i_signal": {
        "label": "Type I ROS signal",
        "unit": "fold",
        "units": {"fold": 1.0, "x": 1.0},
        "range": (0.0, 1.0e4),
        "better": "higher",
        "control": "dark control without irradiation",
        "protocol": "DHR123 (superoxide) or HPF (hydroxyl radical) fluorescence, fold change over the dark control; confirm the route with ESR spin trapping (DMPO or BMPO).",
    },
    "photothermal_efficiency": {
        "label": "Photothermal conversion efficiency",
        "unit": "fraction",
        "units": {"fraction": 1.0, "%": 0.01, "percent": 0.01},
        "range": (0.0, 1.0),
        "better": "higher",
        "control": "solvent or vehicle under the same irradiation",
        "protocol": "Heating and cooling curves under a stated laser wavelength and power density; eta from the time constant of cooling.",
    },
    "dark_cytotoxicity_ic50": {
        "label": "Dark cytotoxicity IC50",
        "unit": "uM",
        "units": {"uM": 1.0, "µM": 1.0, "μM": 1.0, "nM": 0.001, "mM": 1000.0},
        "range": (0.0, 1.0e6),
        "better": None,
        "control": "untreated cells and a vehicle control",
        "protocol": "Viability assay without irradiation, same cell line and incubation as the phototoxicity assay.",
    },
}


def objectives(goal: dict) -> list[str]:
    """The measurements that decide whether a candidate meets this GoalSpec."""
    modalities = set(goal.get("modalities", []))
    chosen: list[str] = []
    if "FLI" in modalities:
        chosen.append("plqy")
        if goal.get("band") or goal.get("targets", {}).get("band"):
            chosen.append("emission_max")
    if "PDT" in modalities:
        chosen.append("ros_type_i_signal" if goal.get("ros_type") == "I" else "singlet_oxygen_yield")
    if {"PTT", "PAI"} & modalities:
        chosen.append("photothermal_efficiency")
    return chosen


def panel(goal: dict) -> list[str]:
    """Everything the plan asks the lab to measure: spectra first, then the objectives."""
    return list(dict.fromkeys(["absorption_max", *(["emission_max"] if "FLI" in goal.get("modalities", []) else []), *objectives(goal)]))


def convert(assay: str, value: float, unit: str) -> float | None:
    """Value in the canonical unit, or None when the unit is not one this assay accepts."""
    factor = ASSAYS[assay]["units"].get(unit.strip())
    return value * factor if factor is not None else None


def in_range(assay: str, value: float) -> bool:
    low, high = ASSAYS[assay]["range"]
    return low <= value <= high


def utility(assay: str, value: float, goal: dict) -> float | None:
    """
    The value on the scale the surrogate learns: higher is better.

    Fractions stay as they are. Fold changes are log-transformed because a
    tenfold signal is not ten times as useful as a twofold one. An emission
    maximum becomes its fit to the target window, the same falloff the
    predictor uses, because "redder" is not better past the window's edge.
    """
    rule = ASSAYS[assay]["better"]
    if rule == "higher":
        return math.log10(max(value, 1e-6)) if ASSAYS[assay]["unit"] == "fold" else value
    if rule == "band":
        return predictor.band_fit({"emission_nm": value}, goal)
    return None


# ---------------------------------------------------------------------------
# The lab dataset
# ---------------------------------------------------------------------------
#
# lab_dataset.json is the only place measured values live. Step 6 appends a
# version to it per ingested results file; steps 4 and 5 read it to fit the
# surrogate and to keep tested compounds out of the next batch.


def load_dataset(output_dir: str) -> dict:
    import os

    from _common import artifact_path, read_artifact

    if not os.path.isfile(artifact_path(output_dir, "dataset")):
        return {"version": 0, "versions": [], "measurements": []}
    return read_artifact(output_dir, "dataset")


def measured(dataset: dict) -> dict[str, dict]:
    """Every compound with at least one QC-accepted value, keyed by InChIKey."""
    out: dict[str, dict] = {}
    for entry in dataset.get("measurements", []):
        if not entry.get("used_for_model"):
            continue
        record = out.setdefault(entry["inchikey"], {"compound_id": entry["compound_id"], "smiles": entry["smiles"], "values": {}})
        record["values"].setdefault(entry["assay"], []).append(entry["mean"])
    return out


def training(dataset: dict, assay: str, goal: dict) -> tuple[list[str], list[float]]:
    """
    One utility per compound for an objective: the mean over every round that
    measured it, so a compound retested in a later round is one point, not two.
    """
    smiles: list[str] = []
    values: list[float] = []
    for record in measured(dataset).values():
        means = record["values"].get(assay)
        if not means:
            continue
        value = utility(assay, sum(means) / len(means), goal)
        if value is None:
            continue
        smiles.append(record["smiles"])
        values.append(value)
    return smiles, values
