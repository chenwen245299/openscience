"""
Photophysical descriptors and design rules for theranostic luminogens.

The drug-discovery descriptors already in this repo (QED, Lipinski, ADMET)
say nothing about whether a molecule emits, crosses to a triplet, or turns
light into heat. This module supplies the missing layer: interpretable
structural proxies for the three excited-state decay channels that compete
in every phototheranostic agent.

    S1 --(radiative)------> fluorescence        imaging (FLI)
       --(ISC -> T1)------> ROS / phosphorescence   therapy (PDT)
       --(internal conv.)-> heat               therapy (PTT), imaging (PAI)

Phi_F + Phi_ISC + Phi_IC ~= 1, so the three are traded against one another
and a design target is a *balance*, not a maximum.

Everything here is a structural proxy computed from the 2D graph. None of it
replaces TD-DFT or measurement. Where a quantity genuinely needs quantum
chemistry (Delta E_ST, SOC, T1 energy, oscillator strength) the function says
so and returns a flag rather than inventing a number.

References for the rules are in ../references/design-rules.md.
"""

from __future__ import annotations

import math
import math
from collections import deque

from _common import require_rdkit

Chem = require_rdkit()
from rdkit.Chem import Descriptors, rdMolDescriptors  # noqa: E402

# ---------------------------------------------------------------------------
# Fragment vocabulary
# ---------------------------------------------------------------------------
# Strength is an ordinal rank used only to compare candidates against one
# another, not an absolute Hammett-style constant.

DONORS: list[tuple[str, str, int]] = [
    ("triphenylamine", "[NX3](c)(c)c", 3),
    ("carbazole", "c1ccc2c(c1)[nX3]c1ccccc12", 3),
    ("phenothiazine", "c1ccc2c(c1)[NX3]c1ccccc1S2", 3),
    ("phenoxazine", "c1ccc2c(c1)[NX3]c1ccccc1O2", 3),
    ("julolidine", "C1CCN2CCCc3cccc1c32", 3),
    ("dialkylamino_aryl", "[NX3;H0;!$(N=*);!$(N#*);!$(N[O])]([CX4])([CX4])c", 2),
    ("alkylamino_aryl", "[NX3;H1;!$(N=*)]([CX4])c", 2),
    ("alkoxy_aryl", "[OX2]([CX4])c", 1),
    ("thiophene", "c1ccsc1", 1),
    ("ethylenedioxythiophene", "c1cc2OCCOc2s1", 2),
    ("fluorene", "C1(c2ccccc2-c2ccccc21)", 1),
    ("methylthio_aryl", "[SX2]([CX4])c", 1),
]

ACCEPTORS: list[tuple[str, str, int]] = [
    # Benzobisthiadiazole-class acceptors are what push emission into NIR-II.
    # The pattern is the whole bis-fused core: a single thiadiazole ring also
    # matches plain benzothiadiazole and would score a BTD dye as if it were
    # a BBTD one.
    ("benzobisthiadiazole", "c1cc2nsnc2c2nsnc12", 5),
    ("thiadiazoloquinoxaline", "c1ccc2nc3nsnc3nc2c1", 5),
    ("benzothiadiazole", "c1ccc2nsnc2c1", 4),
    # Aryl imides (naphthalimide, perylenediimide) are acceptors in their own
    # right; without this they read as bare hydrocarbons.
    ("aryl_imide", "O=[CX3](c)[NX3][CX3](=O)c", 3),
    ("aryl_imide_cyclic", "O=[CX3]~[#7]~[CX3]=O", 2),
    ("benzoselenadiazole", "c1ccc2n[se]nc2c1", 4),
    ("benzoxadiazole", "c1ccc2nonc2c1", 3),
    ("diketopyrrolopyrrole", "O=C1NC(=C1)C(=O)N", 4),
    ("isoindigo", "O=C1Nc2ccccc2C1=C1C(=O)Nc2ccccc21", 4),
    ("dicyanovinyl", "[CX3]=[CX3]([CX2]#[NX1])[CX2]#[NX1]", 4),
    ("tricyanofuran", "[CX2]#[NX1].[CX2]#[NX1].[CX2]#[NX1]", 4),
    ("malononitrile", "[CX4,CX3]([CX2]#[NX1])[CX2]#[NX1]", 3),
    ("quinoxaline", "c1ccc2nccnc2c1", 3),
    ("pyridinium", "[n+]", 3),
    ("ammonium_cation", "[N+;!$([N+][O-])]", 2),
    ("quinone", "O=C1C=CC(=O)C=C1", 4),
    ("naphthoquinone", "O=C1C=CC(=O)c2ccccc12", 4),
    ("anthraquinone", "O=C1c2ccccc2C(=O)c2ccccc12", 4),
    ("nitro", "[NX3](=[OX1])=[OX1]", 3),
    ("nitro_charged", "[N+](=[OX1])[O-]", 3),
    ("sulfone", "[SX4](=[OX1])(=[OX1])", 2),
    ("cyano", "[CX2]#[NX1]", 2),
    ("indanone", "O=C1CCc2ccccc21", 2),
    ("benzothiazole", "c1ccc2scnc2c1", 2),
    ("triazine", "c1ncncn1", 3),
]

