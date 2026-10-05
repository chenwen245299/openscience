"""
Optional ML predictor for absorption, emission and quantum yield.

The rule layer in `photophysics.py` decides what is admissible; this decides
the order among what survives. It is deliberately optional: if no model has
been trained, `available()` is False and the pipeline ranks on the structural
proxies alone rather than on a model that does not exist.

Design follows the fluorophore-generation literature: Morgan fingerprints
concatenated with four Catalan solvent parameters, a regressor each for
absorption and emission, and a classifier for PLQY above 0.5. That split is
what SyntheFluor-RL used, and it is chosen here for the same reason -- the
public fluorescence datasets are too small and too skewed to support
regressing quantum yield directly.

Train first with `train_model.py`; see its header for where the data comes
from. Nothing is downloaded at import time.

What this cannot do: the datasets behind it (ChemFluor, Deep4Chem) are
dilute-solution measurements spanning the UV to NIR-I. A prediction for a
NIR-II emitter, or for anything in an aggregate, is an extrapolation outside
the training distribution. `predict` says so in `in_domain` rather than
quietly returning a number.
"""

from __future__ import annotations

import json
import os
import pickle

MODEL_DIR = os.environ.get(
    "MOLAGENT_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models"),
)
TARGETS = ("absorption", "emission", "plqy")

# Catalan solvent parameters: polarizability, dipolarity, acidity, basicity.
SOLVENTS: dict[str, tuple[float, float, float, float]] = {
    "water": (0.681, 0.997, 1.062, 0.025),
    "methanol": (0.608, 0.904, 0.605, 0.545),
    "ethanol": (0.633, 0.783, 0.400, 0.658),
    "acetonitrile": (0.645, 0.974, 0.044, 0.286),
    "dmso": (0.830, 1.000, 0.072, 0.647),
    "dmf": (0.759, 0.977, 0.031, 0.613),
    "dichloromethane": (0.761, 0.769, 0.040, 0.178),
    "chloroform": (0.783, 0.614, 0.047, 0.071),
    "toluene": (0.843, 0.284, 0.000, 0.128),
    "thf": (0.714, 0.634, 0.000, 0.591),
    "hexane": (0.616, 0.000, 0.000, 0.056),
}
DEFAULT_SOLVENT = "water"

_cache: dict | None = None


def _paths() -> dict[str, str]:
    return {name: os.path.join(MODEL_DIR, f"{name}.pkl") for name in TARGETS}


def available() -> bool:
    return all(os.path.isfile(path) for path in _paths().values())


def featurise(smiles: str, solvent: str = DEFAULT_SOLVENT):
    """Morgan fingerprint plus solvent parameters. Returns None on a bad parse."""
    from rdkit import Chem
    from rdkit.Chem import rdFingerprintGenerator
    import numpy as np

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    bits = np.zeros((2048,), dtype=np.int8)
    for bit in generator.GetFingerprint(mol).GetOnBits():
        bits[bit] = 1
    parameters = SOLVENTS.get(solvent.lower(), SOLVENTS[DEFAULT_SOLVENT])
    return np.concatenate([bits, np.array(parameters, dtype=np.float64)])


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    models = {}
    for name, path in _paths().items():
        with open(path, "rb") as handle:
            models[name] = pickle.load(handle)
    _cache = models
    return models


def predict(smiles: str, solvent: str = DEFAULT_SOLVENT) -> dict | None:
    """
    Predicted absorption and emission in nm and the probability that PLQY
    exceeds 0.5, or None when no model is installed.

    `in_domain` is False when the prediction lands outside the range the
    training data covered; the number is still returned, flagged, because a
    silent extrapolation is worse than a visible one.
    """
    if not available():
        return None
    features = featurise(smiles, solvent)
    if features is None:
        return None

    models = _load()
    row = features.reshape(1, -1)
    absorption = float(models["absorption"].predict(row)[0])
    emission = float(models["emission"].predict(row)[0])
    plqy = float(models["plqy"].predict_proba(row)[0][1])

    meta = _metadata()
    covered = meta.get("emission_range", [296, 1045])
    return {
        "absorption_nm": round(absorption, 1),
        "emission_nm": round(emission, 1),
        "plqy_above_0.5": round(plqy, 3),
        "solvent": solvent,
        "in_domain": covered[0] <= emission <= covered[1],
        "domain_note": (
            f"training emission range {covered[0]}-{covered[1]} nm, dilute solution; "
            "aggregate behaviour and NIR-II are extrapolation"
        ),
    }


# Band edges in nm, used to score a predicted wavelength against a GoalSpec.
BAND_EDGES = {"UV": (200, 420), "visible": (420, 700), "NIR-I": (700, 1000), "NIR-II": (1000, 1700)}


def band_fit(prediction: dict | None, goal: dict) -> float | None:
    """
    How well a predicted emission matches the target window, on 0-1.

    Returns None when there is no prediction or no target, so a caller can
    tell "no opinion" from "predicted badly" and leave the proxy ranking
    untouched rather than multiplying it by a zero it invented.
    """
    if not prediction:
        return None
    target = goal.get("band") or goal.get("targets", {}).get("band")
    edges = BAND_EDGES.get(target or "")
    if not edges:
        return None
    emission = prediction["emission_nm"]
    if edges[0] <= emission <= edges[1]:
        return 1.0
    # Linear falloff over 300 nm either side; a near miss is worth something.
    distance = edges[0] - emission if emission < edges[0] else emission - edges[1]
    return round(max(0.0, 1.0 - distance / 300.0), 3)


def blend(proxy_score: float, prediction: dict | None, goal: dict, weight: float = 0.3) -> dict:
    """
    Combine the structural proxy with the model's band fit.

    The proxy keeps the majority of the weight: it is calibrated against known
    dyes, while the model is extrapolating the moment a candidate leaves the
    training window. When the model has no opinion the proxy passes through
    unchanged.
    """
    fit = band_fit(prediction, goal)
    if fit is None:
        return {"score": proxy_score, "ml_used": False}
    combined = (1 - weight) * proxy_score + weight * fit
    return {
        "score": round(combined, 4),
        "ml_used": True,
        "ml_band_fit": fit,
        "proxy_score": proxy_score,
        "in_domain": prediction.get("in_domain", False),
    }


def _metadata() -> dict:
    path = os.path.join(MODEL_DIR, "metadata.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def describe() -> str:
    """One line for the pipeline log."""
    if not available():
        return "no trained model; ranking on structural proxies only (train with train_model.py)"
    meta = _metadata()
    return (
        f"model trained on {meta.get('rows', '?')} molecule-solvent pairs "
        f"(emission MAE {meta.get('emission_mae', '?')} nm, PLQY ROC-AUC {meta.get('plqy_auc', '?')})"
    )


if __name__ == "__main__":
    import sys

    print(describe())
    for smiles in sys.argv[1:]:
        print(f"{smiles}\n  {predict(smiles)}")
