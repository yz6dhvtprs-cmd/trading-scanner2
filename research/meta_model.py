"""Meta-model: can we predict IN ADVANCE which prediction points will be right?

The question this answers
-------------------------
"Historically some predictions landed close to reality. Can we identify what
those moments looked like, and recognise them next time?"

That is a legitimate and well-known technique (meta-labelling): leave the
primary predictor alone and train a SECOND model whose target is "will the
primary be correct at this bar?". If it works you gate the primary on it and
only act when the meta-model is confident.

The trap it must avoid
----------------------
Looking at the historic hits and reading off what they had in common is
selection on the outcome. With 31 features and a few dozen hits you will
always find a separating rule, and it will be noise. So the meta-model here is
trained and scored under the same purged walk-forward discipline as everything
else, and it is judged against three things, not against zero:

  1. the base rate (does confidence actually concentrate the hits?)
  2. a VOLATILITY-ONLY meta-model (one feature: ATR/close). This is the
     control that matters. Hits cluster where the realised move was small, and
     small moves are forecastable because volatility clusters. If the full
     model cannot beat the one-feature volatility model, then the "identifier"
     we found is just "the tape is about to be quiet" wearing a costume.
  3. a circular-shift permutation null holding the selection size fixed.

And finally the economic test: gating only matters if it converts into R.

    python research/meta_model.py --ticker AAPL
    python research/meta_model.py --ticker AAPL --primary band
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_swings as bs  # noqa: E402
from evaluate import auc, date_block_bootstrap  # noqa: E402
from models import Calibrator, LogitL2, StumpGBM  # noqa: E402
from panel import FEATURES, _context, features_for, load  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOL_ONLY = ["atr_pct"]


def prediction_points(ticker: str, period: str, version: str) -> pd.DataFrame:
    """Every as-of point the swing harness scores, with hit/miss for the
    primary predictor AND for the ATR-band null, plus what actually happened."""
    df = bs.load_daily(ticker, period)
    full = bs.fractal_swings(bs._with_dates(df), bs.K)
    pts = sorted({min(int(b) + bs.K, len(df) - 1)
                  for b in full["bar"].to_numpy()
                  if int(b) + bs.K >= 60 and int(b) + bs.K < len(df) - bs.K - 1})
    rows = []
    for j in [p for p in pts if p >= 60]:
        truth = bs.next_swing_truth(df, j)
        if truth is None:
            continue
        p = bs.predict(df, j, version)
        c = float(df["Close"].iloc[j])
        a = float(df["atr14"].iloc[j])
        if not np.isfinite(a) or a <= 0:
            continue
        known = full[full["bar"] + bs.K <= j]
        prev_side = known.iloc[-1]["side"] if len(known) else "low"
        band_dir = "down" if prev_side == "high" else "up"
        band_lvl = c + a if band_dir == "up" else c - a

        want_up = truth["side"] == "high"
        tl = truth["level"]

        def hit(direction, lvl):
            return bool(((direction == "up") == want_up)
                        and abs(lvl - tl) / tl * 100 <= bs.HIT_PCT)

        rows.append({
            "Date": pd.Timestamp(df["Date"].iloc[j]).normalize(),
            "hit_primary": hit(p["direction"], p["level"]),
            "hit_band": hit(band_dir, band_lvl),
            "err_primary": abs(p["level"] - tl) / tl * 100,
            "move_pct": abs(tl - c) / c * 100,
            "bars_to_swing": truth["in_days"],
            "atr_pct_raw": a / c,
        })
    return pd.DataFrame(rows)


def meta_features(ticker: str, period: str) -> pd.DataFrame:
    raw = bs._cached(ticker, period)
    if raw is None:
        raise FileNotFoundError(f"no cache for {ticker}")
    f = features_for(raw.reset_index(), _context())
    f["Date"] = pd.to_datetime(f["Date"]).dt.normalize()
    return f[["Date"] + FEATURES].dropna().reset_index(drop=True)


def wf_meta(d: pd.DataFrame, feats: list[str], target: str, n_folds: int,
            min_train: int = 150, seed: int = 0, model: str = "gbm"
            ) -> pd.DataFrame:
    """Purged walk-forward over the prediction points themselves."""
    d = d.sort_values("Date").reset_index(drop=True)
    n = len(d)
    edges = np.linspace(int(n * 0.4), n, n_folds + 1).astype(int)
    x = d[feats].to_numpy(dtype=float)
    y = d[target].to_numpy(dtype=float)
    out = []
    for k in range(n_folds):
        lo, hi = edges[k], edges[k + 1]
        if hi - lo < 20 or lo < min_train:
            continue
        # embargo: the label at a point resolves at the NEXT swing, so drop
        # the handful of training points whose outcome overlaps the test edge
        emb = 5
        tr = np.zeros(n, bool)
        tr[:max(0, lo - emb)] = True
        te = np.zeros(n, bool)
        te[lo:hi] = True
        if tr.sum() < min_train or len(np.unique(y[tr])) < 2:
            continue
        ncal = max(10, int(tr.sum() * 0.25))
        tri = np.flatnonzero(tr)
        fit_i, cal_i = tri[:-ncal], tri[-ncal:]
        if len(np.unique(y[fit_i])) < 2:
            continue
        leaf = max(15, int(len(fit_i) * 0.08))
        m = (StumpGBM(seed=seed, min_leaf=leaf, rounds=120)
             if model == "gbm" else LogitL2(lam=8.0))
        m.fit(x[fit_i], y[fit_i])
        cal = Calibrator().fit(m.predict_proba(x[cal_i]), y[cal_i])
        ch = d.loc[te].copy()
        ch["p_meta"] = cal.transform(m.predict_proba(x[te]))
        ch["fold"] = k
        out.append(ch)
    if not out:
        raise RuntimeError("no usable folds")
    return pd.concat(out, ignore_index=True)


def gate_table(res: pd.DataFrame, target: str) -> pd.DataFrame:
    base = res[target].mean()
    rows = [{"gate": "no gate (base rate)", "kept": len(res),
             "kept_pct": 100.0, "hit_rate": round(base, 4),
             "lift_pp": 0.0}]
    for q in (0.5, 0.7, 0.8, 0.9):
        thr = float(np.quantile(res["p_meta"], q))
        s = res[res["p_meta"] >= thr]
        if len(s) < 10:
            continue
        rows.append({"gate": f"meta p >= q{int(q*100)}", "kept": len(s),
                     "kept_pct": round(100 * len(s) / len(res), 1),
                     "hit_rate": round(float(s[target].mean()), 4),
                     "lift_pp": round(100 * (s[target].mean() - base), 2)})
    return pd.DataFrame(rows)


def perm_test(res: pd.DataFrame, target: str, q: float, n_perm: int = 3000,
              seed: int = 0) -> dict:
    r = res.sort_values("Date").reset_index(drop=True)
    p = r["p_meta"].to_numpy()
    y = r[target].to_numpy(dtype=float)
    n = len(r)
    thr = float(np.quantile(p, q))
    obs = float(y[p >= thr].mean())
    rng = np.random.default_rng(seed)
    losh = max(5, n // 50)
    null = []
    for _ in range(n_perm):
        s = int(rng.integers(losh, n - losh))
        ps = np.roll(p, s)
        sel = y[ps >= float(np.quantile(ps, q))]
        if len(sel):
            null.append(sel.mean())
    null = np.array(null)
    return {"observed": round(obs, 4), "null_mean": round(float(null.mean()), 4),
            "null_p95": round(float(np.percentile(null, 95)), 4),
            "p_value": round(float((null >= obs).mean()), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--period", default="15y")
    ap.add_argument("--version", default="v2")
    ap.add_argument("--primary", default="primary",
                    choices=["primary", "band"],
                    help="gate the v2 predictor, or gate the ATR-band null")
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--model", default="gbm", choices=["gbm", "logit"])
    a = ap.parse_args()

    target = "hit_primary" if a.primary == "primary" else "hit_band"
    pts = prediction_points(a.ticker, a.period, a.version)
    feats = meta_features(a.ticker, a.period)
    d = pts.merge(feats, on="Date", how="inner").dropna(
        subset=FEATURES).reset_index(drop=True)
    d[target] = d[target].astype(float)
    print(f"{a.ticker} {a.period}: {len(d):,} prediction points with features, "
          f"{d['Date'].min().date()} -> {d['Date'].max().date()}")
    print(f"gating: {a.primary} ({'v2' if a.primary=='primary' else 'close+/-1ATR alternation'})"
          f"   base hit rate {d[target].mean():.2%}  "
          f"({int(d[target].sum())} hits)\n")

    print("What DID distinguish the historic hits? (descriptive, in-sample)")
    h, m = d[d[target] == 1], d[d[target] == 0]
    desc = pd.DataFrame({
        "on hits": [h["move_pct"].median(), h["bars_to_swing"].median(),
                    h["atr_pct_raw"].median() * 100, h["rsi21"].median(),
                    h["adx14"].median()],
        "on misses": [m["move_pct"].median(), m["bars_to_swing"].median(),
                      m["atr_pct_raw"].median() * 100, m["rsi21"].median(),
                      m["adx14"].median()]},
        index=["realised move %", "bars to swing", "ATR % of price",
               "RSI-21", "ADX-14"]).round(2)
    print(desc.to_string())
    print("  NOTE: this is selection on the outcome. It describes, it does not\n"
          "  predict. The walk-forward below is the part that counts.\n")

    res_full = wf_meta(d, FEATURES, target, a.folds, model=a.model)
    res_vol = wf_meta(d, VOL_ONLY, target, a.folds, model=a.model)
    print(f"out-of-sample meta points: {len(res_full):,}  "
          f"folds {res_full['fold'].nunique()}")
    print(f"meta AUC, all {len(FEATURES)} features : "
          f"{auc(res_full[target].to_numpy(), res_full['p_meta'].to_numpy()):.4f}")
    print(f"meta AUC, volatility feature only     : "
          f"{auc(res_vol[target].to_numpy(), res_vol['p_meta'].to_numpy()):.4f}")

    print(f"\ngating with the FULL meta-model:")
    print(gate_table(res_full, target).to_string(index=False))
    print(f"\ngating with the VOLATILITY-ONLY control:")
    print(gate_table(res_vol, target).to_string(index=False))

    print("\npermutation test on the full model (selection size held fixed):")
    pt = pd.DataFrame([{**{"q": q}, **perm_test(res_full, target, q)}
                       for q in (0.5, 0.7, 0.8, 0.9)])
    pt["p_adj_bonferroni"] = np.minimum(1.0, pt["p_value"] * len(pt))
    pt["significant"] = pt["p_adj_bonferroni"] < 0.05
    print(pt.to_string(index=False))

    print("\nECONOMIC TEST: does a gated hit convert into anything?")
    for label, rr in (("all points", res_full),
                      ("meta p >= q80",
                       res_full[res_full["p_meta"] >= res_full["p_meta"]
                                .quantile(0.8)])):
        lo, hi = date_block_bootstrap(rr["Date"].to_numpy(),
                                      rr[target].to_numpy(), 2000)
        print(f"  {label:<16} n={len(rr):5d}  hit={rr[target].mean():.2%}  "
              f"95% CI [{lo:.2%}, {hi:.2%}]  "
              f"median realised move {rr['move_pct'].median():.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