# Motifs whose intramolecular rotation is the usual route to AIE and to
# heat generation (intramolecular-motion-induced photothermy).
ROTOR_MOTIFS: list[tuple[str, str]] = [
    ("tetraphenylethylene", "C(=C(c1ccccc1)c1ccccc1)(c1ccccc1)c1ccccc1"),
    ("triphenylamine_propeller", "[NX3](c1ccccc1)(c1ccccc1)c1ccccc1"),
    ("triphenylethylene", "C(=C(c1ccccc1)c1ccccc1)c1ccccc1"),
    ("biaryl_axis", "c-c"),
]

# Heavy atoms that raise spin-orbit coupling, hence ISC, hence PDT.
# Value is a coarse Z^4-flavoured weight, normalised against iodine.
HEAVY_ISC = {
    "Br": 0.25,
    "I": 1.00,
    "Se": 0.22,
    "Te": 0.85,
    "Ru": 0.75,
    "Ir": 1.00,
    "Pt": 1.00,
    "Pd": 0.70,
    "Re": 0.95,
    "Os": 1.00,
    "Au": 1.00,
    "Zn": 0.12,
    "Gd": 0.80,
    "Yb": 0.85,
    "Er": 0.85,
    "Eu": 0.80,
    "Tb": 0.80,
}

# Heavy-atom-free routes to ISC (Molecules 2023 review; see design-rules.md).
HEAVY_ATOM_FREE_ISC: list[tuple[str, str, str]] = [
    ("thiocarbonyl", "[CX3]=[SX1]", "n-pi* / pi-pi* El-Sayed channel from thionation"),
    ("nitroaromatic", "c[NX3](=[OX1])=[OX1]", "low-lying n-pi* triplet"),
    ("ketone_aryl", "c[CX3]=[OX1]", "n-pi* channel, weak unless rigidified"),
    ("fullerene_c60", "C12=C3C4=C5C6=C1C7=C8C9=C1C%10=C%11C(=C29)C3=C2C3=C4C4=C5C5=C9C6=C7C6=C7C8=C1C1=C8C%10=C%10C%11=C2C2=C3C3=C4C4=C5C5=C%11C%12=C(C6=C95)C7=C1C1=C%12C5=C%11C4=C3C3=C5C(=C81)C%10=C23", "intrinsic spin converter"),
]

# Groups that improve aqueous behaviour or steer subcellular localisation.
BIOLOGY_MOTIFS: list[tuple[str, str, str]] = [
    ("peg_chain", "[OX2][CX4][CX4][OX2][CX4][CX4][OX2]", "solubility / stealth"),
    ("sulfonate", "[SX4](=[OX1])(=[OX1])[OX2H0-,OX2H]", "water solubility"),
    ("carboxylate", "[CX3](=[OX1])[OX2H1,OX1-]", "water solubility"),
    ("quaternary_ammonium", "[N+;H0;$([N+]([CX4])([CX4])([CX4])[CX4,c])]", "mitochondria targeting"),
    ("triphenylphosphonium", "[P+](c1ccccc1)(c1ccccc1)c1ccccc1", "mitochondria targeting"),
    ("morpholine", "C1COCCN1", "lysosome targeting"),
    ("long_alkyl", "[CX4][CX4][CX4][CX4][CX4][CX4]", "steric shielding / packing control"),
    ("alkoxy_shield", "[OX2]([CX4][CX4][CX4][CX4])c", "shielding unit"),
]


def _compiled(table):
    """Compile a SMARTS table once, dropping any pattern RDKit rejects."""
    out = []
    for row in table:
        pattern = Chem.MolFromSmarts(row[1])
        if pattern is not None:
            out.append((row[0], pattern, *row[2:]))
    return out


