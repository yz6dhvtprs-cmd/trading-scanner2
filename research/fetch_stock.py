"""Per-ticker research data builder: OHLC maps + indicator maps + context.

Usage (project root, venv active):
    python research/fetch_stock.py --ticker AAPL
    python research/fetch_stock.py --ticker AAPL --refresh   # same; rebuilds all

Writes research/<TICKER>/ (ticker-parameterized so new stocks slot in):
    daily_100.csv       last 100 daily candles OHLCV (rebuilt each trading day)
    h1_100.csv          last 100 1h candles OHLCV
    m15_100.csv         last 100 15m candles OHLCV
    weekly_10.csv       last 10 weekly candles OHLC
    ind_1d.csv          daily indicators (MA/EMA sets, RSI-21, MACD, VOL/VMA-10)
    ind_1h.csv          1h indicators (same sets; VWAP session)
    ind_15m.csv         15m indicators (same sets; VWAP session)
    swings_1d.csv       fractal swing highs/lows on daily (causal, k=3)
    volume_profile.json POC/VAH/VAL + top nodes from recent 15m volume
    spy_100.csv         SPY last 100 daily closes (market context / beta)
    vix_100.csv         VIX last 100 daily closes (regime context)
    META.json           build stamp, row counts, data ranges

Notes:
- yfinance 15m history is capped at 60d; 100 candles (~4 sessions) fits.
- MAs: 8,10,21,50,100,200. EMAs: 8,21,34,55,89,144,200.
- RSI-21 on all three timeframes (Wilder). MACD 12/26/9.
- VWAP: session-anchored on 1h/15m; 20d-anchored rolling VWAP on daily
  (daily has no session; the 20d anchor approximates a monthly level).
- Swings are causal: a swing at bar i is only "known" at bar i+k.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MA_SET = (8, 10, 21, 50, 100, 200)
EMA_SET = (8, 21, 34, 55, 89, 144, 200)


def flat(h: pd.DataFrame) -> pd.DataFrame:
    if isinstance(h.columns, pd.MultiIndex):
        h.columns = h.columns.get_level_values(0)
    h.columns = [str(c).capitalize() for c in h.columns]
    return h


def rsi_wilder(c: pd.Series, n: int = 21) -> pd.Series:
    d = c.diff()
    ru = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    rd = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def add_indicators(df: pd.DataFrame, vwap_anchor: str) -> pd.DataFrame:
    """Full indicator map on an OHLCV frame. vwap_anchor: 'session'|'20d'."""
    f = df.copy()
    c = f["Close"]
    for n in MA_SET:
        f[f"ma{n}"] = c.rolling(n).mean()
    for n in EMA_SET:
        f[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    f["rsi21"] = rsi_wilder(c, 21)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26,
                                                      adjust=False).mean()
    f["macd"] = macd
    f["macd_sig"] = macd.ewm(span=9, adjust=False).mean()
    f["macd_hist"] = f["macd"] - f["macd_sig"]
    f["vma10"] = f["Volume"].rolling(10).mean()
    f["rvol10"] = f["Volume"] / f["vma10"]
    tp = (f["High"] + f["Low"] + f["Close"]) / 3
    pv = tp * f["Volume"]
    if vwap_anchor == "session":
        day = f.index.date
        f["vwap"] = pv.groupby(day).cumsum() / f["Volume"].groupby(day).cumsum()
    else:  # 20d-anchored rolling VWAP for daily bars
        f["vwap"] = pv.rolling(20).sum() / f["Volume"].rolling(20).sum()
    f["atr14"] = _atr(f)
    return f


def _atr(f: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([f["High"] - f["Low"],
                    (f["High"] - f["Close"].shift()).abs(),
                    (f["Low"] - f["Close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def fractal_swings(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Causal fractal swings: swing at i confirmed at i+k (no lookahead when
    consumed with known_at = index[i+k])."""
    h, l = df["High"], df["Low"]
    win = 2 * k + 1
    is_hi = (h == h.rolling(win, center=True).max())
    is_lo = (l == l.rolling(win, center=True).min())
    rows = []
    for i in np.flatnonzero(is_hi.to_numpy()):
        rows.append({"date": df.index[i].date().isoformat(), "side": "high",
                     "price": round(float(h.iloc[i]), 2), "bar": int(i),
                     "known_at": df.index[min(i + k, len(df) - 1)]
                     .date().isoformat()})
    for i in np.flatnonzero(is_lo.to_numpy()):
        rows.append({"date": df.index[i].date().isoformat(), "side": "low",
                     "price": round(float(l.iloc[i]), 2), "bar": int(i),
                     "known_at": df.index[min(i + k, len(df) - 1)]
                     .date().isoformat()})
    out = pd.DataFrame(rows).sort_values("bar").reset_index(drop=True)
    return out


