"""Stage-0/confirm indicator library (additive — never replaces combos.py signals).

Precedence (per market-research consensus: weekly bias > daily structure >
strength/momentum > entry trigger):
  1. MTF trend: weekly EMA21 bias + daily SMA200 regime (both causal).
  2. Light-gate alignment score: price vs 9 daily MAs -> WATCHLIST.
  3. Confirms on finalized signals: RSI range-shift, hidden divergence, MACD.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAS = ["ema8", "ema21", "ema34", "ema50", "ema100", "ema200",
       "sma50", "sma100", "sma200"]


def add_extra(df: pd.DataFrame) -> pd.DataFrame:
    """Daily frame -> + long MAs, MACD, weekly trend, RSI-range + hidden-div flags."""
    df = df.copy()
    c = df["Close"]
    for n in (8, 34, 100, 200):
        df[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    for n in (50, 100, 200):
        df[f"sma{n}"] = c.rolling(n).mean()
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["macd"], df["macd_sig"] = macd, macd.ewm(span=9, adjust=False).mean()
    # weekly trend, causal: last COMPLETED week only
    w = c.resample("W-FRI").last()
    w_ema21 = w.ewm(span=21, adjust=False).mean()
    w_up = (w > w_ema21).reindex(c.index, method="ffill").shift(1).fillna(False)
    df["weekly_up"] = w_up.astype(bool)
    df["bull_reg"] = c > df["sma200"]
    df["bear_reg"] = c < df["sma200"]
    df["rsi_range_ok_long"] = (df["rsi"] >= 40) & df["bull_reg"]
    df["rsi_range_ok_short"] = (df["rsi"] <= 60) & df["bear_reg"]
    df["macd_ok_long"] = df["macd"] > df["macd_sig"]
    df["macd_ok_short"] = df["macd"] < df["macd_sig"]
    df["mtf_ok_long"] = df["weekly_up"] & df["bull_reg"]
    df["mtf_ok_short"] = (~df["weekly_up"]) & df["bear_reg"]
    hb, hr = _hidden_div(df)
    df["hidiv_long"], df["hidiv_short"] = hb, hr
    return df


def alignment(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar bull/bear fractions across the 9-MA stack."""
    c = df["Close"]
    votes = pd.DataFrame({m: (c > df[m]).astype(float) for m in MAS
                          if m in df.columns})
    out = pd.DataFrame(index=df.index)
    out["bull_frac"] = votes.mean(axis=1)
    out["bear_frac"] = 1.0 - out["bull_frac"]
    return out


def _pivots(s: pd.Series, k: int = 3, kind: str = "low") -> np.ndarray:
    win = 2 * k + 1
    ref = s.rolling(win, center=True).min() if kind == "low" \
        else s.rolling(win, center=True).max()
    return np.flatnonzero((s == ref).to_numpy())


def _hidden_div(df: pd.DataFrame, k: int = 3, lookback: int = 60,
                valid_days: int = 5) -> tuple:
    """Bullish hidden: higher pivot low + lower RSI at pivots, in uptrend
    (continuation). Bearish mirror. Flags stay hot `valid_days` bars."""
    n = len(df)
    lo = df["Low"].to_numpy(); hi = df["High"].to_numpy()
    r = df["rsi"].to_numpy()
    bull_reg = df["bull_reg"].to_numpy(); bear_reg = df["bear_reg"].to_numpy()
    bull = np.zeros(n, dtype=bool); bear = np.zeros(n, dtype=bool)
    pls = _pivots(df["Low"], k, "low")
    for a, b in zip(pls[:-1], pls[1:]):
        if b - a > lookback or not np.isfinite(r[a]) or not np.isfinite(r[b]):
            continue
        if lo[b] > lo[a] and r[b] < r[a] and bull_reg[b]:
            bull[b:min(b + 1 + valid_days, n)] = True
    phs = _pivots(df["High"], k, "high")
    for a, b in zip(phs[:-1], phs[1:]):
        if b - a > lookback or not np.isfinite(r[a]) or not np.isfinite(r[b]):
            continue
        if hi[b] < hi[a] and r[b] > r[a] and bear_reg[b]:
            bear[b:min(b + 1 + valid_days, n)] = True
    return bull, bear
