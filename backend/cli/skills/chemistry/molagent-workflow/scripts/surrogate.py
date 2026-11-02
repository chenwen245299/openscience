"""
Fingerprint space, applicability domain, and the lab-data surrogate.

Steps 4 to 6 share one notion of chemical distance: Morgan radius-2
fingerprints compared by Tanimoto similarity, the same space step 2 thins
its shortlist in and the optional predictor is trained on.

The surrogate is a Gaussian process with a Tanimoto kernel, fitted only to
measurements the lab has returned through step 6. It is the standard choice
for small-data molecular Bayesian optimisation because it needs no training
set beyond those measurements, has no hyperparameters except the noise, and
returns a posterior standard deviation that grows as a candidate moves away
from anything measured. With fewer than MIN_POINTS measured compounds for an
objective there is no surrogate for it, and callers say so instead of
fitting a line through two points.

NumPy only; RDKit already depends on it.
"""

from __future__ import annotations

import math

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold

FP_SIZE = 2048
MIN_POINTS = 3
# Noise variances tried on standardised targets; the marginal likelihood picks one.
NOISE_GRID = (0.01, 0.05, 0.1, 0.3, 1.0)

_generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=FP_SIZE)


def fingerprint(smiles: str) -> np.ndarray | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    bits = np.zeros(FP_SIZE, dtype=np.float64)
    bits[list(_generator.GetFingerprint(mol).GetOnBits())] = 1.0
    return bits


def matrix(smiles: list[str]) -> np.ndarray:
    """Stack fingerprints; an unparsable SMILES becomes an all-zero row."""
    rows = [fingerprint(s) for s in smiles]
    return np.array([row if row is not None else np.zeros(FP_SIZE) for row in rows]).reshape(len(rows), FP_SIZE)


def tanimoto(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Pairwise Tanimoto similarity between two fingerprint matrices."""
    shared = left @ right.T
    union = left.sum(axis=1)[:, None] + right.sum(axis=1)[None, :] - shared
    return np.divide(shared, union, out=np.zeros_like(shared), where=union > 0)


def nearest(smiles: list[str], reference: list[str]) -> list[float]:
    """Each molecule's highest similarity to any reference molecule; 0 with no reference."""
    if not smiles:
        return []
    if not reference:
        return [0.0] * len(smiles)
    return [round(float(value), 3) for value in tanimoto(matrix(smiles), matrix(reference)).max(axis=1)]


def scaffold(smiles: str) -> str:
    """Bemis-Murcko scaffold, or the input when it has no ring system to reduce to."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return smiles
    core = MurckoScaffold.GetScaffoldForMol(mol)
    return Chem.MolToSmiles(core) if core.GetNumAtoms() else Chem.MolToSmiles(mol)


def inchikey(smiles: str) -> str | None:
    from rdkit.Chem import inchi

    mol = Chem.MolFromSmiles(smiles)
    return inchi.MolToInchiKey(mol) if mol is not None else None


# ---------------------------------------------------------------------------
# Gaussian process
# ---------------------------------------------------------------------------


class Surrogate:
    """Tanimoto-kernel GP on standardised targets, with exact leave-one-out."""

    def __init__(self, smiles: list[str], values: list[float]):
        if len(smiles) < MIN_POINTS:
            raise ValueError(f"a surrogate needs at least {MIN_POINTS} measured compounds, got {len(smiles)}")
        self.smiles = smiles
        self.x = matrix(smiles)
        y = np.array(values, dtype=np.float64)
        self.mean = float(y.mean())
        self.scale = float(y.std()) or 1.0
        self.y = (y - self.mean) / self.scale
        kernel = tanimoto(self.x, self.x)
        best = None
        for noise in NOISE_GRID:
            fit = self._fit(kernel, noise)
            if fit is not None and (best is None or fit["evidence"] > best["evidence"]):
                best = fit
        if best is None:
            raise ValueError("the kernel matrix could not be factorised at any noise level")
        self.noise = best["noise"]
        self.chol = best["chol"]
        self.alpha = best["alpha"]
        self.inverse_diag = best["inverse_diag"]

    def _fit(self, kernel: np.ndarray, noise: float) -> dict | None:
        try:
            chol = np.linalg.cholesky(kernel + noise * np.eye(len(self.y)))
        except np.linalg.LinAlgError:
            return None
        alpha = np.linalg.solve(chol.T, np.linalg.solve(chol, self.y))
        inverse = np.linalg.solve(chol.T, np.linalg.solve(chol, np.eye(len(self.y))))
        evidence = -0.5 * float(self.y @ alpha) - float(np.log(np.diag(chol)).sum()) - 0.5 * len(self.y) * math.log(2 * math.pi)
        return {"noise": noise, "chol": chol, "alpha": alpha, "inverse_diag": np.diag(inverse), "evidence": evidence}

    def predict(self, smiles: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """Posterior mean and standard deviation, standardised units."""
        if not smiles:
            return np.zeros(0), np.zeros(0)
        cross = tanimoto(matrix(smiles), self.x)
        mean = cross @ self.alpha
        v = np.linalg.solve(self.chol, cross.T)
        variance = np.clip(1.0 - (v * v).sum(axis=0), 1e-9, None)
        return mean, np.sqrt(variance)

    def original(self, mean: np.ndarray, std: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return mean * self.scale + self.mean, std * self.scale

    def best(self) -> float:
        """Best observed value, standardised."""
        return float(self.y.max())

    def leave_one_out(self) -> dict:
        """
        Exact GP leave-one-out, so the reported error is for points the fit did
        not see. With a handful of measurements this is the only honest error
        estimate there is, and it is still a small-sample one.
        """
        loo = self.y - self.alpha / self.inverse_diag
        observed = self.y * self.scale + self.mean
        predicted = loo * self.scale + self.mean
        return {
            "n": len(self.y),
            "rmse": round(float(np.sqrt(np.mean((observed - predicted) ** 2))), 4),
            "spearman": spearman(list(observed), list(predicted)),
            "noise": self.noise,
        }


def spearman(left: list[float], right: list[float]) -> float | None:
    """Rank correlation with average ranks for ties; None below three pairs or without spread."""
    if len(left) != len(right) or len(left) < 3:
        return None
    a, b = _ranks(left), _ranks(right)
    if a.std() == 0 or b.std() == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 3)


def _ranks(values: list[float]) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values))
    ranks[order] = np.arange(len(values), dtype=np.float64)
    data = np.array(values, dtype=np.float64)
    for value in np.unique(data):
        tied = data == value
        ranks[tied] = ranks[tied].mean()
    return ranks


def expected_improvement(mean: float, std: float, best: float, xi: float = 0.01) -> float:
    """Closed-form EI for maximisation, in the same units as mean and best."""
    if std <= 0:
        return max(mean - best - xi, 0.0)
    gap = mean - best - xi
    z = gap / std
    cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    return max(gap * cdf + std * pdf, 0.0)


def normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))
