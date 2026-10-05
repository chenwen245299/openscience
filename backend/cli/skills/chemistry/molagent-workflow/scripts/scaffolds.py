"""
Chromophore scaffold library.

The drug-discovery skills in this repo index scaffolds by target class. A
luminogen is indexed by what its excited state does, so this library keys
each scaffold to the optical window it reaches and the decay channels it
supports. It is the vocabulary step 2 searches with and step 3 builds from.

Every entry carries its role so a selection can be explained:

  core       the chromophore that sets the gap
  acceptor   electron-poor unit that red-shifts and lowers T1
  donor      electron-rich unit that feeds the charge-transfer state
  rotor      a motif whose motion drains S1 as heat, or gives AIE on packing
  shield     bulk that keeps water and neighbours off the backbone
  anchor     targeting or solubilising handle

`band` is the window the scaffold typically reaches in a real dye built
around it, not a property of the fragment alone.
"""

from __future__ import annotations

SCAFFOLDS: list[dict] = [
    # ---- cores -----------------------------------------------------------
    {
        "name": "BODIPY",
        "smiles": "C1=CC=[N+]2[B-](F)(F)N3C=CC=C3C=C12",
        "role": "core",
        "band": "visible",
        "channels": ["FLI", "PDT"],
        "note": "bright, narrow emission; halogenate the 2,6-positions for ISC",
    },
    {
        "name": "aza-BODIPY",
        "smiles": "C1=CC=[N+]2[B-](F)(F)N3C=CC=C3N=C12",
        "role": "core",
        "band": "NIR-I",
        "channels": ["FLI", "PDT", "PTT"],
        "note": "aza substitution pushes BODIPY into the NIR-I window",
    },
    {
        "name": "cyanine_Cy5",
        "smiles": "CC1(C)c2ccccc2N(C)/C1=C/C=C/C=C1\\N(C)c2ccccc2C1(C)C",
        "role": "core",
        "band": "NIR-I",
        "channels": ["FLI", "PAI", "PTT"],
        "note": "high extinction, poor photostability; the ICG family",
    },
    {
        "name": "squaraine",
        "smiles": "O=C1C(=C2N(C)c3ccccc3C2(C)C)C(=O)C1=C1N(C)c2ccccc2C1(C)C",
        "role": "core",
        "band": "NIR-I",
        "channels": ["FLI", "PAI", "PTT"],
        "note": "sharp intense absorption; aggregates readily",
    },
    {
        "name": "porphyrin",
        "smiles": "c1cc2cc3ccc(cc4ccc(cc5ccc(cc1n2)[nH]5)n4)[nH]3",
        "role": "core",
        "band": "visible",
        "channels": ["PDT", "FLI"],
        "note": "the clinical PDT family (Photofrin, temoporfin)",
    },
    {
        "name": "phthalocyanine",
        "smiles": "c1ccc2c(c1)c1nc-2nc2[nH]c(nc3nc(nc4[nH]c(n1)c1ccccc41)c1ccccc31)c1ccccc21",
        "role": "core",
        "band": "NIR-I",
        "channels": ["PDT", "PTT", "FLI"],
        "note": "strong Q band near 700 nm; metalate to tune ISC",
    },
    {
        "name": "rhodamine_xanthene",
        "smiles": "c1ccc2c(c1)Oc1ccccc1C2",
        "role": "core",
        "band": "visible",
        "channels": ["FLI"],
        "note": "bright and photostable; spirocyclisation gives activatable probes",
    },
    {
        "name": "coumarin",
        "smiles": "O=c1ccc2ccccc2o1",
        "role": "core",
        "band": "UV",
        "channels": ["FLI"],
        "note": "blue emitter, large Stokes shift when push-pull substituted",
    },
    {
        "name": "naphthalimide",
        "smiles": "O=C1NC(=O)c2cccc3cccc1c23",
        "role": "core",
        "band": "visible",
        "channels": ["FLI", "PDT"],
        "note": "thionate the carbonyl for heavy-atom-free ISC",
    },
    {
        "name": "perylenediimide",
        "smiles": "O=C1NC(=O)c2ccc3c4ccc5c(=O)[nH]c(=O)c6ccc(c3c24)c5c61",
        "role": "core",
        "band": "visible",
        "channels": ["FLI", "PDT"],
        "note": "S1/Tn matching gives long triplet lifetimes without heavy atoms",
    },
    {
        "name": "diketopyrrolopyrrole",
        "smiles": "O=C1NC(c2ccccc2)=C2C(=O)NC(c3ccccc3)=C12",
        "role": "core",
        "band": "NIR-I",
        "channels": ["FLI", "PTT", "PAI"],
        "note": "rigid, strongly absorbing; common in NIR-I photothermal agents",
    },
    # ---- acceptors -------------------------------------------------------
    {
        "name": "benzothiadiazole",
        "smiles": "c1ccc2nsnc2c1",
        "role": "acceptor",
        "band": "visible",
        "channels": ["FLI", "PDT"],
        "note": "the workhorse acceptor; large Stokes shift in D-A-D dyes",
    },
    {
        "name": "benzobisthiadiazole",
        "smiles": "c1cc2nsnc2c2nsnc12",
        "role": "acceptor",
        "band": "NIR-II",
        "channels": ["FLI", "PTT", "PAI"],
        "note": "the acceptor that reaches NIR-II; pair with twisted donors",
    },
    {
        "name": "thiadiazoloquinoxaline",
        "smiles": "c1ccc2nc3nsnc3nc2c1",
        "role": "acceptor",
        "band": "NIR-II",
        "channels": ["FLI", "PTT"],
        "note": "stronger acceptor than BTD; deep NIR with extended donors",
    },
    {
        "name": "quinoxaline",
        "smiles": "c1ccc2nccnc2c1",
        "role": "acceptor",
        "band": "visible",
        "channels": ["FLI"],
        "note": "mild acceptor, easy to functionalise",
    },
    {
        "name": "dicyanovinyl",
        "smiles": "N#CC(C#N)=C",
        "role": "acceptor",
        "band": "NIR-I",
        "channels": ["PTT", "PAI"],
        "note": "strong acceptor; drives TICT and nonradiative decay",
    },
    {
        "name": "anthraquinone",
        "smiles": "O=C1c2ccccc2C(=O)c2ccccc21",
        "role": "acceptor",
        "band": "visible",
        "channels": ["PDT"],
        "note": "electron-transfer mediator; a Type I route",
    },
    {
        "name": "pyridinium",
        "smiles": "C[n+]1ccccc1",
        "role": "acceptor",
        "band": "visible",
        "channels": ["PDT"],
        "note": "cationisation shrinks the S-T gap and targets mitochondria",
    },
    # ---- donors ----------------------------------------------------------
    {
        "name": "triphenylamine",
        "smiles": "c1ccc(N(c2ccccc2)c2ccccc2)cc1",
        "role": "donor",
        "band": "visible",
        "channels": ["FLI", "PTT"],
        "note": "propeller donor; twist gives AIE and separates HOMO from LUMO",
    },
    {
        "name": "carbazole",
        "smiles": "c1ccc2c(c1)[nH]c1ccccc12",
        "role": "donor",
        "band": "visible",
        "channels": ["FLI"],
        "note": "rigid donor, high triplet energy",
    },
    {
        "name": "phenothiazine",
        "smiles": "c1ccc2c(c1)Nc1ccccc1S2",
        "role": "donor",
        "band": "visible",
        "channels": ["FLI", "PDT"],
        "note": "butterfly donor; strong, easily oxidised",
    },
    {
        "name": "phenoxazine",
        "smiles": "c1ccc2c(c1)Nc1ccccc1O2",
        "role": "donor",
        "band": "visible",
        "channels": ["FLI"],
        "note": "stronger donor than carbazole; small Delta E_ST in D-A pairs",
    },
    {
        "name": "ethylenedioxythiophene",
        "smiles": "c1cc2OCCOc2s1",
        "role": "donor",
        "band": "NIR-I",
        "channels": ["FLI", "PTT"],
        "note": "electron-rich pi bridge that red-shifts without twisting",
    },
    # ---- rotors and shields ---------------------------------------------
    {
        "name": "tetraphenylethylene",
        "smiles": "C(=C(c1ccccc1)c1ccccc1)(c1ccccc1)c1ccccc1",
        "role": "rotor",
        "band": "UV",
        "channels": ["FLI", "PTT"],
        "note": "the canonical AIE rotor; dark in solution, emissive on packing",
    },
    {
        "name": "triphenylethylene",
        "smiles": "C(=C(c1ccccc1)c1ccccc1)c1ccccc1",
        "role": "rotor",
        "band": "UV",
        "channels": ["FLI", "PTT"],
        "note": "lighter AIE rotor, one fewer ring to shield",
    },
    {
        "name": "dialkoxybenzene_shield",
        "smiles": "CCCCOc1ccccc1OCCCC",
        "role": "shield",
        "band": "visible",
        "channels": ["FLI"],
        "note": "shielding unit; keeps water off the backbone and aids renal clearance",
    },
    # ---- anchors ---------------------------------------------------------
    {
        "name": "triphenylphosphonium",
        "smiles": "C[P+](c1ccccc1)(c1ccccc1)c1ccccc1",
        "role": "anchor",
        "band": "UV",
        "channels": [],
        "note": "mitochondrial targeting, where short-lived ROS does most damage",
    },
    {
        "name": "morpholine",
        "smiles": "C1COCCN1",
        "role": "anchor",
        "band": "UV",
        "channels": [],
        "note": "lysosomal targeting",
    },
    {
        "name": "sulfonate",
        "smiles": "CS(=O)(=O)[O-]",
        "role": "anchor",
        "band": "UV",
        "channels": [],
        "note": "water solubility without a nanoparticle formulation",
    },
]