_DONORS = _compiled(DONORS)
_ACCEPTORS = _compiled(ACCEPTORS)
_ROTORS = _compiled(ROTOR_MOTIFS)
_HAF_ISC = _compiled(HEAVY_ATOM_FREE_ISC)
_BIOLOGY = _compiled(BIOLOGY_MOTIFS)


# ---------------------------------------------------------------------------
# Conjugation
# ---------------------------------------------------------------------------


def conjugated_system(mol) -> dict:
    """
    Size and reach of the largest connected sp2 network.

    `size` is the atom count, the quantity SyntheFluor-RL optimised as a
    fluorescence prerequisite. `path` is the longest through-bond path inside
    that network, which tracks the HOMO-LUMO gap far better than size does:
    a dendrimer and a linear polyene can share a size and differ by an eV.
    """
    sp2 = set()
    for atom in mol.GetAtoms():
        if atom.GetIsAromatic() or atom.GetHybridization() == Chem.HybridizationType.SP2:
            sp2.add(atom.GetIdx())
        # An sp carbon in a conjugated alkyne or nitrile still carries the pi system.
        elif atom.GetHybridization() == Chem.HybridizationType.SP and atom.GetDegree() <= 2:
            sp2.add(atom.GetIdx())

    adjacency = {i: [] for i in sp2}
    for bond in mol.GetBonds():
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in sp2 and b in sp2:
            adjacency[a].append(b)
            adjacency[b].append(a)

    seen: set[int] = set()
    best: list[int] = []
    for start in sp2:
        if start in seen:
            continue
        component = []
        queue = deque([start])
        seen.add(start)
        while queue:
            node = queue.popleft()
            component.append(node)
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        if len(component) > len(best):
            best = component

    return {
        "size": len(best),
        "path": _longest_path(adjacency, best),
        "fraction_of_heavy_atoms": round(len(best) / max(mol.GetNumHeavyAtoms(), 1), 3),
    }


def _longest_path(adjacency: dict, component: list[int]) -> int:
    """Graph diameter of one component, in atoms, by double BFS."""
    if not component:
        return 0
    if len(component) == 1:
        return 1

    def bfs(source: int):
        distance = {source: 0}
        queue = deque([source])
        far, best = source, 0
        while queue:
            node = queue.popleft()
            for neighbour in adjacency[node]:
                if neighbour not in distance:
                    distance[neighbour] = distance[node] + 1
                    if distance[neighbour] > best:
                        best, far = distance[neighbour], neighbour
                    queue.append(neighbour)
        return far, best

    # Double BFS gives the exact diameter on a tree and a tight lower bound
    # on a cyclic graph, which is all this proxy needs.
    endpoint, _ = bfs(component[0])
    _, diameter = bfs(endpoint)
    return diameter + 1


# ---------------------------------------------------------------------------
# Fragment matching
# ---------------------------------------------------------------------------


def _matches(mol, compiled) -> list[dict]:
    found = []
    for name, pattern, *rest in compiled:
        hits = mol.GetSubstructMatches(pattern, uniquify=True)
        if hits:
            entry = {"name": name, "count": len(hits)}
            if rest:
                entry["strength" if isinstance(rest[0], int) else "note"] = rest[0]
            found.append(entry)
    return found


def polymethine(mol) -> int:
    """
    Length of the longest open-chain methine bridge joining two nitrogens.

    Cyanines carry their colour on this bridge, not on a donor-acceptor pair:
    each added vinylene red-shifts by roughly 100 nm, which is how Cy3, Cy5,
    Cy7 and ICG walk from visible to NIR-I. A fragment vocabulary built for
    push-pull dyes scores them as plain hydrocarbons, so the bridge is counted
    directly. Returns 0 when there is no such bridge.
    """
    chain = [
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetSymbol() == "C" and not atom.GetIsAromatic() and atom.GetHybridization() == Chem.HybridizationType.SP2
    ]
    if not chain:
        return 0
    members = set(chain)
    nitrogens = {a.GetIdx() for a in mol.GetAtoms() if a.GetSymbol() == "N"}

    adjacency = {i: [] for i in chain}
    capped: set[int] = set()
    for bond in mol.GetBonds():
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in members and b in members:
            adjacency[a].append(b)
            adjacency[b].append(a)
        elif a in members and b in nitrogens:
            capped.add(a)
        elif b in members and a in nitrogens:
            capped.add(b)

    # Longest path between two nitrogen-capped chain carbons.
    best = 0
    for start in capped:
        stack = [(start, 1, {start})]
        while stack:
            node, length, seen = stack.pop()
            if node in capped and length > 1:
                best = max(best, length)
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    stack.append((neighbour, length + 1, seen | {neighbour}))
    return best


