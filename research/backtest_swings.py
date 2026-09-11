"""Walk-forward swing-prediction harness: predict the NEXT swing, score it.

Usage:
    python research/backtest_swings.py --ticker AAPL [--version v1]
    python research/backtest_swings.py --ticker AAPL --asof 2026-07-22

Method (no lookahead anywhere):
- For each as-of date (default: every confirmed swing in the window, min
  50 prior daily bars), the predictor sees ONLY the previous 50 daily bars
  + swings confirmed on/before as-of.
- It predicts: direction (up/down), level, eta_days.
- Truth = the next realized fractal swing (k=3) after as-of; level error
  = |pred-actual|/actual. HIT iff error <= 1% AND direction right.
- First half of points = FIT (tune here), second half = HOLDOUT (verify).

Predictor versions (see SWING_ALGO.md for the reasoning log):
- v1: trend-direction 1.272 extension of the last completed leg; range ->
  nearest 20d pivot (R1/S1). ETA = distance / median 20d range.
- v2: v1 + RSI-21 regime: strong (rsi>60 up / <40 down) uses 1.618 ext,
  weak uses 0.786 retracement of the last leg; range uses POC/VAH/VAL.
- v3: alternation prior (next swing = opposite side of the last confirmed
  one) + 50% measured retracement of the last leg. Rationale: at a fresh
  confirmed extreme the next fractal is usually a minor counter-move, not
  a fresh trend extension.

Lower-TF note: the live algos confirm on 1h/15m, but intraday history is
too short for swing backtests, so the harness scores the DAILY-structure
core only. MTF confirmation is a live overlay, not part of the score.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_stock import add_indicators, download, fractal_swings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K = 3
HIT_PCT = 1.0


def load_daily(t: str) -> pd.DataFrame:
    df = add_indicators(download(t, "2y", "1d"), "20d")
    df = df.dropna(subset=["Close"]).reset_index()
    if "Date" not in df.columns:  # index had no name; first col is the date
        df = df.rename(columns={df.columns[0]: "Date"})
    df["Date"] = pd.to_datetime(df["Date"])
    return df.reset_index(drop=True)


def predict(df: pd.DataFrame, j: int, version: str) -> dict:
    """Predict next swing as of bar j using bars [j-49, j]. Causal."""
    w = df.iloc[j - 49:j + 1].copy().reset_index(drop=True)
    c = float(w["Close"].iloc[-1])
    rsi = float(w["rsi21"].iloc[-1])
    sw = fractal_swings(_with_dates(w), K)
    # confirmed on/before as-of bar: known bar index <= 49 within window
    sw = sw[sw["bar"] + K <= 49].sort_values("bar").reset_index(drop=True)
    if len(sw) < 2:
        return {"direction": "up", "level": round(c * 1.01, 2), "eta": 5,
                "why": "fallback: too few swings"}
    last, prev = sw.iloc[-1], sw.iloc[-2]
    rng20 = float((w["High"].iloc[-20:] - w["Low"].iloc[-20:]).median())
    pp = (float(w["High"].iloc[-20:].max())
          + float(w["Low"].iloc[-20:].min()) + c) / 3
    rng = float(w["High"].iloc[-20:].max() - w["Low"].iloc[-20:].min())
    hi20 = float(w["High"].iloc[-20:].max())
    lo20 = float(w["Low"].iloc[-20:].min())

    hs = sw[sw["side"] == "high"]["price"].to_numpy()
    ls = sw[sw["side"] == "low"]["price"].to_numpy()
    up = len(hs) >= 2 and len(ls) >= 2 and hs[-1] > hs[-2] \
        and ls[-1] > ls[-2]
    dn = len(hs) >= 2 and len(ls) >= 2 and hs[-1] < hs[-2] \
        and ls[-1] < ls[-2]
    leg_lo = min(float(prev["price"]), float(last["price"]))
    leg_hi = max(float(prev["price"]), float(last["price"]))
    leg = leg_hi - leg_lo

    if version == "v1":
        if up:
            lvl, direction = leg_hi + leg * 0.272, "up"
            why = "v1 uptrend 1.272 ext"
        elif dn:
            lvl, direction = leg_lo - leg * 0.272, "down"
            why = "v1 downtrend 1.272 ext"
        else:
            direction = "up" if c - lo20 < hi20 - c else "down"
            lvl = pp + rng if direction == "up" else pp - rng
            why = "v1 range R1/S1 pivot"
    elif version in ("v3", "v4"):  # alternation + measured retracement
        depth = 0.5 if version == "v3" else 0.382
        anchor = float(last["price"])
        if last["side"] == "high":
            direction = "down"
            lvl = anchor - leg * depth
            why = f"{version} high->low {depth:.0%} retrace"
        else:
            direction = "up"
            lvl = anchor + leg * depth
            why = f"{version} low->high {depth:.0%} retrace"
    else:  # v2: RSI regime picks extension vs retracement depth
        strong_up = up and rsi > 60
        strong_dn = dn and rsi < 40
        if strong_up:
            lvl, direction = leg_hi + leg * 0.618, "up"
            why = "v2 strong-up 1.618 ext"
        elif strong_dn:
            lvl, direction = leg_lo - leg * 0.618, "down"
            why = "v2 strong-down 1.618 ext"
        elif up:
            lvl, direction = leg_hi - leg * 0.214, "up"
            why = "v2 weak-up 0.786 retrace"
        elif dn:
            lvl, direction = leg_lo + leg * 0.214, "down"
            why = "v2 weak-down 0.786 retrace"
        else:
            direction = "up" if rsi >= 50 else "down"
            lvl = (leg_hi + leg_lo) / 2 + (leg * 0.272 if direction == "up"
                                           else -leg * 0.272)
            why = "v2 range mid+0.272"
    eta = max(1, int(round(abs(lvl - c) / rng20))) if rng20 > 0 else 5
    return {"direction": direction, "level": round(lvl, 2), "eta": eta,
            "why": why}


def _with_dates(w: pd.DataFrame) -> pd.DataFrame:
    out = w.copy()
    out.index = pd.DatetimeIndex(w["Date"]) if "Date" in w.columns \
        else pd.date_range("2020-01-01", periods=len(w), freq="B")
    return out


def next_swing_truth(df: pd.DataFrame, j: int) -> dict | None:
    """Next fractal swing strictly after bar j (full-history computation,
    but only swings with swing-bar > j are read — no leakage into predict)."""
    full = fractal_swings(_with_dates(df.reset_index(drop=True)), K)
    # map window bars: full-frame bar numbers align with df integer loc
    fut = full[full["bar"] > j].sort_values("bar")
    if len(fut) == 0:
        return None
    n = fut.iloc[0]
    return {"side": n["side"], "level": float(n["price"]),
            "bar": int(n["bar"]), "date": n["date"],
            "in_days": int(n["bar"]) - j}


def run(t: str, version: str, asof: str | None) -> int:
    df = load_daily(t)
    if "Date" not in df.columns and not isinstance(df.index,
                                                   pd.DatetimeIndex):
        df["Date"] = pd.date_range("2020-01-01", periods=len(df), freq="B")
    if asof:
        pts = [int(np.searchsorted(
            pd.DatetimeIndex(df["Date"]).date.astype(str), asof))]
    else:
        full = fractal_swings(_with_dates(df), K)
        # predict FROM each confirmed swing (as-of = confirmation bar)
        pts = sorted({min(int(b) + K, len(df) - 1)
                      for b in full["bar"].to_numpy()
                      if int(b) + K >= 60 and int(b) + K < len(df) - K - 1})
        pts = [p for p in pts if p >= 60]
    rows = []
    for j in pts:
        p = predict(df, j, version)
        truth = next_swing_truth(df, j)
        if truth is None:
            continue
        want_up = truth["side"] == "high"
        dir_ok = (p["direction"] == "up") == want_up
        err = abs(p["level"] - truth["level"]) / truth["level"] * 100
        rows.append({"asof": str(df["Date"].iloc[j].date())
                     if hasattr(df["Date"].iloc[j], "date")
                     else str(df["Date"].iloc[j]),
                     "pred_dir": p["direction"], "pred_lvl": p["level"],
                     "pred_eta": p["eta"], "true_side": truth["side"],
                     "true_lvl": truth["level"], "true_in": truth["in_days"],
                     "err_pct": round(err, 2),
                     "hit": bool(dir_ok and err <= HIT_PCT), "why": p["why"]})
    if not rows:
        print("no prediction points", flush=True)
        return 1
    r = pd.DataFrame(rows)
    n = len(r)
    fit, hold = r.iloc[:n // 2], r.iloc[n // 2:]
    for name, s in (("FIT", fit), ("HOLDOUT", hold), ("ALL", r)):
        print(f"{version} {name}: n={len(s)} hits={int(s['hit'].sum())} "
              f"hit_rate={s['hit'].mean():.0%} med_err={s['err_pct'].median():.2f}% "
              f"mean_err={s['err_pct'].mean():.2f}%", flush=True)
    print(r.to_string(index=False), flush=True)
    r.to_csv(os.path.join(ROOT, "research", t.upper(),
                          f"swingtest_{version}.csv"), index=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--version", default="v1")
    ap.add_argument("--asof", default=None)
    a = ap.parse_args()
    return run(a.ticker.upper(), a.version, a.asof)


if __name__ == "__main__":
    sys.exit(main())