def volume_profile(m15: pd.DataFrame, bins: int = 24) -> dict:
    """POC/VAH/VAL over the 15m window + top-5 volume nodes."""
    tp = ((m15["High"] + m15["Low"] + m15["Close"]) / 3).to_numpy()
    v = m15["Volume"].to_numpy().astype(float)
    lo, hi = float(tp.min()), float(tp.max())
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    vol = np.bincount(idx, weights=v, minlength=bins)
    mid = (edges[:-1] + edges[1:]) / 2
    poc = int(np.argmax(vol))
    # expand 70% value area from POC
    tot, acc, a, b = vol.sum(), vol[poc], poc, poc
    while acc / tot < 0.70 and (a > 0 or b < bins - 1):
        lv = vol[a - 1] if a > 0 else -1
        rv = vol[b + 1] if b < bins - 1 else -1
        if lv >= rv:
            a -= 1
            acc += vol[a]
        else:
            b += 1
            acc += vol[b]
    top = sorted(range(bins), key=lambda i: -vol[i])[:5]
    return {"poc": round(float(mid[poc]), 2),
            "vah": round(float(mid[b]), 2), "val": round(float(mid[a]), 2),
            "nodes": [{"price": round(float(mid[i]), 2),
                       "vol_pct": round(float(vol[i] / tot * 100), 1)}
                      for i in top],
            "window": f"{m15.index[0]} -> {m15.index[-1]}"}


def download(t: str, period: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    h = flat(yf.download(t, period=period, interval=interval, auto_adjust=True,
                         progress=False))
    h = h.dropna(subset=["Close"])
    # yfinance may return duplicate/odd cols; keep first of each
    h = h.loc[:, ~h.columns.duplicated()]
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    t = a.ticker.upper()
    out = os.path.join(ROOT, "research", t)
    os.makedirs(out, exist_ok=True)

    daily = download(t, "2y", "1d")      # long history for MAs/EMAs/RSI
    h1 = download(t, "1y", "1h")
    m15 = download(t, "60d", "15m")
    if len(daily) < 210 or len(h1) < 120 or len(m15) < 100:
        print(f"abort: short history d={len(daily)} h1={len(h1)} "
              f"m15={len(m15)}", flush=True)
        return 1

    d100 = daily.iloc[-100:]
    h100 = h1.iloc[-100:]
    m100 = m15.iloc[-100:]
    for df, name in ((d100, "daily_100"), (h100, "h1_100"), (m100, "m15_100")):
        df[["Open", "High", "Low", "Close", "Volume"]].round(2).to_csv(
            os.path.join(out, f"{name}.csv"), index_label="ts")

    ind_d = add_indicators(daily, "20d").iloc[-100:]
    ind_h = add_indicators(h1, "session").iloc[-100:]
    ind_m = add_indicators(m15, "session").iloc[-100:]
    for df, name in ((ind_d, "ind_1d"), (ind_h, "ind_1h"), (ind_m, "ind_15m")):
        df.round(4).to_csv(os.path.join(out, f"{name}.csv"), index_label="ts")

    w = daily["Close"].resample("W-FRI").last()
    wo = daily["Open"].resample("W-FRI").first()
    wh = daily["High"].resample("W-FRI").max()
    wl = daily["Low"].resample("W-FRI").min()
    wv = daily["Volume"].resample("W-FRI").sum()
    weekly = pd.DataFrame({"Open": wo, "High": wh, "Low": wl, "Close": w,
                           "Volume": wv}).dropna().iloc[-10:]
    weekly.round(2).to_csv(os.path.join(out, "weekly_10.csv"),
                           index_label="week_ending")

    fractal_swings(daily).to_csv(os.path.join(out, "swings_1d.csv"),
                                 index=False)

    with open(os.path.join(out, "volume_profile.json"), "w") as f:
        json.dump(volume_profile(m100), f, indent=1)

    spy = download("SPY", "1y", "1d")[["Close"]].iloc[-100:]
    spy.to_csv(os.path.join(out, "spy_100.csv"), index_label="ts")
    vix = download("^VIX", "1y", "1d")[["Close"]].iloc[-100:]
    vix.to_csv(os.path.join(out, "vix_100.csv"), index_label="ts")

    meta = {"ticker": t,
            "built": pd.Timestamp.now(tz="America/Los_Angeles")
            .strftime("%Y-%m-%d %H:%M %Z"),
            "daily_range": f"{d100.index[0].date()} -> {d100.index[-1].date()}",
            "h1_range": f"{h100.index[0]} -> {h100.index[-1]}",
            "m15_range": f"{m100.index[0]} -> {m100.index[-1]}",
            "rows": {"daily": len(d100), "h1": len(h100), "m15": len(m100)}}
    with open(os.path.join(out, "META.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(f"{t}: maps written to research/{t}/ "
          f"(d={len(d100)} h1={len(h100)} m15={len(m100)})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
