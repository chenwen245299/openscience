"""
Train the optional absorption / emission / PLQY models used by predictor.py.

The data is not bundled and is not downloaded automatically. Fetch one of the
public fluorescence datasets yourself and point this script at the CSV:

  ChemFluor   ~4,300 molecule-solvent pairs
              https://figshare.com/articles/dataset/ChemFluor/12110619
  Deep4Chem   20,236 pairs over 7,016 chromophores and 365 solvents
              https://figshare.com (search "Experimental database of optical
              properties of organic compounds")

Column names differ between releases, so pass them explicitly if the
auto-detection below misses:

    uv run --python 3.12 --with rdkit --with scikit-learn --with pandas \
        --no-project python train_model.py --data chemfluor.csv

Honest limits, repeated here because they travel with the model:
the public data is dilute-solution and concentrated in the UV to NIR-I. A
model fitted here ranks candidates in that regime. It does not predict NIR-II
emitters and it knows nothing about aggregates, which is the state a
theranostic agent actually works in.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

from predictor import MODEL_DIR, SOLVENTS, featurise

SMILES_COLUMNS = ("smiles", "SMILES", "Chromophore", "chromophore")
SOLVENT_COLUMNS = ("solvent", "Solvent", "solvent_name")
ABSORPTION_COLUMNS = ("absorption", "Absorption max (nm)", "abs_max", "Absorption/nm", "lambda_abs")
EMISSION_COLUMNS = ("emission", "Emission max (nm)", "em_max", "Emission/nm", "lambda_em")
PLQY_COLUMNS = ("plqy", "Quantum yield", "quantum_yield", "PLQY", "phi")


def pick(frame, candidates, label: str, required: bool = True) -> str | None:
    for name in candidates:
        if name in frame.columns:
            return name
    if required:
        raise SystemExit(
            f"Could not find the {label} column. Looked for {candidates}.\n"
            f"Columns present: {list(frame.columns)[:20]}"
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the optional photophysics predictor")
    parser.add_argument("--data", required=True, help="CSV with SMILES, solvent, absorption, emission, PLQY")
    parser.add_argument("--out", default=MODEL_DIR)
    parser.add_argument("--trees", type=int, default=400)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    import numpy as np
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    from sklearn.metrics import mean_absolute_error, roc_auc_score
    from sklearn.model_selection import train_test_split

    frame = pd.read_csv(args.data)
    smiles_col = pick(frame, SMILES_COLUMNS, "SMILES")
    solvent_col = pick(frame, SOLVENT_COLUMNS, "solvent", required=False)
    absorption_col = pick(frame, ABSORPTION_COLUMNS, "absorption")
    emission_col = pick(frame, EMISSION_COLUMNS, "emission")
    plqy_col = pick(frame, PLQY_COLUMNS, "PLQY", required=False)

    features, rows = [], []
    for _, row in frame.iterrows():
        solvent = str(row[solvent_col]).lower() if solvent_col else "water"
        if solvent not in SOLVENTS:
            solvent = "water"
        vector = featurise(str(row[smiles_col]), solvent)
        if vector is None:
            continue
        features.append(vector)
        rows.append(row)
    if not features:
        raise SystemExit("No rows featurised; check the SMILES column.")

    X = np.vstack(features)
    print(f"Featurised {len(features)}/{len(frame)} rows")

    os.makedirs(args.out, exist_ok=True)
    metadata: dict = {"rows": len(features), "source": os.path.basename(args.data)}

    for name, column, kind in (
        ("absorption", absorption_col, "regress"),
        ("emission", emission_col, "regress"),
        ("plqy", plqy_col, "classify"),
    ):
        if column is None:
            print(f"  skipping {name}: no column found")
            continue
        values = np.array([r[column] for r in rows], dtype=float)
        mask = ~np.isnan(values)
        if mask.sum() < 50:
            print(f"  skipping {name}: only {int(mask.sum())} usable rows")
            continue

        Xi, yi = X[mask], values[mask]
        if kind == "classify":
            # PLQY is modelled as a threshold rather than a regression: the
            # public data is too sparse and too skewed near zero for the
            # regression to mean anything.
            yi = (yi > 0.5).astype(int)
            if len(set(yi)) < 2:
                print(f"  skipping {name}: single class after thresholding")
                continue

        X_train, X_test, y_train, y_test = train_test_split(Xi, yi, test_size=0.1, random_state=args.seed)
        model = (
            RandomForestClassifier(n_estimators=args.trees, random_state=args.seed, n_jobs=-1)
            if kind == "classify"
            else RandomForestRegressor(n_estimators=args.trees, random_state=args.seed, n_jobs=-1)
        )
        model.fit(X_train, y_train)

        if kind == "classify":
            score = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])
            metadata["plqy_auc"] = round(float(score), 3)
            print(f"  {name}: ROC-AUC {score:.3f} on {len(y_test)} held out")
        else:
            score = mean_absolute_error(y_test, model.predict(X_test))
            metadata[f"{name}_mae"] = round(float(score), 1)
            print(f"  {name}: MAE {score:.1f} nm on {len(y_test)} held out")
            if name == "emission":
                metadata["emission_range"] = [round(float(yi.min()), 1), round(float(yi.max()), 1)]

        with open(os.path.join(args.out, f"{name}.pkl"), "wb") as handle:
            pickle.dump(model, handle)

    with open(os.path.join(args.out, "metadata.json"), "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2)

    print(f"\nModels written to {args.out}")
    print("predictor.available() will now return True; the pipeline will use it for ranking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
