"""Wave 2: vol-scaled asymmetric ranges + fade variants, selected on
Jun+Jul 2026, graded on Aug 2026 holdout vs SPY buy-hold.

Usage: python research/range_wave2.py
"""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from range_loop import TICKERS, _apply, features, trend_of  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONTHS = ("2026-06", "2026-07", "2026-08")


def sim(df, trend, base_k, fade, thresh, month, capital=25000.0):
    m = df["Date"].dt.strftime("%Y-%m") == month
    if m.sum() < 5:
        return None
    g = np.flatnonzero(m.to_numpy())
    tr = trend_of(df, trend)[g]
    # ATR-regime scaling: stretch k when current ATR > 60d median
    atr = df["atr20"].to_numpy()[g]
    med = pd.Series(df["atr20"].to_numpy()).rolling(60).median().to_numpy()[g]
    scale = np.clip(atr / np.where(med > 0, med, atr), 0.7, 1.6)
    k = base_k * scale
    khi = np.where(tr > 0, k + 0.5, k)
    klo = np.where(tr < 0, k + 0.5, k)
    ph = _apply(df, "hi", "close_atr", khi, g)
    pl = _apply(df, "lo", "close_atr", klo, g)
    cc = df["Close"].to_numpy()[g]
    edge = ((ph + pl) / 2 - cc) / cc * (-1 if fade else 1)
    pos = np.where(edge > thresh, 1, np.where(edge < -thresh, -1, 0))
    closes = df["Close"].to_numpy()
    ret = closes[np.minimum(g + 1, len(df) - 1)] / cc - 1
    turnover = np.abs(np.diff(np.append(0, pos)))
    pnl = pos * ret - turnover * 0.00025
    eq = capital * float(np.prod(1 + pnl))
    return {"ret": round((eq / capital - 1) * 100, 2),
            "bh": round((cc[-1] / cc[0] - 1) * 100, 2),
            "days": int((pos != 0).sum())}


def main() -> int:
    frames = {}
    for t in TICKERS:
        print(f"loading {t}...", flush=True)
        frames[t] = df = features(t)
    rows = []
    grid = itertools.product(TICKERS, ("ema50", "macd", "mom10", "rsi50"),
                             (1.5, 2.0), (False, True), (0.001, 0.003))
    for t, tr, k, fade, th in grid:
        df = frames[t]
        r = {"ticker": t, "trend": tr, "k": k, "fade": fade, "th": th}
        ok = True
        for mo in MONTHS:
            s = sim(df, tr, k, fade, th, mo)
            if s is None:
                ok = False
                break
            r[mo] = s["ret"]
            r[f"{mo}_bh"] = s["bh"]
        if not ok:
            continue
        r["sel"] = round((r["2026-06"] + r["2026-07"]) / 2, 2)
        rows.append(r)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(ROOT, "research", "wave2.csv"), index=False)
    spy_aug_bh = float(res[res["ticker"] == "SPY"]["2026-08_bh"].iloc[0])
    print(f"SPY Aug buy-hold: {spy_aug_bh}%", flush=True)
    top = res.sort_values(["sel", "2026-08"], ascending=False).head(10)
    print(top.to_string(index=False), flush=True)
    b = top.iloc[0]
    aug = b["2026-08"]
    grade = "Worst" if aug < -1 else ("bad" if aug < spy_aug_bh - 0.5
                                     else ("fine" if aug <= spy_aug_bh + 0.5
                                           else "best"))
    print(f"BEST sel(JunJul)={b['sel']}%: {b['ticker']} {b['trend']} k={b['k']} "
          f"fade={b['fade']} th={b['th']} -> Aug holdout {aug}% vs SPY "
          f"{spy_aug_bh}% => {grade}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
