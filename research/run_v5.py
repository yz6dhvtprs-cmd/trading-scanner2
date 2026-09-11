"""v5 predictor: calibrated triple-barrier probability, graded on expectancy.

    python research/run_v5.py                       # default long study
    python research/run_v5.py --model logit
    python research/run_v5.py --side -1             # shorts
    python research/run_v5.py --horizon 10 --tp-r 2.0 --stop-atr 1.5
    python research/run_v5.py --holdout-tickers AMD,CRM,NFLX,ADBE,INTC,CSCO

The old track asked "what price is the next swing?" and scored a 1% band.
This asks "should this bar be traded, and what is the expected R?" — the
question grading.md was already written to answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluate import (calibration_table, grade, null_baselines,  # noqa: E402
                      score_threshold, shift_permutation_test, walk_forward,
                      auc, brier)
from panel import DEFAULT_TICKERS, build_panel  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def report(res: pd.DataFrame, args, tag: str) -> dict:
    y = res["y_win"].to_numpy()
    p = res["p"].to_numpy()
    print(f"\n=== {tag} ===")
    print(f"out-of-sample rows : {len(res):,}   folds: {res['fold'].nunique()}"
          f"   dates: {res['Date'].nunique():,}"
          f"   window: {res['Date'].min().date()} -> {res['Date'].max().date()}")
    print(f"discrimination     : AUC {auc(y, p):.4f}   Brier {brier(y, p):.4f}"
          f"   base rate {y.mean():.3f}")

    rows = null_baselines(res)
    for q in (0.5, 0.7, 0.8, 0.9, 0.95):
        thr = float(np.quantile(p, q))
        rows.append(score_threshold(res, thr, f"model p >= q{int(q*100)}"))
    t = pd.DataFrame(rows)
    t["grade"] = [grade(e, f, n) for e, f, n in
                  zip(t.expectancy_R, t.pf, t.trades)]
    t["CI95"] = [f"[{a:+.3f}, {b:+.3f}]" for a, b in zip(t.ci_lo, t.ci_hi)]
    show = t[["rule", "trades", "trades_pct", "win_rate", "expectancy_R",
              "CI95", "pf", "grade"]]
    print("\n" + show.to_string(index=False))

    print("\ncalibration (out of sample):")
    print(calibration_table(res).to_string(index=False))

    qs = (0.5, 0.7, 0.8, 0.9, 0.95)
    perm = pd.DataFrame([shift_permutation_test(res, q) for q in qs])
    # Bonferroni over the thresholds we looked at, so the best one is not
    # rewarded simply for being the best of five.
    perm["p_adj"] = np.minimum(1.0, perm["p_value"] * len(qs))
    perm["significant"] = perm["p_adj"] < 0.05
    print("\npermutation test (circular shift, selection size held fixed):")
    print(perm.to_string(index=False))

    always = float(t[t.rule.str.startswith("take every")].iloc[0].expectancy_R)
    best_i = int(perm["observed_R"].idxmax())
    b = perm.loc[best_i]
    any_sig = bool(perm["significant"].any())
    print(f"\nalways-trade (pure exposure) baseline : {always:+.4f}R")
    print(f"best selective rule                   : {b.observed_R:+.4f}R "
          f"at q{int(b['q']*100)} on {int(b.k_selected):,} trades")
    print(f"same-size no-information null         : {b.null_mean_R:+.4f}R "
          f"(95th pct {b.null_p95_R:+.4f})")
    print(f"p = {b.p_value:.4f}, Bonferroni-adjusted p = {b.p_adj:.4f}  "
          f"-> selection skill demonstrated: {any_sig}")
    return {"auc": auc(y, p), "brier": brier(y, p), "base_rate": float(y.mean()),
            "n_oos": int(len(res)), "always_trade_R": always,
            "table": t.to_dict(orient="records"),
            "permutation": perm.to_dict(orient="records"),
            "beats_null": any_sig}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gbm", choices=["gbm", "logit"])
    ap.add_argument("--side", type=int, default=1, choices=[1, -1])
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--tp-r", type=float, default=2.0)
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--min-train-rows", type=int, default=0,
                    help="0 = auto (scales with universe size)")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--holdout-tickers", default="",
                    help="never trained on; replication check")
    ap.add_argument("--out", default="research/v5_results.json")
    a = ap.parse_args()

    hold = [t.strip().upper() for t in a.holdout_tickers.split(",") if t.strip()]
    train_t = [t.strip().upper() for t in a.tickers.split(",")
               if t.strip() and t.strip().upper() not in hold]

    print(f"building panel: {len(train_t)} tickers, side={a.side:+d}, "
          f"horizon={a.horizon}, stop={a.stop_atr}xATR, target={a.tp_r}R")
    panel = build_panel(train_t, a.side, a.horizon, a.stop_atr, a.tp_r)
    print(f"panel rows: {len(panel):,}  "
          f"{panel['Date'].min().date()} -> {panel['Date'].max().date()}  "
          f"base win rate: {panel['y_win'].mean():.3f}  "
          f"mean R if all taken: {panel['y_R'].mean():+.4f}")

    mtr = a.min_train_rows or max(400, min(2000, len(panel) // 8))
    mte = 50 if len(train_t) > 3 else 20
    res = walk_forward(panel, a.model, a.horizon, a.folds,
                       min_train_rows=mtr, min_test_rows=mte)
    summary = report(res, a, f"{a.model.upper()} | in-universe walk-forward")

    if hold:
        hp = build_panel(hold, a.side, a.horizon, a.stop_atr, a.tp_r)
        combined = pd.concat([panel, hp], ignore_index=True).sort_values(
            ["Date", "ticker"]).reset_index(drop=True)
        hres = walk_forward(combined, a.model, a.horizon, a.folds)
        hres = hres[hres.ticker.isin(hold)]
        summary["holdout"] = report(
            hres, a, f"{a.model.upper()} | HELD-OUT TICKERS {','.join(hold)}")

    res.to_csv(os.path.join(ROOT, "research", "v5_oos_predictions.csv"),
               index=False)
    with open(os.path.join(ROOT, a.out), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(f"\nwrote research/v5_oos_predictions.csv and {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
