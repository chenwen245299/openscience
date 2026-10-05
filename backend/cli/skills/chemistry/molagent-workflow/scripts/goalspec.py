"""
Turn a design question into a GoalSpec.

The GoalSpec is the first artifact in the chain and every later stage reads
it: it fixes the modalities, the optical window, the ROS route and the
constraints, so that step 2 ranks and step 3 generates against the same
target rather than against whatever the model remembered.

Parsing is deterministic keyword matching over Chinese and English, with no
model call, so the same question always produces the same spec. Anything it
cannot infer is left absent rather than guessed, and `--show` prints the spec
for the agent to correct with explicit flags before the pipeline runs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from _common import utcnow, validate_output_dir, write_artifact

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

MODALITIES = {
    "FLI": [
        "fluorescence imaging", "fluorescence", "fluorescent", "fluorophore", "emission imaging",
        "荧光成像", "荧光", "发光", "成像剂",
    ],
    "PAI": ["photoacoustic", "optoacoustic", "光声", "光声成像"],
    "PDT": [
        "photodynamic", "pdt", "singlet oxygen", "reactive oxygen", "ros", "photosensitiser", "photosensitizer",
        "光动力", "活性氧", "单线态氧", "光敏剂", "超氧",
    ],
    "PTT": [
        "photothermal", "ptt", "hyperthermia", "photothermal conversion",
        "光热", "热疗", "光热转换",
    ],
}

BANDS = [
    ("NIR-II", ["nir-ii", "nir ii", "nir2", "second near-infrared", "1000-1700", "swir", "近红外二区", "二区", "近红外 II"]),
    ("NIR-I", ["nir-i", "nir i", "first near-infrared", "near-infrared", "near infrared", "nir", "近红外一区", "一区", "近红外"]),
    ("visible", ["visible", "可见光", "可见区"]),
    ("UV", ["ultraviolet", "uv-excited", "紫外"]),
]

CONSTRAINTS = {
    "hypoxia_tolerant": ["hypoxia", "hypoxic", "anoxic", "low oxygen", "乏氧", "缺氧", "低氧"],
    "aqueous": ["water-soluble", "water soluble", "aqueous", "physiological", "水溶", "水相", "生理"],
    "aggregate": ["nanoparticle", "nanoparticles", "aggregate", "aggregation", "aie", "nanoaggregate", "self-assembl",
                  "纳米", "聚集", "自组装"],
    "renal_clearance": ["renal clearance", "renally cleared", "excretion", "肾清除", "肾代谢", "排泄"],
    "two_photon": ["two-photon", "two photon", "双光子"],
    "activatable": ["activatable", "turn-on", "responsive", "stimuli", "可激活", "响应", "点亮"],
    "image_guided_surgery": ["image-guided surgery", "intraoperative", "fluorescence-guided", "术中", "导航手术"],
}

ROS_TYPE_I = ["type i", "type-i", "type 1", "superoxide", "hydroxyl radical", "electron transfer",
              "i型", "一型", "超氧", "羟基自由基"]
ROS_TYPE_II = ["type ii", "type-ii", "type 2", "singlet oxygen", "energy transfer",
               "ii型", "二型", "单线态氧"]

# Minimum conjugated network per band. 12 is the threshold SyntheFluor-RL used
# as a fluorescence prerequisite; redder emission needs more.
MIN_SP2 = {"UV": 10, "visible": 12, "NIR-I": 18, "NIR-II": 24}


def _hits(text: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n in text]


def parse(question: str) -> dict:
    """Derive a GoalSpec from a free-text design question."""
    text = question.lower()

    modalities = [name for name, needles in MODALITIES.items() if _hits(text, needles)]
    # A theranostic request with no modality named still needs something to aim
    # at; imaging plus the therapy the words imply is the safest default.
    if not modalities:
        modalities = ["FLI"]

    band = next((name for name, needles in BANDS if _hits(text, needles)), None)

    constraints = {key: bool(_hits(text, needles)) for key, needles in CONSTRAINTS.items()}
    constraints = {k: v for k, v in constraints.items() if v}

    # Hypoxia is the clinical reason Type I exists, so infer it when the
    # question asks for one and says nothing about the other.
    ros_type = None
    if _hits(text, ROS_TYPE_I):
        ros_type = "I"
    elif _hits(text, ROS_TYPE_II):
        ros_type = "II"
    elif constraints.get("hypoxia_tolerant") and "PDT" in modalities:
        ros_type = "I"

    targets: dict = {}
    if band:
        targets["band"] = band
        targets["min_sp2_network"] = MIN_SP2[band]
    if ros_type:
        targets["ros_type"] = ros_type
    if "PTT" in modalities or "PAI" in modalities:
        targets["min_rotors"] = 3
    if "FLI" in modalities and len(modalities) > 1:
        # A multimodal agent cannot put everything into emission; ask only for
        # a usable share rather than a maximum.
        targets["min_fluorescence_share"] = 0.15

    return {
        "question": question,
        "modalities": modalities,
        "band": band,
        "ros_type": ros_type,
        "targets": targets,
        "constraints": constraints,
        "keywords": keywords(question, modalities, band, constraints, ros_type),
        "unresolved": unresolved(band, modalities, ros_type),
        "created": utcnow(),
    }


def keywords(question: str, modalities: list[str], band: str | None, constraints: dict, ros_type: str | None) -> dict:
    """
    Search terms for step 1. Scholarly APIs are English-indexed, so the
    canonical English concepts are carried separately from the raw question.
    """
    canonical = {
        "FLI": "fluorescence imaging",
        "PAI": "photoacoustic imaging",
        "PDT": "photodynamic therapy",
        "PTT": "photothermal therapy",
    }
    # Ordered most distinctive first: a query led by "fluorescence imaging"
    # returns the whole field, one led by "NIR-II fluorophore" returns the
    # papers this design actually needs.
    concepts: list[str] = []
    if band in ("NIR-I", "NIR-II"):
        concepts.append("NIR-II fluorophore" if band == "NIR-II" else "near-infrared fluorophore")
    if ros_type == "I":
        concepts.append("type I photosensitizer")
    if constraints.get("aggregate"):
        concepts.append("aggregation-induced emission")
    if len(modalities) > 1:
        concepts.append("phototheranostics")
    if constraints.get("hypoxia_tolerant"):
        concepts.append("tumor hypoxia")
    if constraints.get("two_photon"):
        concepts.append("two-photon absorption")
    if constraints.get("activatable"):
        concepts.append("activatable probe")
    concepts.extend(canonical[m] for m in modalities if m in canonical)

    # Latin-script tokens from the question are usually the chemistry terms
    # worth keeping (BODIPY, BBTD, AIE); CJK is dropped because the indexes
    # will not match it.
    raw = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", question) if t.lower() not in STOPWORDS]

    return {
        "concepts": concepts or ["phototheranostics"],
        "from_question": sorted(set(raw))[:12],
        "queries": build_queries(concepts, raw),
    }


STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "design", "molecule", "molecules", "using", "can", "how",
    "what", "which", "please", "help", "need", "want", "make", "new", "good", "high", "low",
}


def build_queries(concepts: list[str], raw: list[str]) -> list[str]:
    """
    Two or three targeted queries beat one broad one, which is also what the
    literature tool's own contract advises.
    """
    if not concepts:
        return ["phototheranostic agent molecular design"]
    queries = []
    head = concepts[0]
    queries.append(f"{head} molecular design strategy")
    if len(concepts) > 1:
        queries.append(" ".join(concepts[:3]))
    queries.append(f"{head} structure-property relationship review")
    chem = [t for t in raw if t.isupper() or len(t) > 6][:2]
    if chem:
        queries.append(f"{' '.join(chem)} {head}")
    return queries[:4]


def unresolved(band: str | None, modalities: list[str], ros_type: str | None) -> list[str]:
    """What the question did not say. Printed so it can be fixed with a flag."""
    gaps = []
    if not band:
        gaps.append("optical window not stated (--band UV|visible|NIR-I|NIR-II)")
    if modalities == ["FLI"]:
        gaps.append("no therapeutic modality named; assuming imaging only (--modalities FLI,PDT,PTT)")
    if "PDT" in modalities and not ros_type:
        gaps.append("ROS route not stated (--ros-type I|II)")
    return gaps


def apply_overrides(spec: dict, args) -> dict:
    """Explicit flags win over anything inferred from the text."""
    if args.modalities:
        spec["modalities"] = [m.strip().upper() for m in args.modalities.split(",") if m.strip()]
    if args.band:
        spec["band"] = args.band
        spec["targets"]["band"] = args.band
        spec["targets"]["min_sp2_network"] = MIN_SP2[args.band]
    if args.ros_type:
        spec["ros_type"] = args.ros_type
        spec["targets"]["ros_type"] = args.ros_type
    if args.aqueous:
        spec["constraints"]["aqueous"] = True
    spec["unresolved"] = unresolved(spec["band"], spec["modalities"], spec["ros_type"])
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse a design question into a GoalSpec")
    parser.add_argument("--question", required=True, help="The design question, in Chinese or English")
    parser.add_argument("--output-dir", default="./molagent_results")
    parser.add_argument("--modalities", help="Override, comma separated: FLI,PAI,PDT,PTT")
    parser.add_argument("--band", choices=["UV", "visible", "NIR-I", "NIR-II"], help="Override the optical window")
    parser.add_argument("--ros-type", choices=["I", "II"], dest="ros_type", help="Override the ROS route")
    parser.add_argument("--aqueous", action="store_true", help="Require an aqueous-compatible design")
    parser.add_argument("--show", action="store_true", help="Print the spec without writing it")
    args = parser.parse_args()

    spec = apply_overrides(parse(args.question), args)

    if args.show:
        print(json.dumps(spec, indent=2, ensure_ascii=False))
        return 0

    output_dir = validate_output_dir(args.output_dir)
    target = write_artifact(output_dir, "goal", spec)
    print(f"GoalSpec written to {target}")
    print(f"  modalities: {', '.join(spec['modalities'])}")
    print(f"  band:       {spec['band'] or 'unspecified'}")
    print(f"  ROS route:  {spec['ros_type'] or 'unspecified'}")
    if spec["constraints"]:
        print(f"  constraints: {', '.join(sorted(spec['constraints']))}")
    for gap in spec["unresolved"]:
        print(f"  UNRESOLVED: {gap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
