"""
The properties-and-ADMET box of the scoring step, as rules rather than a model.

Nothing here is a predicted ADMET endpoint. This module computes descriptors
with RDKit, checks them against three published rule sets, estimates aqueous
solubility from Delaney's ESOL regression, and matches three RDKit
structural-alert catalogues. Every number follows from the structure alone --
no training data, no fit to this project's chemistry -- which is why it is
reported as rules and estimates and never as a measured or predicted value.

Scope matters more here than anywhere else in the pipeline. Lipinski, Veber
and Egan describe oral absorption of small-molecule drugs; a theranostic
luminogen is usually injected, and often as a nanoparticle, so a violation is
a note for the formulation rather than a reason to drop a candidate. Every
check is advisory, and step 4 never lets one decide whether a candidate fits
the profile.

Two things here do bear on the experiment that step 5 plans. Solubility: a
compound ESOL puts below roughly a microgram per millilitre will not give a
clean aqueous photophysics measurement without a co-solvent stock, and the
plan should say so before the lab finds out. Reactivity: a Brenk or NIH alert
names a group that can decompose or bind covalently, which belongs beside the
dark-cytotoxicity control, and a PAINS alert names one that interferes with
fluorescence and redox readouts -- the two readouts this pipeline lives on.
"""

from __future__ import annotations

import warnings

from _common import require_rdkit

Chem = require_rdkit()
from rdkit.Chem import Descriptors, rdMolDescriptors  # noqa: E402

with warnings.catch_warnings():
    # Some RDKit builds register the FilterCatalog converters twice on import
    # and say so on stderr; the catalog works either way.
    warnings.filterwarnings("ignore", message="to-Python converter for .*FilterHierarchyMatcher")
    from rdkit.Chem import FilterCatalog  # noqa: E402


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


def descriptors(mol) -> dict:
    """The inputs every rule and the solubility estimate below are computed from."""
    heavy = mol.GetNumHeavyAtoms()
    aromatic = sum(1 for atom in mol.GetAtoms() if atom.GetIsAromatic())
    return {
        "molecular_weight": round(Descriptors.MolWt(mol), 2),
        "logp": round(Descriptors.MolLogP(mol), 2),
        "tpsa": round(Descriptors.TPSA(mol), 2),
        "hbd": rdMolDescriptors.CalcNumHBD(mol),
        "hba": rdMolDescriptors.CalcNumHBA(mol),
        "rotatable_bonds": Descriptors.NumRotatableBonds(mol),
        "heavy_atoms": heavy,
        "aromatic_fraction": round(aromatic / heavy, 3) if heavy else 0.0,
        "formal_charge": Chem.GetFormalCharge(mol),
    }


# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------
#
# `allowed` is how many limits the rule itself tolerates: Lipinski's rule of
# five predicts poor absorption at two or more violations, so one is a pass
# under the rule as published. Veber and Egan name both of their criteria as
# necessary.

RULES: dict[str, dict] = {
    "lipinski": {
        "limits": (("molecular_weight", 500.0), ("logp", 5.0), ("hbd", 5.0), ("hba", 10.0)),
        "allowed": 1,
        "scope": "oral absorption of small-molecule drugs (Lipinski 2001); advisory for an injected or nanoformulated agent",
    },
    "veber": {
        "limits": (("rotatable_bonds", 10.0), ("tpsa", 140.0)),
        "allowed": 0,
        "scope": "oral bioavailability in rats (Veber 2002); advisory for an injected agent",
    },
    "egan": {
        "limits": (("logp", 5.88), ("tpsa", 131.6)),
        "allowed": 0,
        "scope": "passive intestinal absorption (Egan 2000); advisory for an injected agent",
    },
}


def rules(values: dict) -> list[dict]:
    """Each rule set as an advisory check, naming the limits the structure exceeds."""
    out: list[dict] = []
    for name, rule in RULES.items():
        broken = [f"{key.replace('_', ' ')} {values[key]:g} > {limit:g}" for key, limit in rule["limits"] if values[key] > limit]
        out.append({
            "rule": name,
            "pass": len(broken) <= rule["allowed"],
            "severity": "advisory",
            "detail": f"{len(broken)} of {len(rule['limits'])} limits exceeded" + (f": {'; '.join(broken)}" if broken else ""),
            "scope": rule["scope"],
        })
    return out


# ---------------------------------------------------------------------------
# Aqueous solubility
# ---------------------------------------------------------------------------
#
# ESOL, Delaney 2004: a four-term regression on 2874 measured solubilities,
#
#     log S = 0.16 - 0.63 clogP - 0.0062 MW + 0.066 RB - 0.74 AP
#
# with a reported held-out error near 0.7 log units. A class is all that
# supports; a concentration quoted to two significant figures is not.