def donor_acceptor(mol) -> dict:
    """
    Push-pull character, the lever that sets both the absorption edge and the
    spatial HOMO/LUMO separation that a small Delta E_ST needs.
    """
    donors = _matches(mol, _DONORS)
    acceptors = _matches(mol, _ACCEPTORS)
    donor_strength = max((d["strength"] for d in donors), default=0)
    acceptor_strength = max((a["strength"] for a in acceptors), default=0)
    return {
        "donors": donors,
        "acceptors": acceptors,
        "donor_strength": donor_strength,
        "acceptor_strength": acceptor_strength,
        # Both ends must be present for a charge-transfer state to exist at all.
        "push_pull": donor_strength * acceptor_strength,
        "polymethine_length": polymethine(mol),
        "architecture": _architecture(len(donors), len(acceptors)),
    }


def _architecture(n_donor: int, n_acceptor: int) -> str:
    if not n_donor and not n_acceptor:
        return "neither"
    if n_donor and not n_acceptor:
        return "donor-only"
    if n_acceptor and not n_donor:
        return "acceptor-only"
    if n_donor >= 2 and n_acceptor == 1:
        return "D-A-D"
    if n_acceptor >= 2 and n_donor == 1:
        return "A-D-A"
    return "D-A"


def intersystem_crossing(mol) -> dict:
    """
    Structural routes to the triplet. Heavy atoms are the blunt instrument;
    the heavy-atom-free channels matter because iodine and selenium carry
    dark toxicity and shorten triplet lifetimes.
    """
    heavy = {}
    for atom in mol.GetAtoms():
        symbol = atom.GetSymbol()
        if symbol in HEAVY_ISC:
            heavy[symbol] = heavy.get(symbol, 0) + 1
    weight = sum(HEAVY_ISC[s] * n for s, n in heavy.items())
    free = _matches(mol, _HAF_ISC)

    # Twisted biaryl / orthogonal D-A geometry is the SOCT-ISC route. Count
    # rotatable bonds that join two aromatic rings as a proxy for the twist.
    twist_pattern = Chem.MolFromSmarts("[a;!$(a(:a):a)]-!@[a]")
    twists = len(mol.GetSubstructMatches(twist_pattern)) if twist_pattern else 0

    return {
        "heavy_atoms": heavy,
        "heavy_atom_weight": round(weight, 3),
        "heavy_atom_free_motifs": free,
        "biaryl_twists": twists,
        "routes": _isc_routes(weight, free, twists),
    }


def _isc_routes(weight: float, free: list[dict], twists: int) -> list[str]:
    routes = []
    if weight >= 0.5:
        routes.append("heavy-atom SOC")
    elif weight > 0:
        routes.append("weak heavy-atom SOC")
    for motif in free:
        routes.append(f"heavy-atom-free: {motif['name']}")
    if twists >= 2:
        routes.append("possible SOCT-ISC from twisted donor-acceptor geometry")
    return routes or ["no structural ISC route identified"]


def motion(mol) -> dict:
    """
    Rotors and flexibility. In dilute solution these drain S1 into heat; on
    aggregation they are restricted and the energy returns to fluorescence.
    The same feature therefore reads as 'photothermal' or 'AIE emitter'
    depending on the state the agent will be used in -- which is why the
    aggregate state has to be part of the design target.
    """
    rotatable = rdMolDescriptors.CalcNumRotatableBonds(mol)
    motifs = _matches(mol, _ROTORS)
    # A biaryl single bond is counted by `_ROTORS` through `biaryl_axis`; drop
    # it from the named motif list when a real propeller is present.
    named = [m for m in motifs if m["name"] != "biaryl_axis"]
    return {
        "rotatable_bonds": rotatable,
        "aie_motifs": named,
        "fraction_sp3": round(rdMolDescriptors.CalcFractionCSP3(mol), 3),
        "ring_count": rdMolDescriptors.CalcNumRings(mol),
        "fused_rings": _fused_ring_count(mol),
    }


