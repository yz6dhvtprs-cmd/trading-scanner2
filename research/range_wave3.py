"""Wave 3: regime-adaptive direction (trend in trends, fade in chop).

Regime (causal): adx20 > X -> trending (follow trend sign), else chop
(fade it). trend in {mom10, ema50}; X in {18, 22, 26}. Range model only
sizes the edge gate (same vol-scaled close+-k*ATR, k=1.5).
Select on Jun+Jul 2026, grade on Aug 2026 holdout vs SPY buy-hold.
Usage: python research/range_wave3.py
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


def adx20(df: pd.DataFrame) -> np.ndarray:
    h, l, c = df["High"], df["Low"], df["Close"]
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 20, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / 20, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1 / 20, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / 20, adjust=False).mean().to_numpy()


def sim(df, ax, trend, x, month, capital=25000.0):
    m = df["Date"].dt.strftime("%Y-%m") == month
    if m.sum() < 5:
        return None
    g = np.flatnonzero(m.to_numpy())
    tr = trend_of(df, trend)[g]
    trending = ax[g] > x
    side = np.where(trending, tr, -tr)  # follow in trends, fade in chop
    atr = df["atr20"].to_numpy()[g]
    med = pd.Series(df["atr20"].to_numpy()).rolling(60).median().to_numpy()[g]
    k = 1.5 * np.clip(atr / np.where(med > 0, med, atr), 0.7, 1.6)
    ph = _apply(df, "hi", "close_atr", np.where(tr > 0, k + 0.5, k), g)
    pl = _apply(df, "lo", "close_atr", np.where(tr < 0, k + 0.5, k), g)
    cc = df["Close"].to_numpy()[g]
    edge = np.abs((ph + pl) / 2 - cc) / cc
    pos = np.where(edge > 0.002, side, 0)
    closes = df["Close"].to_numpy()
    ret = closes[np.minimum(g + 1, len(df) - 1)] / cc - 1
    turnover = np.abs(np.diff(np.append(0, pos)))
    pnl = pos * ret - turnover * 0.00025
    eq = capital * float(np.prod(1 + pnl))
    return {"ret": round((eq / capital - 1) * 100, 2),
            "bh": round((cc[-1] / cc[0] - 1) * 100, 2)}


def main() -> int:
    frames = {t: features(t) for t in TICKERS}
    print("loaded", flush=True)
    rows = []
    for t, tr, x in itertools.product(TICKERS, ("mom10", "ema50"),
                                      (18, 22, 26)):
        df = frames[t]
        ax = adx20(df)
        r = {"ticker": t, "trend": tr, "adx_x": x}
        for mo in ("2026-06", "2026-07", "2026-08"):
            s = sim(df, ax, tr, x, mo)
            r[mo], r[f"{mo}_bh"] = s["ret"], s["bh"]
        r["sel"] = round((r["2026-06"] + r["2026-07"]) / 2, 2)
        rows.append(r)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(ROOT, "research", "wave3.csv"), index=False)
    spy_bh = float(res[res["ticker"] == "SPY"]["2026-08_bh"].iloc[0])
    print(res.sort_values("sel", ascending=False).to_string(index=False),
          flush=True)
    b = res.sort_values("sel", ascending=False).iloc[0]
    aug = b["2026-08"]
    grade = "Worst" if aug < -1 else ("bad" if aug < spy_bh - 0.5
                                     else ("fine" if aug <= spy_bh + 0.5
                                           else "best"))
    print(f"BEST sel={b['sel']}%: {b['ticker']} {b['trend']} adx>{b['adx_x']} "
          f"-> Aug {aug}% vs SPY {spy_bh}% => {grade}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
