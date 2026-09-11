"""Does the evaluation pipeline actually work?

Every model tried so far lands near AUC 0.49 out of sample. That is either an
honest "no signal here" or a broken harness, and the two are indistinguishable
from the number alone. This file separates them with three checks:

1. IN-SAMPLE vs OUT-OF-SAMPLE AUC. A working learner must fit the training set
   noticeably better than chance. If train AUC is also ~0.50 the model or the
   feature matrix is broken, not the market.
2. PLANTED SIGNAL. Add one synthetic feature that genuinely predicts the label
   at a known strength. A working pipeline must recover it out of sample, and
   the recovered AUC must scale with the planted strength.
3. SHUFFLED LABELS. Break the feature-label link on purpose. A working pipeline
   must then report AUC ~0.50 and no expectancy lift. If it still shows an edge
   there is leakage.

Run:  python research/sanity_check.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluate import auc  # noqa: E402
from models import Calibrator, LogitL2, StumpGBM  # noqa: E402
from panel import DEFAULT_TICKERS, FEATURES, build_panel  # noqa: E402


def split_fit_eval(p: pd.DataFrame, feats: list[str], y_col: str,
                   model_name: str = "gbm", horizon: int = 20,
                   frac: float = 0.65, seed: int = 0) -> dict:
    """One clean purged split; returns train and test AUC for the same model."""
    d = p["Date"].to_numpy()
    uniq = np.unique(d)
    t0 = uniq[int(len(uniq) * frac)]
    cut = t0 - np.timedelta64(int(horizon * 1.6) + 3, "D")
    tr, te = d < cut, d >= t0
    x = p[feats].to_numpy(dtype=float)
    y = p[y_col].to_numpy(dtype=float)
    leaf = max(25, min(200, int(tr.sum() * 0.05)))
    m = (StumpGBM(seed=seed, min_leaf=leaf) if model_name == "gbm"
         else LogitL2(lam=5.0))
    m.fit(x[tr], y[tr])
    ptr = m.predict_proba(x[tr])
    pte = m.predict_proba(x[te])
    return {"n_train": int(tr.sum()), "n_test": int(te.sum()),
            "auc_train": auc(y[tr], ptr), "auc_test": auc(y[te], pte)}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="AAPL")
    a = ap.parse_args()
    tk = ([t.strip().upper() for t in a.tickers.split(",") if t.strip()]
          if a.tickers != "ALL" else DEFAULT_TICKERS)
    print(f"building panel ({len(tk)} ticker(s): {','.join(tk[:6])}"
          f"{'...' if len(tk) > 6 else ''}, 20d horizon, 2R target)...")
    p = build_panel(tk, 1, 20, 1.5, 2.0)
    print(f"panel {len(p):,} rows  base rate {p['y_win'].mean():.3f}\n")
    rng = np.random.default_rng(7)
    y = p["y_win"].to_numpy(dtype=float)

    print("=" * 74)
    print("CHECK 1  real features, real labels  (train vs test AUC)")
    print("=" * 74)
    for mdl in ("logit", "gbm"):
        r = split_fit_eval(p, FEATURES, "y_win", mdl)
        print(f"  {mdl:<6} train AUC {r['auc_train']:.4f}   "
              f"test AUC {r['auc_test']:.4f}   "
              f"(n_train {r['n_train']:,}  n_test {r['n_test']:,})")
    print("  reading: train >> 0.50 with test ~0.50 means the learner works\n"
          "           and the signal does not generalise. Both ~0.50 means\n"
          "           the harness itself is broken.\n")

    print("=" * 74)
    print("CHECK 2  planted signal of known strength (must be recovered)")
    print("=" * 74)
    for strength in (0.02, 0.05, 0.10, 0.25):
        q = p.copy()
        # a feature that is the label plus noise: informative by construction,
        # available at predict time, and independent of everything else.
        noise = rng.normal(0, 1, len(q))
        q["planted"] = strength * (y - y.mean()) / y.std() + noise
        r = split_fit_eval(q, FEATURES + ["planted"], "y_win", "gbm")
        print(f"  strength {strength:>5.2f}  ->  train AUC {r['auc_train']:.4f}"
              f"   test AUC {r['auc_test']:.4f}")
    print("  reading: test AUC must rise monotonically with strength. If it\n"
          "           stays flat at 0.50 the pipeline cannot see signal at all.\n")

    print("=" * 74)
    print("CHECK 3  shuffled labels (must collapse to chance)")
    print("=" * 74)
    q = p.copy()
    q["y_shuf"] = rng.permutation(y)
    r = split_fit_eval(q, FEATURES, "y_shuf", "gbm")
    print(f"  gbm    train AUC {r['auc_train']:.4f}   test AUC {r['auc_test']:.4f}")
    print("  reading: test AUC materially above 0.50 here would prove leakage.\n")

    print("=" * 74)
    print("CHECK 4  label sanity (is y_R arithmetic self-consistent?)")
    print("=" * 74)
    r = p["y_R"].to_numpy()
    win = p["y_win"].to_numpy().astype(bool)
    print(f"  distinct y_R values at the barriers: "
          f"stop={np.isclose(r, -1.0).mean():.3f} of rows, "
          f"target={np.isclose(r, 2.0).mean():.3f}, "
          f"timeout={(~np.isclose(r, -1.0) & ~np.isclose(r, 2.0)).mean():.3f}")
    print(f"  mean R  {r.mean():+.4f}   win rate {win.mean():.4f}")
    implied = win.mean() * 2.0 - (1 - win.mean()) * 1.0
    print(f"  pure-barrier implied expectancy {implied:+.4f} "
          f"(differs from mean R only because of timeouts)")
    print(f"  timeout rows mean R {r[~np.isclose(r, -1.0) & ~np.isclose(r, 2.0)].mean():+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