def _fused_ring_count(mol) -> int:
    rings = mol.GetRingInfo().AtomRings()
    fused = 0
    for i, first in enumerate(rings):
        for second in rings[i + 1 :]:
            if len(set(first) & set(second)) >= 2:
                fused += 1
    return fused


def biology(mol) -> dict:
    """Aqueous behaviour, localisation handles, and the renal-clearance check."""
    motifs = _matches(mol, _BIOLOGY)
    logp = Descriptors.MolLogP(mol)
    return {
        "molecular_weight": round(Descriptors.MolWt(mol), 2),
        "logp": round(logp, 2),
        "tpsa": round(Descriptors.TPSA(mol), 2),
        "hbd": rdMolDescriptors.CalcNumHBD(mol),
        "hba": rdMolDescriptors.CalcNumHBA(mol),
        "formal_charge": Chem.GetFormalCharge(mol),
        "motifs": motifs,
        "targeting": sorted({m["note"] for m in motifs if "targeting" in str(m.get("note", ""))}),
    }


# ---------------------------------------------------------------------------
# Coarse spectral band
# ---------------------------------------------------------------------------
#
# Deliberately a BAND, not a wavelength. Conjugation length and push-pull
# strength set the gap to roughly the right decade; converting that to a
# number in nm would imply a precision this has no basis for. Use
# predictor.py (trained on ChemFluor) when a number is needed, and TD-DFT or
# measurement when it has to be right.

BANDS = ["UV", "visible", "NIR-I", "NIR-II"]

# Named chromophore classes that absorb further to the red than their size
# alone suggests, because the colour comes from a specific electronic
# arrangement rather than from sheer conjugation. A BODIPY is the clearest
# case: eleven conjugated atoms, and it absorbs at 500 nm.
CHROMOPHORE_CORES: list[tuple[str, str, float]] = [
    ("aza-BODIPY", "C1=CC=[N+]2[B-](F)(F)N3C=CC=C3N=C12", 2.5),
    ("BODIPY", "C1=CC=[N+]2[B-](F)(F)N3C=CC=C3C=C12", 1.5),
    ("squaraine", "O=C1C(=C)C(=O)C1=C", 2.0),
    ("azo", "c[NX2]=[NX2]c", 1.0),
]

_CORES: list[tuple[str, object, float]] = []


def _core_bonus(mol) -> float:
    """Largest single core bonus; they are alternatives, not additive."""
    if mol is None:
        return 0.0
    global _CORES
    if not _CORES:
        _CORES = [
            (name, pattern, bonus)
            for name, smarts, bonus in CHROMOPHORE_CORES
            if (pattern := Chem.MolFromSmarts(smarts)) is not None
        ]
    return max((bonus for _, pattern, bonus in _CORES if mol.HasSubstructMatch(pattern)), default=0.0)


def spectral_band(mol, conjugation: dict, da: dict) -> dict:
    """
    Three terms, each standing for a different way a gap gets narrowed:

      delocalisation  how far the pi system actually reaches. Path length and
                      system size are both used, size under a square root,
                      because pendant aryls that twist out of plane add atoms
                      without adding conjugation -- rubrene and triphenylamine
                      are large and blue.
      charge transfer only when a donor AND an acceptor are present. A strong
                      acceptor on its own is not a CT band.
      polymethine     cyanines sit outside the push-pull picture and need
                      their own term.

    Thresholds are fitted to the reference dyes in CALIBRATION below, which is
    what the test suite checks. The output stays a band: turning this into
    nanometres would imply a precision it does not have.
    """
    path = conjugation["path"]
    size = conjugation["size"]

    # Both terms saturate. The gap converges towards the polymer limit, so the
    # twentieth conjugated atom is worth far less than the fifth; without
    # saturation a large, twisted, perfectly blue molecule such as rubrene or
    # a triphenylamine adduct outscores a compact NIR-II dye on bulk alone.
    score = 6.0 * (1.0 - math.exp(-path / 9.0)) + 3.0 * (1.0 - math.exp(-size / 22.0))

    # Charge transfer needs both ends. The acceptor dominates: moving from a
    # benzothiadiazole to a benzobisthiadiazole is the step that reaches
    # NIR-II, and it has to outweigh a couple of atoms of extra backbone.
    if da["donor_strength"] and da["acceptor_strength"]:
        score += 0.50 * da["acceptor_strength"] + 0.15 * da["donor_strength"]

    bridge = da.get("polymethine_length", 0)
    if bridge >= 3:
        score += 0.30 * (bridge - 2)

    score += _core_bonus(mol)

    if score < 4.85:
        band = "UV"
    elif score < 6.9:
        band = "visible"
    elif score < 10.8:
        band = "NIR-I"
    else:
        band = "NIR-II"

    return {
        "band": band,
        "score": round(score, 2),
        "confidence": "low",
        "basis": "2D structural proxy fitted to reference dyes; not a wavelength prediction",
    }


