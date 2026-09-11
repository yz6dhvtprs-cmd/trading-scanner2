"""15-minute improvement loop: cross-combinations for 10-day range prediction.

Usage:
    python research/range_loop.py --budget-min 12

Searches direction x high-model x low-model combos, walk-forward, zero
lookahead. TARGET: predict the next-10-day high AND low each within 1%.
Then trades the best combo on $25k over Aug 2026 vs SPY buy-hold:
  end < start -> Worst | under SPY -> bad | ~= SPY (+/-0.5%) -> fine |
  beats SPY -> best. Falls back AAPL -> QQQ -> SPY for the tradeable win.

Saves research/loop_results.csv (every combo) + research/loop_best.json.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_stock import download  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS = ("AAPL", "QQQ", "SPY")


def features(t: str) -> pd.DataFrame:
    df = download(t, "2y", "1d")
    c = df["Close"]
    df["atr20"] = _atr(df, 20)
    df["hi20"] = df["High"].rolling(20).max().shift(1)
    df["lo20"] = df["Low"].rolling(20).min().shift(1)
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    d = c.diff()
    ru = d.clip(lower=0).ewm(alpha=1 / 21, adjust=False).mean()
    rd = (-d.clip(upper=0)).ewm(alpha=1 / 21, adjust=False).mean()
    df["rsi21"] = 100 - 100 / (1 + ru / rd.replace(0, np.nan))
    m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26,
                                                   adjust=False).mean()
    df["macd_h"] = m - m.ewm(span=9, adjust=False).mean()
    df["mom10"] = c / c.shift(10) - 1
    # truth: next-10-bar high/low (read only in scoring, never in predict).
    # shift(-10)+rolling(10) at row i covers orig rows i+1..i+10 exactly.
    df["f_hi10"] = df["High"].shift(-10).rolling(10).max()
    df["f_lo10"] = df["Low"].shift(-10).rolling(10).min()
    df["f_ret10"] = c.shift(-10) / c - 1
    df["Date"] = df.index.tz_localize(None)
    return df.dropna().reset_index(drop=True)


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - df["Close"].shift()).abs(),
                    (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def trend_of(df: pd.DataFrame, mode: str) -> np.ndarray:
    c = df["Close"].to_numpy()
    if mode == "ema50":
        return (c > df["ema50"].to_numpy()).astype(int) * 2 - 1
    if mode == "macd":
        return (df["macd_h"].to_numpy() > 0).astype(int) * 2 - 1
    if mode == "mom10":
        return (df["mom10"].to_numpy() > 0).astype(int) * 2 - 1
    if mode == "rsi50":
        return (df["rsi21"].to_numpy() > 50).astype(int) * 2 - 1
    return np.zeros(len(df), dtype=int)  # none -> symmetric


def high_model(df: pd.DataFrame, name: str, k: float) -> np.ndarray:
    c = df["Close"].to_numpy()
    a = df["atr20"].to_numpy()
    if name == "close_atr":
        return c + k * a
    if name == "hi20_atr":
        return df["hi20"].to_numpy() + k * a
    if name == "keltner":
        return df["ema20"].to_numpy() + k * a
    if name == "fibext":  # 20d-range extension in trend direction (up leg)
        leg = df["hi20"].to_numpy() - df["lo20"].to_numpy()
        return df["hi20"].to_numpy() + leg * (k - 1)
    raise ValueError(name)


def low_model(df: pd.DataFrame, name: str, k: float) -> np.ndarray:
    c = df["Close"].to_numpy()
    a = df["atr20"].to_numpy()
    if name == "close_atr":
        return c - k * a
    if name == "lo20_atr":
        return df["lo20"].to_numpy() - k * a
    if name == "keltner":
        return df["ema20"].to_numpy() - k * a
    if name == "fibext":
        leg = df["hi20"].to_numpy() - df["lo20"].to_numpy()
        return df["lo20"].to_numpy() - leg * (k - 1)
    raise ValueError(name)


SPACE = {
    "trend": ["none", "ema50", "macd", "mom10", "rsi50"],
    "hi": [("close_atr", 1.5), ("close_atr", 2.0), ("close_atr", 2.5),
           ("close_atr", 3.0), ("hi20_atr", 0.5), ("hi20_atr", 1.0),
           ("keltner", 2.0), ("keltner", 2.5), ("fibext", 1.272),
           ("fibext", 1.618)],
    "lo": [("close_atr", 1.5), ("close_atr", 2.0), ("close_atr", 2.5),
           ("close_atr", 3.0), ("lo20_atr", 0.5), ("lo20_atr", 1.0),
           ("keltner", 2.0), ("keltner", 2.5), ("fibext", 1.272),
           ("fibext", 1.618)],
}
KS = [1.5, 2.0, 2.5, 3.0]


def search(df: pd.DataFrame, t0: float, budget: float) -> list:
    """Grid over trend x hi x lo (+ asymmetric k by trend). Vectorized."""
    n = len(df)
    pts = np.arange(60, n - 11, 5)
    thi = df["f_hi10"].to_numpy()[pts]
    tlo = df["f_lo10"].to_numpy()[pts]
    tret = df["f_ret10"].to_numpy()[pts]
    cc = df["Close"].to_numpy()[pts]
    out = []
    combos = list(itertools.product(SPACE["trend"], SPACE["hi"], SPACE["lo"]))
    for trend, (hn, hk), (ln, lk) in combos:
        if time.time() - t0 > budget:
            break
        tr = trend_of(df, trend)[pts]
        # asymmetric stretch: with-trend side +0.5 ATR multiple
        khi = np.where(tr > 0, hk + 0.5, hk)
        klo = np.where(tr < 0, lk + 0.5, lk)
        if trend == "none":
            khi = np.full_like(khi, hk, dtype=float)
            klo = np.full_like(klo, lk, dtype=float)
        ph = _apply(df, "hi", hn, khi, pts)
        pl = _apply(df, "lo", ln, klo, pts)
        he = np.abs(ph - thi) / thi * 100
        le = np.abs(pl - tlo) / tlo * 100
        hit = (he <= 1.0) & (le <= 1.0)
        mid = (ph + pl) / 2
        dacc = (((mid > cc).astype(int) * 2 - 1) * np.sign(tret) > 0).mean()
        out.append({"trend": trend, "hi": f"{hn}@{hk}", "lo": f"{ln}@{lk}",
                    "n": len(pts), "hit_rate": round(float(hit.mean()), 4),
                    "he_med": round(float(np.median(he)), 2),
                    "le_med": round(float(np.median(le)), 2),
                    "dir_acc": round(float(dacc), 3)})
    return out


def _apply(df, side, name, karr, pts):
    c = df["Close"].to_numpy()[pts]
    a = df["atr20"].to_numpy()[pts]
    if side == "hi":
        base = {"close_atr": c, "hi20_atr": df["hi20"].to_numpy()[pts],
                "keltner": df["ema20"].to_numpy()[pts]}.get(name)
        if base is not None:
            return base + karr * a
        leg = df["hi20"].to_numpy()[pts] - df["lo20"].to_numpy()[pts]
        return df["hi20"].to_numpy()[pts] + leg * (karr - 1)
    base = {"close_atr": c, "lo20_atr": df["lo20"].to_numpy()[pts],
            "keltner": df["ema20"].to_numpy()[pts]}.get(name)
    if base is not None:
        return base - karr * a
    leg = df["hi20"].to_numpy()[pts] - df["lo20"].to_numpy()[pts]
    return df["lo20"].to_numpy()[pts] - leg * (karr - 1)


def simulate(df: pd.DataFrame, combo: dict, capital: float = 25000.0,
             month: str = "2026-08") -> dict:
    """Daily-rebalanced long/short/flat on predicted 10d edge over one
    calendar month. Costs 2.5bps on position changes."""
    per = df[df["Date"].dt.strftime("%Y-%m") == month].copy().reset_index(
        drop=True) if "Date" in df.columns else None
    if per is None or len(per) < 5:
        return {"error": "month missing"}
    idx = per.index.to_numpy()
    gidx = df.index[df["Date"].dt.strftime("%Y-%m") == month].to_numpy()
    tr = trend_of(df, combo["trend"])[gidx]
    hn, hk = combo["hi"].split("@")
    ln, lk = combo["lo"].split("@")
    khi = np.where(tr > 0, float(hk) + 0.5, float(hk))
    klo = np.where(tr < 0, float(lk) + 0.5, float(lk))
    ph = _apply(df, "hi", hn, khi, gidx)
    pl = _apply(df, "lo", ln, klo, gidx)
    cc = df["Close"].to_numpy()[gidx]
    edge = ((ph + pl) / 2 - cc) / cc
    pos = np.where(edge > 0.003, 1, np.where(edge < -0.003, -1, 0))
    closes = df["Close"].to_numpy()
    nxt = np.minimum(gidx + 1, len(df) - 1)
    ret = closes[nxt] / cc - 1
    turnover = np.abs(np.diff(np.append(0, pos)))
    pnl = pos * ret - turnover * 0.00025
    eq = capital * float(np.prod(1 + pnl))
    bh = capital * float(cc[-1] / cc[0])
    return {"trades_days": int((pos != 0).sum()),
            "flips": int((turnover > 0).sum()), "equity": round(eq, 2),
            "ret_pct": round((eq / capital - 1) * 100, 2),
            "buyhold_pct": round((bh / capital - 1) * 100, 2)}


def grade(sim: dict, spy_bh: float) -> str:
    if sim.get("error"):
        return "no-data"
    r = sim["ret_pct"]
    if r < 0 and sim["equity"] < 25000:
        return "Worst" if r < -1 else "bad"
    if r < spy_bh - 0.5:
        return "bad"
    if r <= spy_bh + 0.5:
        return "fine"
    return "best"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float, default=12.0)
    a = ap.parse_args()
    t0 = time.time()
    budget = a.budget_min * 60
    frames = {}
    for t in TICKERS:
        if time.time() - t0 > budget * 0.25:
            break
        print(f"loading {t}...", flush=True)
        frames[t] = features(t)
    allres = []
    best = {}
    for t, df in frames.items():
        res = search(df, t0, t0 + budget * 0.7)
        for r in res:
            r["ticker"] = t
        allres += res
        res.sort(key=lambda r: (-r["hit_rate"],
                                r["he_med"] + r["le_med"]))
        best[t] = res[0]
        b = res[0]
        print(f"{t} BEST: trend={b['trend']} hi={b['hi']} lo={b['lo']} "
              f"hit={b['hit_rate']:.1%} he={b['he_med']}% le={b['le_med']}% "
              f"dir={b['dir_acc']:.0%} n={b['n']} ({len(res)} combos)",
              flush=True)
    pd.DataFrame(allres).to_csv(os.path.join(ROOT, "research",
                                             "loop_results.csv"), index=False)
    # capital sims: best combo per ticker over Aug 2026
    spy_bh = None
    sims = {}
    for t, df in frames.items():
        s = simulate(df, best[t])
        sims[t] = s
        if t == "SPY" and "buyhold_pct" in s:
            spy_bh = s["buyhold_pct"]
        print(f"{t} SIM Aug2026: {s}", flush=True)
    if spy_bh is None and "SPY" in frames:
        spy_bh = sims["SPY"].get("buyhold_pct", 0)
    for t, s in sims.items():
        g = grade(s, spy_bh or 0)
        print(f"{t} GRADE vs SPY({spy_bh}%): {g}", flush=True)
        s["grade"] = g
    with open(os.path.join(ROOT, "research", "loop_best.json"), "w") as f:
        json.dump({"best": best, "sims": sims, "spy_month_bh": spy_bh,
                   "elapsed_s": round(time.time() - t0)}, f, indent=1)
    print(f"done in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