ESOL_DOMAIN = {"H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "I"}
# At log S = -6 and a typical 500 g/mol luminogen this is about 0.5 ug/mL:
# below it, a dilute aqueous measurement needs a co-solvent stock.
POORLY_SOLUBLE = -6.0
# The regression was fitted on drug-like organics, and these bounds are a
# guard against quoting a concentration for chemistry far outside that set
# rather than a boundary the paper publishes. They earn their place here: a
# NIR-II luminogen reaches clogP 12 routinely, which puts ESOL several log
# units past anything it was fitted on, and 1e-9 mg/mL printed to three
# figures would be a fabricated number. Past them the estimate is reported as
# a direction and the concentration is withheld.
ESOL_LOGP = (-3.0, 8.0)
ESOL_MW = 800.0


def solubility(mol, values: dict) -> dict:
    log_s = (
        0.16
        - 0.63 * values["logp"]
        - 0.0062 * values["molecular_weight"]
        + 0.066 * values["rotatable_bonds"]
        - 0.74 * values["aromatic_fraction"]
    )
    outside = sorted({atom.GetSymbol() for atom in mol.GetAtoms()} - ESOL_DOMAIN)
    low, high = ESOL_LOGP
    reason = (
        f"outside the regression's element set ({', '.join(outside)})" if outside
        else "a charged species; the regression was fitted on neutral organics" if values["formal_charge"]
        else f"clogP {values['logp']:g} is outside the {low:g} to {high:g} of its drug-like training set" if not low <= values["logp"] <= high
        else f"molecular weight {values['molecular_weight']:g} is above the {ESOL_MW:g} of its drug-like training set" if values["molecular_weight"] > ESOL_MW
        else None
    )
    return {
        "log_s": round(log_s, 2),
        # log S is mol/L, so the mass concentration in g/L is numerically mg/mL.
        **({} if reason else {"mg_per_ml": float(f"{10 ** log_s * values['molecular_weight']:.3g}")}),
        "class": (
            "soluble" if log_s >= -2
            else "moderately soluble" if log_s >= -4
            else "slightly soluble" if log_s >= POORLY_SOLUBLE
            else "poorly soluble"
        ),
        "in_domain": reason is None,
        "method": "ESOL (Delaney 2004) estimate, roughly +/-0.7 log units on its own test set; not a measurement",
        **({"out_of_domain": reason} if reason else {}),
    }


# ---------------------------------------------------------------------------
# Structural alerts
# ---------------------------------------------------------------------------

ALERTS: dict[str, str] = {
    "PAINS": "may interfere with fluorescence or redox assays; check controls",
    "BRENK": "a group associated with instability or reactivity; relevant to shelf life and to the dark-cytotoxicity control",
    "NIH": "reactive or assay-interfering group from the NIH screening filters",
}


def _catalog(name: str):
    params = FilterCatalog.FilterCatalogParams()
    params.AddCatalog(getattr(FilterCatalog.FilterCatalogParams.FilterCatalogs, name))
    return FilterCatalog.FilterCatalog(params)


CATALOGS = {name: _catalog(name) for name in ALERTS}


def alerts(mol) -> dict[str, list[str]]:
    return {name: [entry.GetDescription() for entry in catalog.GetMatches(mol)] for name, catalog in CATALOGS.items()}


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------

BASIS = (
    "computed descriptors, three published rule sets applied as advisory, an ESOL solubility estimate "
    "and PAINS/Brenk/NIH structural alerts; no trained ADMET model and no predicted ADMET endpoint"
)


def assess(mol, goal: dict | None = None) -> dict:
    """
    The ADMET-adjacent block for one candidate, plus the flags worth carrying.

    A flag here is something that changes what the lab should do -- prepare a
    co-solvent stock, keep a control, watch for decomposition -- not a mark
    against the molecule. Rule violations alone do not produce one: for an
    injected luminogen they are expected often enough that flagging them
    would bury the two that matter.
    """
    values = descriptors(mol)
    water = solubility(mol, values)
    matched = alerts(mol)
    aqueous = bool((goal or {}).get("constraints", {}).get("aqueous"))

    flags: list[str] = []
    if water["class"] == "poorly soluble":
        flags.append(
            (
                f"ESOL estimates {water['mg_per_ml']:g} mg/mL aqueous solubility"
                if water["in_domain"]
                else f"ESOL reaches log S {water['log_s']:g} by extrapolation ({water['out_of_domain']}), a direction and not a concentration"
            )
            + ": a dilute aqueous measurement needs a co-solvent stock and an aggregation check"
            + ("; the GoalSpec asks for an aqueous agent" if aqueous else "")
        )
    for name, found in matched.items():
        if found:
            label = name if name != "BRENK" else "Brenk"
            flags.append(f"{label} alert ({', '.join(found)}): {ALERTS[name]}")

    return {
        "descriptors": values,
        "rules": rules(values),
        "solubility": water,
        "alerts": matched,
        "flags": flags,
        "basis": BASIS,
    }