# Reference dyes whose emission band is not in question, used to fit the
# thresholds above and to catch a regression in the fragment vocabulary.
# `scaffolds.py` SMILES are reused where they are the same structure.
CALIBRATION: list[tuple[str, str, str]] = [
    ("benzene", "c1ccccc1", "UV"),
    ("naphthalene", "c1ccc2ccccc2c1", "UV"),
    # Anthracene emits 380-450 nm: violet-blue, visible. Bare 1,8-naphthalimide
    # emits near 390 nm and belongs in UV -- it is the 4-amino derivatives that
    # are green emitters. Both labels are the literature's, not a fit.
    ("anthracene", "c1ccc2cc3ccccc3cc2c1", "visible"),
    ("coumarin", "O=c1ccc2ccccc2o1", "UV"),
    ("perylene", "c1cc2cccc3c2c2c1cccc2c1cccc3c1", "visible"),
    ("naphthalimide", "O=C1NC(=O)c2cccc3cccc1c23", "UV"),
    ("BODIPY_core", "C1=CC=[N+]2[B-](F)(F)N3C=CC=C3C=C12", "visible"),
    ("perylenediimide", "O=C1NC(=O)c2ccc3c4ccc5c(=O)[nH]c(=O)c6ccc(c3c24)c5c61", "visible"),
    ("porphyrin", "c1cc2cc3ccc(cc4ccc(cc5ccc(cc1n2)[nH]5)n4)[nH]3", "visible"),
    ("rubrene_core", "c1ccc(-c2c3ccccc3c(-c3ccccc3)c3c2cccc3)cc1", "visible"),
    ("Cy5", "CC1(C)c2ccccc2N(C)/C1=C/C=C/C=C1\\N(C)c2ccccc2C1(C)C", "NIR-I"),
    ("Cy7", "CC1(C)c2ccccc2N(C)/C1=C/C=C/C=C/C=C1\\N(C)c2ccccc2C1(C)C", "NIR-I"),
    ("phthalocyanine", "c1ccc2c(c1)c1nc-2nc2[nH]c(nc3nc(nc4[nH]c(n1)c1ccccc41)c1ccccc31)c1ccccc21", "NIR-I"),
]


