"""Separated directional algorithms: LONG / SHORT / REVERSAL / REJECTION.

Each is standalone: same map inputs, own trigger/gate/exit logic, no shared
state. All reads are causal (row i uses only data known at/before bar i;
swings use their known_at confirmation bar).

Usage:
    from signals import long_signal, short_signal, reversal_signal, \\
        rejection_signal, load_maps
    m = load_maps("AAPL")
    print(long_signal(m, -1))   # as-of last bar; use an int offset or date

A signal dict: {algo, side, entry, stop, target, eta_days, rr, reason}.
None = no setup. eta_days = bars to target at recent swing velocity.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIBS = (0.382, 0.5, 0.618, 0.786)
EXTS = (1.272, 1.618)


def load_maps(ticker: str) -> dict:
    d = os.path.join(ROOT, "research", ticker.upper())
    out = {}
    for name in ("daily_100", "h1_100", "m15_100", "weekly_10", "ind_1d",
                 "ind_1h", "ind_15m", "swings_1d", "news_15m", "spy_100",
                 "vix_100"):
        p = os.path.join(d, f"{name}.csv")
        if os.path.exists(p):
            df = pd.read_csv(p, parse_dates=[0])
            out[name] = df.set_index(df.columns[0]).sort_index()
    for name in ("volume_profile", "earnings", "META"):
        p = os.path.join(d, f"{name}.json")
        if os.path.exists(p):
            out[name] = json.load(open(p))
    return out


def _row(m: dict, tf: str, i: int) -> pd.Series:
    df = m[tf]
    return df.iloc[i if i >= 0 else len(df) + i]


def _atr(m: dict, i: int) -> float:
    return float(_row(m, "ind_1d", i)["atr14"])


def confirmed_swings(m: dict, asof: str) -> pd.DataFrame:
    """Swings whose confirmation bar is on/before asof (causal)."""
    s = m["swings_1d"]
    return s[s["known_at"] <= asof].sort_values("bar").reset_index(drop=True)


def structure(sw: pd.DataFrame) -> str:
    """HH/HL/LH/LL read from the last 4 confirmed swings."""
    if len(sw) < 4:
        return "range"
    hs = sw[sw["side"] == "high"]["price"].to_numpy()[-2:]
    ls = sw[sw["side"] == "low"]["price"].to_numpy()[-2:]
    if len(hs) < 2 or len(ls) < 2:
        return "range"
    up = hs[1] > hs[0] and ls[1] > ls[0]
    dn = hs[1] < hs[0] and ls[1] < ls[0]
    return "up" if up else ("down" if dn else "range")


def fib_levels(lo: float, hi: float) -> dict:
    return {r: hi - (hi - lo) * r for r in FIBS}


def fib_ext(lo: float, hi: float, direction: int) -> dict:
    d = hi - lo
    if direction == 1:
        return {e: hi + d * (e - 1) for e in EXTS}
    return {e: lo - d * (e - 1) for e in EXTS}


def classic_pivots(h: float, l: float, c: float) -> dict:
    pp = (h + l + c) / 3
    return {"pp": pp, "r1": 2 * pp - l, "s1": 2 * pp - h,
            "r2": pp + (h - l), "s2": pp - (h - l)}


def _eta_days(m: dict, i: int, distance: float) -> int:
    """Bars to cover distance at the median daily range pace (min 1)."""
    d = m["daily_100"]
    lo = max(0, (len(d) + i if i < 0 else i) - 20)
    hi = len(d) + i if i < 0 else i
    pace = float((d["High"].iloc[lo:hi] - d["Low"].iloc[lo:hi]).median())
    return max(1, int(round(abs(distance) / pace))) if pace > 0 else 5


def _candle(o: float, h: float, l: float, c: float) -> str:
    rng = h - l
    if rng <= 0:
        return "-"
    body = abs(c - o)
    if body < 0.1 * rng:
        return "doji"
    if (min(o, c) - l) > 2 * body and (h - max(o, c)) < body:
        return "hammer" if c >= o else "hammer_dn"
    if (h - max(o, c)) > 2 * body and (min(o, c) - l) < body:
        return "shooting_star" if c <= o else "shooting_star_up"
    return "-"


# ---------- 1. LONG (trend pullback) ----------
def long_signal(m: dict, i: int = -1) -> dict | None:
    d, r = m["daily_100"], _row(m, "ind_1d", i)
    b = d.iloc[i if i >= 0 else len(d) + i]
    asof = str(d.index[i if i >= 0 else len(d) + i].date())
    sw = confirmed_swings(m, asof)
    if structure(sw) != "up":
        return None
    if not (b["Close"] > r["ema55"] and r["rsi21"] > 40
            and r["rsi21"] < 68 and r["macd_hist"] > -0.5):
        return None
    lows = sw[sw["side"] == "low"]
    highs = sw[sw["side"] == "high"]
    if len(lows) < 1 or len(highs) < 1:
        return None
    lo, hi = float(lows["price"].iloc[-1]), float(highs["price"].iloc[-1])
    fib = fib_levels(min(lo, hi), max(lo, hi))
    c = float(b["Close"])
    zone = any(abs(c - v) / v <= 0.02 for v in fib.values())
    zone = zone or abs(c - float(r["ema21"])) / c <= 0.015
    if not zone:
        return None
    h1 = _row(m, "ind_1h", -1)
    if not (float(m["h1_100"]["Close"].iloc[-1]) > float(h1["ema55"])):
        return None
    atr = _atr(m, i)
    stop = min(float(b["Low"]), lo) - 0.25 * atr
    tgt = hi if hi > c else c + 2 * (c - stop)
    return {"algo": "long", "side": "long", "entry": round(c, 2),
            "stop": round(stop, 2), "target": round(tgt, 2),
            "eta_days": _eta_days(m, i, tgt - c),
            "rr": round((tgt - c) / (c - stop), 2),
            "reason": f"uptrend pullback to fib/ema21, rsi "
                      f"{r['rsi21']:.0f}, 1h above ema55"}


# ---------- 2. SHORT (trend rally) ----------
def short_signal(m: dict, i: int = -1) -> dict | None:
    d, r = m["daily_100"], _row(m, "ind_1d", i)
    b = d.iloc[i if i >= 0 else len(d) + i]
    asof = str(d.index[i if i >= 0 else len(d) + i].date())
    sw = confirmed_swings(m, asof)
    if structure(sw) != "down":
        return None
    if not (b["Close"] < r["ema55"] and r["rsi21"] > 32
            and r["rsi21"] < 60 and r["macd_hist"] < 0.5):
        return None
    lows = sw[sw["side"] == "low"]
    highs = sw[sw["side"] == "high"]
    if len(lows) < 1 or len(highs) < 1:
        return None
    lo, hi = float(lows["price"].iloc[-1]), float(highs["price"].iloc[-1])
    fib = fib_levels(min(lo, hi), max(lo, hi))
    c = float(b["Close"])
    zone = any(abs(c - v) / v <= 0.02 for v in fib.values())
    zone = zone or abs(c - float(r["ema21"])) / c <= 0.015
    if not zone:
        return None
    h1 = _row(m, "ind_1h", -1)
    if not (float(m["h1_100"]["Close"].iloc[-1]) < float(h1["ema55"])):
        return None
    atr = _atr(m, i)
    stop = max(float(b["High"]), hi) + 0.25 * atr
    tgt = lo if lo < c else c - 2 * (stop - c)
    return {"algo": "short", "side": "short", "entry": round(c, 2),
            "stop": round(stop, 2), "target": round(tgt, 2),
            "eta_days": _eta_days(m, i, tgt - c),
            "rr": round((c - tgt) / (stop - c), 2),
            "reason": f"downtrend rally to fib/ema21, rsi "
                      f"{r['rsi21']:.0f}, 1h below ema55"}


# ---------- 3. REVERSAL (washed-out long at support) ----------
def reversal_signal(m: dict, i: int = -1) -> dict | None:
    d, r = m["daily_100"], _row(m, "ind_1d", i)
    b = d.iloc[i if i >= 0 else len(d) + i]
    asof = str(d.index[i if i >= 0 else len(d) + i].date())
    sw = confirmed_swings(m, asof)
    lows = sw[sw["side"] == "low"]
    if len(lows) < 1:
        return None
    c = float(b["Close"])
    pat = _candle(float(b["Open"]), float(b["High"]), float(b["Low"]), c)
    if pat not in ("hammer", "hammer_dn", "doji"):
        return None
    if not (r["rsi21"] < 42):
        return None
    sup = float(lows["price"].iloc[-1])
    if abs(c - sup) / sup > 0.025:
        return None
    m15 = _row(m, "ind_15m", -1)
    if not (float(m["m15_100"]["Close"].iloc[-1]) > float(m15["ema21"])):
        return None
    atr = _atr(m, i)
    stop = min(float(b["Low"]), sup) - 0.25 * atr
    highs = sw[sw["side"] == "high"]
    tgt = float(highs["price"].iloc[-1]) if len(highs) else c + 2 * atr
    if tgt <= c:
        tgt = c + 2 * (c - stop)
    return {"algo": "reversal", "side": "long", "entry": round(c, 2),
            "stop": round(stop, 2), "target": round(tgt, 2),
            "eta_days": _eta_days(m, i, tgt - c),
            "rr": round((tgt - c) / (c - stop), 2),
            "reason": f"{pat} at support {sup:.2f}, rsi "
                      f"{r['rsi21']:.0f}, 15m reclaim"}


# ---------- 4. REJECTION (extended long at resistance) ----------
def rejection_signal(m: dict, i: int = -1) -> dict | None:
    d, r = m["daily_100"], _row(m, "ind_1d", i)
    b = d.iloc[i if i >= 0 else len(d) + i]
    asof = str(d.index[i if i >= 0 else len(d) + i].date())
    sw = confirmed_swings(m, asof)
    highs = sw[sw["side"] == "high"]
    if len(highs) < 1:
        return None
    c = float(b["Close"])
    pat = _candle(float(b["Open"]), float(b["High"]), float(b["Low"]), c)
    if pat not in ("shooting_star", "shooting_star_up", "doji"):
        return None
    if not (r["rsi21"] > 58):
        return None
    res = float(highs["price"].iloc[-1])
    if abs(c - res) / res > 0.025:
        return None
    m15 = _row(m, "ind_15m", -1)
    if not (float(m["m15_100"]["Close"].iloc[-1]) < float(m15["ema21"])):
        return None
    atr = _atr(m, i)
    stop = max(float(b["High"]), res) + 0.25 * atr
    lows = sw[sw["side"] == "low"]
    tgt = float(lows["price"].iloc[-1]) if len(lows) else c - 2 * atr
    if tgt >= c:
        tgt = c - 2 * (stop - c)
    return {"algo": "rejection", "side": "short", "entry": round(c, 2),
            "stop": round(stop, 2), "target": round(tgt, 2),
            "eta_days": _eta_days(m, i, tgt - c),
            "rr": round((c - tgt) / (stop - c), 2),
            "reason": f"{pat} at resistance {res:.2f}, rsi "
                      f"{r['rsi21']:.0f}, 15m rollover"}


def scan_all(m: dict, i: int = -1) -> list:
    out = []
    for fn in (long_signal, short_signal, reversal_signal,
               rejection_signal):
        try:
            s = fn(m, i)
        except (KeyError, IndexError, ValueError):
            s = None
        if s:
            out.append(s)
    return out
