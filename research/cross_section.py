"""Cross-sectional (beta-neutral) prediction — the answerable version.

Why this and not the absolute model
-----------------------------------
`run_v5.py` shows that on this universe a long trade taken on EVERY bar earns
+0.22R, which grades B+ on the project rubric all by itself. That number is
market beta over a 2012-2026 sample, not skill, and it swamps everything: an
absolute up/down model has to beat +0.22R before it has demonstrated anything,
and neither the linear nor the boosted model gets there (AUC ~0.49).

Ranking the universe against ITSELF removes that. On any given day the top and
bottom baskets both eat the same market move, so the spread between them is
what is left after beta. That is the only place a daily-OHLCV model has a
realistic chance, and it is measurable with the sample we have.

Scored as: lift of the top basket over the same day's universe mean, and the
top-minus-bottom spread, both in R, with date-level block-bootstrap intervals.

    python research/cross_section.py --top-k 10 --model gbm
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluate import auc, date_block_bootstrap  # noqa: E402
from models import Calibrator, LogitL2, StumpGBM  # noqa: E402
from panel import DEFAULT_TICKERS, FEATURES, build_panel  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cross_sectionalise(panel: pd.DataFrame, min_names: int = 20
                       ) -> pd.DataFrame:
    """Z-score every feature within its date and add the relative label.

    Cross-sectional standardisation is what makes the ranking problem
    well-posed: it strips the common (market) component out of each feature so
    the model sees "rich vs the rest of the tape today", not "rich vs 2014".
    """
    p = panel.copy()
    cnt = p.groupby("Date")["ticker"].transform("size")
    p = p[cnt >= min_names].copy()
    g = p.groupby("Date")
    for c in FEATURES:
        mu = g[c].transform("mean")
        sd = g[c].transform("std").replace(0, np.nan)
        p[f"cs_{c}"] = ((p[c] - mu) / sd).fillna(0.0).clip(-6, 6)
    gr = p.groupby("Date")["y_R"]
    p["y_R_rel"] = p["y_R"] - gr.transform("mean")
    p["y_top"] = (p["y_R"] > gr.transform("median")).astype(float)
    return p.reset_index(drop=True)


CS_FEATURES = [f"cs_{c}" for c in FEATURES]


def walk_forward_cs(p: pd.DataFrame, model_name: str, horizon: int,
                    n_folds: int, min_train_years: float = 4.0,
                    calib_frac: float = 0.2, seed: int = 0) -> pd.DataFrame:
    d = p["Date"].to_numpy()
    uniq = np.unique(d)
    start_i = int(np.searchsorted(
        uniq, uniq[0] + np.timedelta64(int(min_train_years * 365), "D")))
    edges = np.linspace(start_i, len(uniq), n_folds + 1).astype(int)
    x = p[CS_FEATURES].to_numpy(dtype=float)
    y = p["y_top"].to_numpy(dtype=float)
    out = []
    for k in range(n_folds):
        te_lo, te_hi = edges[k], edges[k + 1]
        if te_hi - te_lo < 5:
            continue
        t0 = uniq[te_lo]
        cut = t0 - np.timedelta64(int(horizon * 1.6) + 3, "D")
        tr = d < cut
        te = (d >= t0) & (d < (uniq[te_hi] if te_hi < len(uniq)
                               else uniq[-1] + np.timedelta64(1, "D")))
        if tr.sum() < 5000 or te.sum() < 200:
            continue
        tr_idx = np.flatnonzero(tr)
        n_cal = int(len(tr_idx) * calib_frac)
        fit_idx, cal_idx = tr_idx[:-n_cal], tr_idx[-n_cal:]
        m = StumpGBM(seed=seed) if model_name == "gbm" else LogitL2(lam=5.0)
        m.fit(x[fit_idx], y[fit_idx])
        cal = Calibrator().fit(m.predict_proba(x[cal_idx]), y[cal_idx])
        chunk = p.loc[te, ["Date", "ticker", "y_R", "y_R_rel", "y_top"]].copy()
        chunk["p"] = cal.transform(m.predict_proba(x[te]))
        chunk["fold"] = k
        out.append(chunk)
    if not out:
        raise RuntimeError("no usable folds")
    return pd.concat(out, ignore_index=True)


def basket_daily(res: pd.DataFrame, top_k: int) -> pd.DataFrame:
    """Per-date basket returns in R: top-K, bottom-K, and universe mean."""
    rows = []
    for dt, g in res.groupby("Date"):
        if len(g) < 2 * top_k:
            continue
        s = g.sort_values("p", ascending=False)
        rows.append({"Date": dt, "n": len(g),
                     "top": float(s.head(top_k)["y_R"].mean()),
                     "bot": float(s.tail(top_k)["y_R"].mean()),
                     "uni": float(g["y_R"].mean())})
    b = pd.DataFrame(rows)
    b["lift"] = b["top"] - b["uni"]
    b["spread"] = b["top"] - b["bot"]
    return b


def ci(b: pd.DataFrame, col: str, n_boot: int = 3000) -> tuple:
    return date_block_bootstrap(b["Date"].to_numpy(), b[col].to_numpy(),
                                n_boot)


def random_baseline(res: pd.DataFrame, top_k: int, n_rep: int = 200,
                    seed: int = 0) -> dict:
    """Same basket machinery, random ranking. This is the number to beat."""
    rng = np.random.default_rng(seed)
    lifts, spreads = [], []
    for _ in range(n_rep):
        r = res.copy()
        r["p"] = rng.random(len(r))
        b = basket_daily(r, top_k)
        lifts.append(b["lift"].mean())
        spreads.append(b["spread"].mean())
    return {"lift_mean": float(np.mean(lifts)),
            "lift_sd": float(np.std(lifts)),
            "lift_p95": float(np.percentile(lifts, 95)),
            "spread_mean": float(np.mean(spreads)),
            "spread_sd": float(np.std(spreads)),
            "spread_p95": float(np.percentile(spreads, 95))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gbm", choices=["gbm", "logit"])
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--tp-r", type=float, default=2.0)
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--universe", default="")
    ap.add_argument("--out", default="research/cs_results.json")
    a = ap.parse_args()

    if a.universe:
        u = pd.read_csv(os.path.join(ROOT, a.universe))
        tickers = sorted(set(u["ticker"].dropna().astype(str)))
    else:
        tickers = DEFAULT_TICKERS
    avail = [t for t in tickers if os.path.exists(
        os.path.join(ROOT, "research", "_cache", f"{t}_1d.csv"))]
    print(f"universe: {len(avail)} tickers cached (of {len(tickers)} asked)")

    panel = build_panel(avail, 1, a.horizon, a.stop_atr, a.tp_r)
    p = cross_sectionalise(panel)
    print(f"panel {len(p):,} rows, {p['Date'].nunique():,} dates, "
          f"median names/day {int(p.groupby('Date').size().median())}, "
          f"{p['Date'].min().date()} -> {p['Date'].max().date()}")

    res = walk_forward_cs(p, a.model, a.horizon, a.folds)
    b = basket_daily(res, a.top_k)
    rnd = random_baseline(res, a.top_k)

    print(f"\n=== CROSS-SECTIONAL {a.model.upper()} | top-{a.top_k} of "
          f"~{int(res.groupby('Date').size().median())} names ===")
    print(f"out-of-sample: {len(res):,} rows, {len(b):,} trading days, "
          f"{res['Date'].min().date()} -> {res['Date'].max().date()}")
    print(f"rank AUC (in top half): {auc(res['y_top'].to_numpy(), res['p'].to_numpy()):.4f}")

    rows = []
    for name, col in (("top-K minus universe mean (lift)", "lift"),
                      ("top-K minus bottom-K (spread)", "spread")):
        lo, hi = ci(b, col)
        rnd_mu = rnd[f"{col}_mean"]
        rnd_p95 = rnd[f"{col}_p95"]
        rows.append({"metric": name, "mean_R": round(b[col].mean(), 4),
                     "CI95": f"[{lo:+.4f}, {hi:+.4f}]",
                     "random_mean": round(rnd_mu, 4),
                     "random_p95": round(rnd_p95, 4),
                     "beats_random": bool(lo > rnd_p95)})
    rows.append({"metric": "top-K absolute", "mean_R": round(b["top"].mean(), 4),
                 "CI95": "", "random_mean": round(b["uni"].mean(), 4),
                 "random_p95": "", "beats_random": ""})
    rows.append({"metric": "universe mean (pure beta)",
                 "mean_R": round(b["uni"].mean(), 4), "CI95": "",
                 "random_mean": "", "random_p95": "", "beats_random": ""})
    t = pd.DataFrame(rows)
    print("\n" + t.to_string(index=False))

    yearly = b.assign(yr=b["Date"].dt.year).groupby("yr")[
        ["top", "bot", "uni", "lift", "spread"]].mean().round(3)
    print("\nby year (mean R):")
    print(yearly.to_string())

    out = {"model": a.model, "top_k": a.top_k, "n_days": len(b),
           "table": t.to_dict(orient="records"),
           "yearly": yearly.reset_index().to_dict(orient="records"),
           "random_baseline": rnd}
    with open(os.path.join(ROOT, a.out), "w") as f:
        json.dump(out, f, indent=1, default=str)
    res.to_csv(os.path.join(ROOT, "research", "cs_oos_predictions.csv"),
               index=False)
    print(f"\nwrote {a.out} and research/cs_oos_predictions.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