def calibration_report() -> list[dict]:
    """Band predicted versus band known, for every reference dye."""
    out = []
    for name, smiles, expected in CALIBRATION:
        prof = profile(smiles)
        got = prof.get("spectral_band", {}).get("band") if "error" not in prof else "PARSE FAIL"
        out.append(
            {
                "name": name,
                "expected": expected,
                "predicted": got,
                "score": prof.get("spectral_band", {}).get("score"),
                "match": got == expected,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Channel balance
# ---------------------------------------------------------------------------


def channel_balance(conjugation: dict, da: dict, isc: dict, mot: dict) -> dict:
    """
    Relative propensity for the three competing decay channels, on 0-1.

    These are RANKING scores for comparing candidates in one batch. They are
    not quantum yields and do not sum to 1 in any physical sense; the
    normalisation below only makes the three comparable to each other.
    """
    rigidity = 1.0 / (1.0 + 0.25 * mot["rotatable_bonds"])
    fused = min(mot["fused_rings"] / 4.0, 1.0)

    # Radiative: wants a big, rigid, planar chromophore and few escape routes.
    radiative = 0.0
    radiative += min(conjugation["path"] / 20.0, 1.0) * 0.5
    radiative += rigidity * 0.3
    radiative += fused * 0.2
    # The energy gap law: pushing emission deep into the NIR costs quantum yield.
    if conjugation["path"] > 28:
        radiative *= 0.75

    # ISC: heavy atoms, heavy-atom-free channels, twisted push-pull geometry.
    crossing = 0.0
    crossing += min(isc["heavy_atom_weight"] / 2.0, 1.0) * 0.45
    crossing += min(len(isc["heavy_atom_free_motifs"]) / 2.0, 1.0) * 0.3
    crossing += min(isc["biaryl_twists"] / 4.0, 1.0) * 0.25

    # Nonradiative: rotors, flexibility, strong charge transfer (TICT).
    thermal = 0.0
    thermal += min(mot["rotatable_bonds"] / 10.0, 1.0) * 0.45
    thermal += min(da["push_pull"] / 12.0, 1.0) * 0.35
    thermal += min(len(mot["aie_motifs"]) / 2.0, 1.0) * 0.2

    total = radiative + crossing + thermal or 1.0
    return {
        "radiative": round(radiative, 3),
        "intersystem_crossing": round(crossing, 3),
        "nonradiative": round(thermal, 3),
        "share": {
            "fluorescence": round(radiative / total, 3),
            "pdt": round(crossing / total, 3),
            "ptt_pai": round(thermal / total, 3),
        },
        "dominant": max(
            (("fluorescence", radiative), ("pdt", crossing), ("ptt_pai", thermal)), key=lambda kv: kv[1]
        )[0],
        "caveat": "ranking proxies, not quantum yields; the true balance needs Delta E_ST, SOC and measurement",
    }


def type_i_propensity(mol, da: dict) -> dict:
    """
    Does the structure favour the oxygen-independent Type I route (electron
    transfer to superoxide / hydroxyl) over Type II (energy transfer to
    singlet oxygen)? Type I is what survives a hypoxic tumour.

    The real criteria are an energy one (T1 below ~0.98 eV blocks energy
    transfer to O2) and a redox one (reduction potential more negative than
    the -0.33 V of O2/O2*-). Neither is available from the 2D graph, so this
    reports the structural signatures that the literature associates with
    each route and says plainly what is missing.
    """
    signatures = []
    cationic = Chem.GetFormalCharge(mol) > 0
    if cationic:
        signatures.append("cationic (pyridinium-type cationisation lowers Delta E_ST and favours electron transfer)")
    quinones = [a["name"] for a in da["acceptors"] if "quinone" in a["name"]]
    if quinones:
        signatures.append(f"quinone electron-transfer mediator present ({', '.join(quinones)})")
    if da["acceptor_strength"] >= 4:
        signatures.append("strong acceptor, extended conjugation lowers T1")
    if da["push_pull"] >= 9:
        signatures.append("strong intramolecular charge transfer")

    return {
        "structural_signatures": signatures,
        "leaning": "type-I-leaning" if len(signatures) >= 2 else "unresolved",
        "requires": [
            "T1 energy vs the ~0.98 eV singlet-oxygen threshold (TD-DFT)",
            "reduction potential vs -0.33 V for O2/O2*- (cyclic voltammetry)",
            "ESR spin trapping (DMPO/BMPO) and probe panel (DHR123, HPF vs SOSG, ABDA)",
        ],
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def profile(smiles: str) -> dict:
    """Full photophysical profile for one SMILES. Returns {'error': ...} on a bad parse."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"smiles": smiles, "error": "RDKit could not parse this SMILES"}

    conjugation = conjugated_system(mol)
    da = donor_acceptor(mol)
    isc = intersystem_crossing(mol)
    mot = motion(mol)

    return {
        "smiles": Chem.MolToSmiles(mol),
        "input_smiles": smiles,
        "conjugation": conjugation,
        "donor_acceptor": da,
        "intersystem_crossing": isc,
        "motion": mot,
        "biology": biology(mol),
        "spectral_band": spectral_band(mol, conjugation, da),
        "channel_balance": channel_balance(conjugation, da, isc, mot),
        "type_i": type_i_propensity(mol, da),
    }


# ---------------------------------------------------------------------------
# Rule gate
# ---------------------------------------------------------------------------


def gate(prof: dict, goal: dict) -> dict:
    """
    Hard structural checks against a GoalSpec. Each rule carries its reason so
    a rejection is readable rather than a bare score.
    """
    if "error" in prof:
        return {"pass": False, "checks": [{"rule": "parse", "pass": False, "detail": prof["error"]}]}

    targets = goal.get("targets", {})
    modalities = set(goal.get("modalities", []))
    checks: list[dict] = []

    def check(rule: str, ok: bool, detail: str, severity: str = "hard"):
        checks.append({"rule": rule, "pass": bool(ok), "detail": detail, "severity": severity})

    # A molecule with no appreciable pi system cannot do any of this.
    size = prof["conjugation"]["size"]
    check(
        "conjugated_system",
        size >= targets.get("min_sp2_network", 12),
        f"largest sp2 network is {size} atoms (need >= {targets.get('min_sp2_network', 12)})",
    )

    want_band = targets.get("band")
    if want_band:
        got = prof["spectral_band"]["band"]
        order = {b: i for i, b in enumerate(BANDS)}
        check(
            "spectral_band",
            order.get(got, 0) >= order.get(want_band, 0),
            f"structural proxy puts this in {got}; target is {want_band} or redder",
            severity="soft",
        )

    if "PDT" in modalities:
        routes = prof["intersystem_crossing"]["routes"]
        check(
            "isc_route",
            routes != ["no structural ISC route identified"],
            f"ISC routes: {'; '.join(routes)}",
        )
        if targets.get("ros_type") == "I":
            check(
                "type_i_signature",
                prof["type_i"]["leaning"] == "type-I-leaning",
                f"Type I signatures: {len(prof['type_i']['structural_signatures'])} found",
                severity="soft",
            )

    if "PTT" in modalities or "PAI" in modalities:
        check(
            "motion_channel",
            prof["motion"]["rotatable_bonds"] >= targets.get("min_rotors", 3),
            f"{prof['motion']['rotatable_bonds']} rotatable bonds available to dissipate energy as heat",
            severity="soft",
        )

    if "FLI" in modalities:
        check(
            "emissive_character",
            prof["channel_balance"]["share"]["fluorescence"] >= targets.get("min_fluorescence_share", 0.2),
            f"fluorescence share of the ranking proxy is {prof['channel_balance']['share']['fluorescence']}",
            severity="soft",
        )

    if goal.get("constraints", {}).get("aqueous"):
        bio = prof["biology"]
        soluble = bio["formal_charge"] != 0 or any(
            m["name"] in ("sulfonate", "carboxylate", "peg_chain") for m in bio["motifs"]
        )
        check(
            "aqueous_handle",
            soluble,
            "ionic or PEG solubilising group detected" if soluble else "no ionic or PEG solubilising group; expect formulation into nanoparticles",
            severity="soft",
        )

    if goal.get("constraints", {}).get("renal_clearance"):
        check(
            "renal_clearance",
            prof["biology"]["molecular_weight"] <= 1200,
            f"MW {prof['biology']['molecular_weight']}; sub-5.5 nm renal clearance needs a small, shielded scaffold",
            severity="soft",
        )

    hard_failures = [c for c in checks if c["severity"] == "hard" and not c["pass"]]
    soft_failures = [c for c in checks if c["severity"] == "soft" and not c["pass"]]
    return {
        "pass": not hard_failures,
        "hard_failures": len(hard_failures),
        "soft_failures": len(soft_failures),
        "checks": checks,
    }


def rank_terms(prof: dict, goal: dict) -> dict[str, float]:
    """Weighted contributions used for both ordering and the selection explanation."""
    if "error" in prof:
        return {}

    modalities = set(goal.get("modalities", []))
    share = prof["channel_balance"]["share"]

    # Reward the channels the goal actually wants, penalise the ones it does not.
    wanted = 0.0
    if "FLI" in modalities:
        wanted += share["fluorescence"]
    if "PDT" in modalities:
        wanted += share["pdt"]
    if {"PTT", "PAI"} & modalities:
        wanted += share["ptt_pai"]
    wanted /= max(len(modalities & {"FLI", "PDT", "PTT", "PAI"}), 1)

    band_order = {b: i for i, b in enumerate(BANDS)}
    want_band = goal.get("targets", {}).get("band", "visible")
    band_fit = 1.0 - min(abs(band_order.get(prof["spectral_band"]["band"], 1) - band_order.get(want_band, 1)) / 3.0, 1.0)

    conjugation_fit = min(prof["conjugation"]["path"] / 24.0, 1.0)
    penalties = 0.1 * gate(prof, goal)["soft_failures"]

    return {
        "target_channels": 0.45 * wanted,
        "spectral_band": 0.3 * band_fit,
        "conjugation": 0.25 * conjugation_fit,
        "unmet_preferences": -penalties,
    }


def rank_score(prof: dict, goal: dict) -> float:
    """Ordinal structural ranking score, not calibrated molecular performance."""
    return round(max(0.0, sum(rank_terms(prof, goal).values())), 4)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: photophysics.py <SMILES> [SMILES ...]", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps([profile(s) for s in sys.argv[1:]], indent=2, ensure_ascii=False))
