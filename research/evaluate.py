"""Purged walk-forward evaluation, scored the way this repo grades everything
else: expectancy in R, not accuracy.

Why this file exists
--------------------
`backtest_swings.py` scored a point forecast of the next fractal's price and
called it a hit inside a 1% band. That metric is (a) not the one `grading.md`
uses anywhere else in the project and (b) beaten outright by an
information-free null. Here the question is instead: given the features at the
close of bar t, what is the probability a trade opened at the next open reaches
its target before its stop, and does acting on that probability produce
positive expectancy out of sample?

Protocol
--------
- Walk forward in time over the whole panel; every fold trains only on the past.
- PURGE + EMBARGO: a label at date d peeks `horizon` bars ahead, so training
  rows within `horizon` bars of the test window are dropped. Without this the
  train and test labels overlap and every metric is optimistic.
- Calibration is fitted on the tail of TRAIN, never on TEST.
- Confidence intervals come from a DATE-level block bootstrap: resampling rows
  independently would treat 38 tickers on the same day as 38 independent
  observations, which they are not.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from models import Calibrator, LogitL2, StumpGBM
from panel import FEATURES


# ------------------------------------------------------------------- metrics

def auc(y: np.ndarray, p: np.ndarray) -> float:
    if len(np.unique(y)) < 2:
        return float("nan")
    r = pd.Series(p).rank().to_numpy()
    n1 = float(y.sum())
    n0 = float(len(y) - n1)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def expectancy(r: np.ndarray) -> float:
    return float(np.mean(r)) if len(r) else float("nan")


def profit_factor(r: np.ndarray) -> float:
    win, loss = r[r > 0].sum(), -r[r < 0].sum()
    return float(win / loss) if loss > 0 else float("inf")


def date_block_bootstrap(dates: np.ndarray, values: np.ndarray,
                         n_boot: int = 2000, seed: int = 0) -> tuple:
    """Resample whole DATES with replacement, keeping each date's rows intact."""
    if len(values) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(dates, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    sorted_vals = values[order]
    counts = np.bincount(inv, minlength=len(uniq))
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([np.arange(starts[k], starts[k] + counts[k])
                              for k in pick if counts[k] > 0])
        means[b] = sorted_vals[idx].mean() if len(idx) else np.nan
    return (float(np.nanpercentile(means, 2.5)),
            float(np.nanpercentile(means, 97.5)))


# ------------------------------------------------------------- walk forward

def walk_forward(panel: pd.DataFrame, model_name: str = "gbm",
                 horizon: int = 20, n_folds: int = 8,
                 min_train_years: float = 4.0,
                 calib_frac: float = 0.2, seed: int = 0,
                 min_train_rows: int = 2000,
                 min_test_rows: int = 50) -> pd.DataFrame:
    """Return the panel's test rows with an out-of-sample `p` column."""
    d = panel["Date"].to_numpy()
    uniq = np.unique(d)
    start_i = int(np.searchsorted(
        uniq, uniq[0] + np.timedelta64(int(min_train_years * 365), "D")))
    if start_i >= len(uniq) - 10:
        raise RuntimeError("not enough history for the requested train window")
    edges = np.linspace(start_i, len(uniq), n_folds + 1).astype(int)

    x_all = panel[FEATURES].to_numpy(dtype=float)
    y_all = panel["y_win"].to_numpy(dtype=float)
    out = []
    for k in range(n_folds):
        te_lo, te_hi = edges[k], edges[k + 1]
        if te_hi - te_lo < 5:
            continue
        test_start = uniq[te_lo]
        # purge: a training label spans `horizon` BARS; use calendar days with
        # a generous multiplier so weekends/holidays cannot sneak overlap in.
        cut = test_start - np.timedelta64(int(horizon * 1.6) + 3, "D")
        tr = d < cut
        te = (d >= test_start) & (d < (uniq[te_hi] if te_hi < len(uniq)
                                       else uniq[-1] + np.timedelta64(1, "D")))
        if tr.sum() < min_train_rows or te.sum() < min_test_rows:
            continue

        # hold out the tail of train for calibration only
        tr_idx = np.flatnonzero(tr)
        n_cal = max(1, int(len(tr_idx) * calib_frac))
        fit_idx, cal_idx = tr_idx[:-n_cal], tr_idx[-n_cal:]

        # leaves must stay a sane fraction of a small single-ticker sample
        leaf = max(25, min(200, int(len(fit_idx) * 0.05)))
        model = (StumpGBM(seed=seed, min_leaf=leaf) if model_name == "gbm"
                 else LogitL2(lam=5.0))
        model.fit(x_all[fit_idx], y_all[fit_idx])
        cal = Calibrator().fit(model.predict_proba(x_all[cal_idx]),
                               y_all[cal_idx])
        p = cal.transform(model.predict_proba(x_all[te]))

        chunk = panel.loc[te, ["Date", "ticker", "y_R", "y_win", "adx14",
                               "rvol", "d_hi20", "ema_stack"]].copy()
        chunk["p"] = p
        chunk["fold"] = k
        out.append(chunk)
    if not out:
        raise RuntimeError("no usable folds")
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------- scoring

def score_threshold(res: pd.DataFrame, thr: float, label: str,
                    n_boot: int = 1000) -> dict:
    sel = res[res["p"] >= thr]
    r = sel["y_R"].to_numpy()
    lo, hi = date_block_bootstrap(sel["Date"].to_numpy(), r, n_boot)
    return {"rule": label, "thr": round(thr, 3), "trades": len(r),
            "trades_pct": round(100 * len(r) / max(len(res), 1), 1),
            "win_rate": round(float((r > 0).mean()), 3) if len(r) else np.nan,
            "expectancy_R": round(expectancy(r), 4),
            "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
            "pf": round(profit_factor(r), 2) if len(r) else np.nan}


def shift_permutation_test(res: pd.DataFrame, q: float, n_perm: int = 2000,
                           seed: int = 0) -> dict:
    """Is the model's selected subset better than an equally sized subset
    chosen with no information?

    Comparing the selected subset's confidence interval against the
    all-trades mean is NOT a valid test: the subset is drawn from that very
    population, so the two are strongly dependent and the comparison is
    biased toward "significant". The honest null keeps the size of the
    selection fixed and destroys only the ALIGNMENT between the score and the
    outcome.

    We break alignment with a circular shift rather than an i.i.d. shuffle.
    Overlapping 20-bar labels make y_R strongly autocorrelated; an i.i.d.
    shuffle destroys that autocorrelation too and yields an over-tight null
    that almost anything beats. A circular shift preserves the serial
    structure of both series and moves only their phase.
    """
    r = res.sort_values("Date").reset_index(drop=True)
    p = r["p"].to_numpy()
    y = r["y_R"].to_numpy()
    n = len(r)
    k = max(1, int(round(n * (1 - q))))
    thr = float(np.quantile(p, q))
    obs = float(y[p >= thr].mean())

    rng = np.random.default_rng(seed)
    # avoid tiny shifts, which leave the series almost aligned
    lo_shift = max(21, n // 50)
    null = np.empty(n_perm)
    for i in range(n_perm):
        s = int(rng.integers(lo_shift, n - lo_shift))
        ps = np.roll(p, s)
        t = float(np.quantile(ps, q))
        sel = y[ps >= t]
        null[i] = sel.mean() if len(sel) else np.nan
    null = null[np.isfinite(null)]
    pval = float((null >= obs).mean())
    return {"q": q, "thr": round(thr, 4), "k_selected": int((p >= thr).sum()),
            "observed_R": round(obs, 4),
            "null_mean_R": round(float(null.mean()), 4),
            "null_p95_R": round(float(np.percentile(null, 95)), 4),
            "p_value": round(pval, 4),
            "n_perm": len(null)}


def grade(exp_r: float, pf: float, n: int) -> str:
    """The rubric from grading.md, applied unchanged."""
    if n < 8:
        return "B (thin)"
    if exp_r >= 0.50 and pf >= 1.5:
        return "A+"
    if exp_r >= 0.30 and pf >= 1.3:
        return "A"
    if exp_r >= 0.15:
        return "B+"
    return "B"


def null_baselines(res: pd.DataFrame, seed: int = 0,
                   n_boot: int = 1000) -> list[dict]:
    """Rules that use no learned information, scored identically."""
    rng = np.random.default_rng(seed)
    rows = [score_threshold(res, -1.0, "take every bar (null: always trade)",
                            n_boot)]
    shuffled = res.copy()
    shuffled["p"] = rng.permutation(res["p"].to_numpy())
    q = float(np.quantile(res["p"], 0.8))
    s = score_threshold(shuffled, q, "shuffled probabilities (null)", n_boot)
    rows.append(s)
    # the repo's own validated primary: breakout-long ADX>=20 RVOL>=2
    rule = res[(res["adx14"] >= 20) & (res["rvol"] >= 2.0)
               & (res["d_hi20"] >= 0)]
    r = rule["y_R"].to_numpy()
    lo, hi = date_block_bootstrap(rule["Date"].to_numpy(), r, n_boot)
    rows.append({"rule": "repo primary: breakout ADX>=20 RVOL>=2", "thr": None,
                 "trades": len(r),
                 "trades_pct": round(100 * len(r) / max(len(res), 1), 1),
                 "win_rate": round(float((r > 0).mean()), 3) if len(r) else np.nan,
                 "expectancy_R": round(expectancy(r), 4),
                 "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                 "pf": round(profit_factor(r), 2) if len(r) else np.nan})
    return rows


def calibration_table(res: pd.DataFrame, bins: int = 10) -> pd.DataFrame:
    q = pd.qcut(res["p"], bins, duplicates="drop")
    g = res.groupby(q, observed=True).agg(
        n=("y_win", "size"), p_mean=("p", "mean"),
        actual=("y_win", "mean"), exp_R=("y_R", "mean"))
    return g.round(3).reset_index(drop=True)