BAND_ORDER = {"UV": 0, "visible": 1, "NIR-I": 2, "NIR-II": 3}


def select(goal: dict, roles: list[str] | None = None) -> list[dict]:
    """
    Scaffolds worth searching or building with for one GoalSpec.

    A scaffold qualifies when it serves a modality the goal asks for. The band
    filter is deliberately loose: a visible-band acceptor is still the right
    starting point for a NIR-II dye once it is extended, so only scaffolds
    far redder than the target are dropped.
    """
    want_band = goal.get("band") or goal.get("targets", {}).get("band") or "visible"
    want_channels = set(goal.get("modalities", []))
    ceiling = BAND_ORDER.get(want_band, 1)

    picked = []
    for entry in SCAFFOLDS:
        if roles and entry["role"] not in roles:
            continue
        if entry["role"] in ("rotor", "shield", "anchor"):
            picked.append(entry)
            continue
        if entry["channels"] and want_channels and not (set(entry["channels"]) & want_channels):
            continue
        if BAND_ORDER.get(entry["band"], 1) > ceiling + 1:
            continue
        picked.append(entry)

    # Nearest-band-first, so a NIR-II goal sees BBTD before benzothiadiazole.
    picked.sort(key=lambda e: (abs(BAND_ORDER.get(e["band"], 1) - ceiling), e["name"]))
    return picked


def by_role(entries: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for entry in entries:
        out.setdefault(entry["role"], []).append(entry)
    return out


def validate() -> list[str]:
    """Report scaffolds whose SMILES RDKit rejects. Used by the test suite."""
    from rdkit import Chem, RDLogger

    RDLogger.DisableLog("rdApp.*")
    return [e["name"] for e in SCAFFOLDS if Chem.MolFromSmiles(e["smiles"]) is None]


if __name__ == "__main__":
    bad = validate()
    print(f"{len(SCAFFOLDS)} scaffolds, {len(bad)} invalid")
    for name in bad:
        print(f"  INVALID: {name}")
