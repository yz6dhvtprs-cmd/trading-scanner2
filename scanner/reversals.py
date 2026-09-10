"""Standalone reversal routines (R1/R2), callable from the live analyzer.

R1 - 1-2-3 swing-pivot reversal (Lingrid/Trader-Vic lineage, researched
2026-09-10): existing trend by swing highs/lows; P1 = trend extreme; P2 =
corrective pullback extreme; P3 = first swing past P2 that FAILS to make a
new extreme. Trigger = daily CLOSE beyond P2 (never trade unconfirmed).
Entry = that close; SL = beyond P3 + 0.25xATR breathing room (the
aggressive standard placement: a revisit past P3 invalidates the reversal;
Lingrid's P1 placement is kept in the note/trace as the conservative
alternative). Calibrated 2026-09-10 on AAPL/MSFT/TSLA/NVDA: P1-anchored
stops sat 6-14xATR away (e.g. MSFT Buy 515/SL 345) and never resolved in
the forward window; P3-anchored risk runs 1-4xATR and is scorable.
No target. TOP (P1 high, P3 lower high) -> short bias; BOTTOM mirrors.

R2 - ATR-adaptive zigzag + TEMA(8/13/20) rollover (ATR-ZigZag lineage):
pivots confirm only on a reverse move >= ATR(14) x 3.0 from the running
extreme, alternating H/L enforced, confirmed pivots never move (no
repaint). BEARISH rollover = fresh confirmed HIGH + fast TEMA crossed below
mid (8<13) + close below TEMA20 (punched dynamic support). A full
8<13<20 sort was tried first but fires only deep into established trends,
after any pivot is fresh; the fast-cross + slow-zone punch is the
standard TEMA rollover read and fires at the breakdown.
Entry = close, SL = pivot extreme + 0.5xATR buffer, no target. BULLISH
mirrors.

Both read a prepared DAILY frame (High/Low/Close + atr; Open optional for
the P1 exhaustion-wick read) and use past bars only. Deliberately shares
NO gates with the armed Possible-Reversal logic (no RSI bands, no candle
patterns, no 15m flip). Each returns a list of signal dicts:
{state, price, stop, target:"-", note, algo, sig_key}; state names reuse
the ops vocabulary so printers/graders work unchanged.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FRACTAL_K = 2        # confirmed bars each side of a swing pivot (Williams)
R1_MIN_HEIGHT_ATR = 1.0   # P1-P2 pattern height floor (noise filter)
R1_SL_BUF_ATR = 0.25      # breathing room beyond P1 (Lingrid)
R1_BREAK_FRESH = 3        # P2 break must be within the last N closes
R1_LOOKBACK_PIVOTS = 12   # recent pivots searched for the pattern
ZZ_ATR_LEN = 14
ZZ_ATR_MULT = 3.0    # reverse move confirming a zigzag pivot
TEMA_SPANS = (8, 13, 20)
ZZ_FRESH = 20        # confirmed pivot recency, bars (~1 month daily;
                   # the breakdown often completes weeks after the extreme)
ZX_SL_ATR = 0.5      # SL buffer beyond the zigzag extreme
MIN_TEMA_SEP_ATR = 0.1  # fast/mid cross must clear this (xATR) so 1-tick
                        # flicker never triggers; breakdown crosses clear it
                        # by ~1xATR (verified on synthetic markdowns)


def fractal_pivots(df: pd.DataFrame, k: int = FRACTAL_K) -> list:
    """Confirmed swing pivots [(pos, 'H'|'L', price)], time order. A high
    pivot is the window maximum with k confirmed bars each side (low
    mirrors). Same-side neighbours merge, keeping the extreme."""
    h = df["High"].to_numpy()
    lo = df["Low"].to_numpy()
    n = len(df)
    raw = []
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max():
            raw.append((i, "H", float(h[i])))
        if lo[i] == lo[i - k:i + k + 1].min():
            raw.append((i, "L", float(lo[i])))
    out = []
    for pos, typ, px in sorted(raw):
        if out and out[-1][1] == typ:
            if typ == "H" and px <= out[-1][2]:
                continue
            if typ == "L" and px >= out[-1][2]:
                continue
            out[-1] = (pos, typ, px)
        else:
            out.append((pos, typ, px))
    return out


def _wick(df: pd.DataFrame, pos: int, side: str):
    """Exhaustion wick ratio at pos (top: upper wick; bottom: lower wick).
    Quality read for the trace; not a v1 gate."""
    try:
        o = float(df["Open"].iloc[pos])
        hh = float(df["High"].iloc[pos])
        ll = float(df["Low"].iloc[pos])
        c = float(df["Close"].iloc[pos])
        rng = hh - ll
        if rng <= 0:
            return None
        if side == "top":
            return round((hh - max(o, c)) / rng, 3)
        return round((min(o, c) - ll) / rng, 3)
    except Exception:
        return None


def _atr_last(df: pd.DataFrame):
    a = float(df["atr"].iloc[-1])
    return a if np.isfinite(a) and a > 0 else None


def r1_123(df: pd.DataFrame, trace: dict | None = None) -> list:
    """1-2-3 reversal signals on a prepared daily frame (see module doc)."""
    out = []
    tr = trace
    if len(df) < 30 or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    closes = df["Close"].to_numpy()
    last_close = float(closes[-1])
    pivs = fractal_pivots(df)[-R1_LOOKBACK_PIVOTS:]
    if tr is not None:
        tr.update({"algo": "R1", "entry": round(last_close, 2),
                   "atr": round(atr, 3),
                   "pivots": [(df.index[p].date().isoformat(), t, round(v, 2))
                              for p, t, v in pivs],
                   "checks": []})

    def log(s: str):
        if tr is not None:
            tr["checks"].append(s)

    def seek(side: str):
        """Most recent valid (P1, P2, P3) triple for side, or None."""
        is_top = side == "top"
        ext, fail = ("H", "L") if is_top else ("L", "H")
        cands = [p for p in pivs if p[1] == ext]
        for p1 in reversed(cands):
            prev = [p for p in pivs if p[1] == ext and p[0] < p1[0]]
            if not prev:
                continue
            p0 = prev[-1]
            if is_top and not p1[2] > p0[2]:
                continue  # P1 must be a NEW extreme (trend leg)
            if not is_top and not p1[2] < p0[2]:
                continue
            mids = [p for p in pivs
                    if p[1] == fail and p1[0] < p[0]]
            if not mids:
                continue
            p2 = min(mids, key=lambda p: p[2]) if is_top else \
                max(mids, key=lambda p: p[2])
            height = abs(p1[2] - p2[2])
            if height < R1_MIN_HEIGHT_ATR * atr:
                continue
            after = [p for p in pivs if p[1] == ext and p[0] > p2[0]]
            if not after:
                continue
            p3 = after[0]  # first retest attempt past P2
            if is_top and not p3[2] < p1[2]:
                continue  # retest must FAIL to make a new extreme
            if not is_top and not p3[2] > p1[2]:
                continue
            return p1, p2, p3, height
        return None

    for side in ("top", "bottom"):
        found = seek(side)
        if found is None:
            log(f"R1 {side}: no P1/P2/P3 structure in recent pivots -> skip")
            continue
        (i1, _, v1), (i2, _, v2), (i3, _, v3), height = found
        d1, d2, d3 = (df.index[i].date().isoformat() for i in (i1, i2, i3))
        wick = _wick(df, i1, side)
        if side == "top":
            trig = last_close < v2
            fresh = len(closes) > R1_BREAK_FRESH and \
                float(closes[-1 - R1_BREAK_FRESH]) >= v2
            entry, stop = last_close, v3 + R1_SL_BUF_ATR * atr
            state = "Possible upcoming Rejection"
        else:
            trig = last_close > v2
            fresh = len(closes) > R1_BREAK_FRESH and \
                float(closes[-1 - R1_BREAK_FRESH]) <= v2
            entry, stop = last_close, v3 - R1_SL_BUF_ATR * atr
            state = "Possible upcoming Reversal"
        why = (f"P1={v1:.2f}@{d1} P2={v2:.2f}@{d2} P3={v3:.2f}@{d3} "
               f"height={height / atr:.2f}xATR wick={wick}")
        if trig and fresh:
            log(f"R1 {side}: {why} break-fresh SL={stop:.2f} "
                f"(P3-anchored; P1 {v1:.2f} conservative) -> FIRE")
            out.append({
                "state": state, "price": round(entry, 2),
                "stop": round(stop, 2), "target": "-",
                "note": f"123-{side} {why}", "algo": "R1",
                "sig_key": ("R1", side, d1, d2, d3),
            })
        else:
            log(f"R1 {side}: {why} trig={trig} fresh={fresh} -> skip")
    if tr is not None:
        tr["fired"] = [r["state"] for r in out]
    return out


def tema(s: pd.Series, span: int) -> pd.Series:
    """Triple EMA: 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA))."""
    e1 = s.ewm(span=span, adjust=False).mean()
    e2 = e1.ewm(span=span, adjust=False).mean()
    e3 = e2.ewm(span=span, adjust=False).mean()
    return 3 * e1 - 3 * e2 + e3


def atr_zigzag(df: pd.DataFrame, atr_len: int = ZZ_ATR_LEN,
               mult: float = ZZ_ATR_MULT) -> list:
    """Confirmed zigzag pivots [(pos, 'H'|'L', price)], time order. A pivot
    confirms only when price reverses by >= ATR x mult from the running
    extreme (penetration-based, causal); H/L strictly alternate, so
    confirmed pivots never move."""
    h = df["High"].to_numpy()
    lo = df["Low"].to_numpy()
    atr = df["atr"].to_numpy() if "atr" in df.columns else \
        np.full(len(df), np.nan)
    n = len(df)
    pivots = []
    mode = "UP"  # last confirmed (seed) treated as a low at bar 0
    ext, ext_i = h[0], 0
    for i in range(1, n):
        thr = atr[i] * mult
        if not np.isfinite(thr) or thr <= 0:
            continue
        if mode == "UP":
            if h[i] > ext:
                ext, ext_i = h[i], i
            if lo[i] <= ext - thr:
                pivots.append((ext_i, "H", float(ext)))
                mode, ext, ext_i = "DN", lo[i], i
        else:
            if lo[i] < ext:
                ext, ext_i = lo[i], i
            if h[i] >= ext + thr:
                pivots.append((ext_i, "L", float(ext)))
                mode, ext, ext_i = "UP", h[i], i
    return pivots


R2_MIN_BARS = 65  # ~3x longest TEMA span so TEMA20 has stabilized


def r2_zigzag_tema(df: pd.DataFrame, trace: dict | None = None) -> list:
    """ATR-zigzag + TEMA rollover signals (see module doc)."""
    out = []
    tr = trace
    if len(df) < R2_MIN_BARS or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    c = df["Close"]
    last_close = float(c.iloc[-1])
    t8, t13, t20 = (float(tema(c, s).iloc[-1]) for s in TEMA_SPANS)
    if not all(np.isfinite(v) for v in (t8, t13, t20)):
        return out
    bull_stack = t8 > t13 > t20
    bear_stack = t8 < t13 < t20
    sep = t8 - t13
    bull_cross = sep >= MIN_TEMA_SEP_ATR * atr
    bear_cross = -sep >= MIN_TEMA_SEP_ATR * atr
    pivs = atr_zigzag(df)
    last = pivs[-1] if pivs else None
    fresh = last is not None and (len(df) - 1 - last[0]) <= ZZ_FRESH
    if tr is not None:
        tr.update({
            "algo": "R2", "entry": round(last_close, 2),
            "atr": round(atr, 3),
            "tema": {"8": round(t8, 2), "13": round(t13, 2),
                     "20": round(t20, 2),
                     "stack": "bull" if bull_stack else
                     "bear" if bear_stack else "mixed"},
            "pivots": [(df.index[p].date().isoformat(), t, round(v, 2))
                       for p, t, v in pivs[-6:]],
            "checks": [],
        })

    def log(s: str):
        if tr is not None:
            tr["checks"].append(s)

    if last is None or not fresh:
        log(f"R2: no fresh confirmed pivot (last={last}) -> skip")
    elif last[1] == "H" and bear_cross and last_close < t20:
        stop = last[2] + ZX_SL_ATR * atr
        dp = df.index[last[0]].date().isoformat()
        log(f"R2 bear: confirmed HIGH {last[2]:.2f}@{dp}, TEMA8 {t8:.2f}"
            f"<TEMA13 {t13:.2f}, close {last_close:.2f}<TEMA20 {t20:.2f} "
            f"-> FIRE")
        out.append({
            "state": "Possible upcoming Rejection", "price": round(last_close, 2),
            "stop": round(stop, 2), "target": "-",
            "note": f"zz-top {last[2]:.2f}@{dp} TEMA8/13/20 "
                    f"{t8:.2f}/{t13:.2f}/{t20:.2f}", "algo": "R2",
            "sig_key": ("R2", "top", dp, round(last[2], 2)),
        })
    elif last[1] == "L" and bull_cross and last_close > t20:
        stop = last[2] - ZX_SL_ATR * atr
        dp = df.index[last[0]].date().isoformat()
        log(f"R2 bull: confirmed LOW {last[2]:.2f}@{dp}, TEMA8 {t8:.2f}"
            f">TEMA13 {t13:.2f}, close {last_close:.2f}>TEMA20 {t20:.2f} "
            f"-> FIRE")
        out.append({
            "state": "Possible upcoming Reversal", "price": round(last_close, 2),
            "stop": round(stop, 2), "target": "-",
            "note": f"zz-bottom {last[2]:.2f}@{dp} TEMA8/13/20 "
                    f"{t8:.2f}/{t13:.2f}/{t20:.2f}", "algo": "R2",
            "sig_key": ("R2", "bottom", dp, round(last[2], 2)),
        })
    else:
        log(f"R2: last={last}, stack="
            f"{'bull' if bull_stack else 'bear' if bear_stack else 'mixed'}, "
            f"close={last_close:.2f} vs TEMA20={t20:.2f} -> skip")
    if tr is not None:
        tr["fired"] = [r["state"] for r in out]
    return out
