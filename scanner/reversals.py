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


def _sma50(df: pd.DataFrame):
    """sma50 series or None (needs 50+ bars of warmup)."""
    if "sma50" not in df.columns or len(df) < 55:
        return None
    s = df["sma50"]
    if not np.isfinite(float(s.iloc[-1])):
        return None
    return s


def _recent_pivots(df: pd.DataFrame, side: str, nbar: int):
    """Confirmed fractal pivots of side within the last nbar bars."""
    pivs = fractal_pivots(df)
    return [p for p in pivs if p[1] == side and (len(df) - 1 - p[0]) <= nbar]


def _line_val(p1, p2, at: int) -> float:
    """Extrapolated value at bar position `at` of the line through two
    (pos, price) pivots."""
    (i1, v1), (i2, v2) = (p1[0], p1[2]), (p2[0], p2[2])
    if i2 == i1:
        return v2
    return v1 + (v2 - v1) * (at - i1) / (i2 - i1)


def _trendline(df: pd.DataFrame, side: str):
    """Objective trendline through the last two same-side fractal pivots.
    side 'up' (higher-low line, needs ascending lows) or 'dn' (descending
    highs). Returns (p1, p2) or None; latest anchor must be <=30 bars old."""
    want = "L" if side == "up" else "H"
    cands = [p for p in fractal_pivots(df) if p[1] == want]
    if len(cands) < 2:
        return None
    p1, p2 = cands[-2], cands[-1]
    if len(df) - 1 - p2[0] > 30:
        return None
    if side == "up" and not p2[2] > p1[2]:
        return None
    if side == "dn" and not p2[2] < p1[2]:
        return None
    return p1, p2


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


# --------------------------------------------------------------------------
# R3-R10: the remaining reversal tools from Trading Setups Review's "9 tools
# trend traders can use to find reversals" (researched 2026-09-10). Same
# signal contract as R1/R2: {state, price, stop, target:"-", note, algo,
# sig_key}, daily frame in, past bars only, no shared gates with the armed
# Possible-Reversal logic.
# --------------------------------------------------------------------------

R3_FILL_ATR = 0.1  # entry sits this far past the line (conservative
                  # fill on the crossed path, not the chase-close)
R3_BREAK_ATR = 0.5  # trend-line break magnitude (the page: false breaks are
                    # common; the magnitude of the break is the key)


def _r3_entry(df: pd.DataFrame, side: str, lv: float, close: float,
              atr: float) -> tuple:
    """(entry, fill): line-cross fill gated on recent trade. The break bar
    normally crosses the line, so entry = line -/+ 0.1xATR (earlier and
    closer to the turn than the chase-close). But late detection (anchors
    completing after a runaway move) can leave the line outside anything
    traded in the last 5 bars -> then entry falls back to the close."""
    raw = lv - R3_FILL_ATR * atr if side == "up" else lv + R3_FILL_ATR * atr
    rng_lo = float(df["Low"].iloc[-5:].min())
    rng_hi = float(df["High"].iloc[-5:].max())
    if rng_lo <= raw <= rng_hi:
        return raw, "line"
    return close, "close"


def _r3_stop(df: pd.DataFrame, side: str, entry: float,
             atr: float) -> tuple:
    """(stop, source): structural stop guaranteed on the valid side of
    `entry`. Prefers the recent swing pivot; falls back to the 10-bar
    extreme when the pivot is stale (a crash can leave every recent
    pivot on the wrong side of entry); then entry -/+ 0.5xATR; and a
    0.5xATR minimum-risk floor so a stop printing on top of entry can
    never manufacture a lottery win."""
    buf = R1_SL_BUF_ATR * atr
    if side == "up":  # short: stop must sit OVER entry
        hs = _recent_pivots(df, "H", 30)
        cand = [hs[-1][2] + buf if hs else float("-inf")]
        cand.append(float(df["High"].iloc[-10:].max()) + buf)
        cand.append(entry + 0.5 * atr)
        stop = next(s for s in cand if s > entry)
        src = ("pivot" if stop == cand[0] else "hi10"
               if stop == cand[1] else "atr")
    else:  # long: stop must sit UNDER entry
        ls = _recent_pivots(df, "L", 30)
        cand = [ls[-1][2] - buf if ls else float("inf")]
        cand.append(float(df["Low"].iloc[-10:].min()) - buf)
        cand.append(entry - 0.5 * atr)
        stop = next(s for s in cand if s < entry)
        src = ("pivot" if stop == cand[0] else "lo10"
               if stop == cand[1] else "atr")
    if abs(entry - stop) < 0.5 * atr:  # degenerate pinch: enforce min risk
        stop = entry + 0.5 * atr if side == "up" else entry - 0.5 * atr
        src = "minrisk"
    return stop, src


def r3_trendline(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R3 - trend-line break (tool #2). Objective line through the last two
    same-side fractal pivots; reversal = CLOSE beyond the line by
    >= 0.5xATR. ENTRY is the line cross (line -/+ a 0.1xATR fill buffer),
    not the chase-close: the break bar necessarily traded through the
    line, so the fill is earlier and closer to the turn. Short breaks
    the uptrend line (SL = recent swing high); long breaks the downtrend
    line (SL = recent swing low), both clamped to the valid side."""
    out = []
    if len(df) < 30 or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    last = len(df) - 1
    close = float(df["Close"].iloc[-1])
    tr = _new_trace(trace, "R3", close, atr)
    for side, state in (("up", "Possible upcoming Rejection"),
                        ("dn", "Possible upcoming Reversal")):
        tl = _trendline(df, side)
        if tl is None:
            _log(tr, f"R3 {side}: no valid trendline -> skip")
            continue
        p1, p2 = tl
        lv = _line_val(p1, p2, last)
        d1 = df.index[p1[0]].date().isoformat()
        d2 = df.index[p2[0]].date().isoformat()
        if side == "up":
            dist = (lv - close) / atr
            if close < lv - R3_BREAK_ATR * atr:
                entry, fill = _r3_entry(df, side, lv, close, atr)
                stop, src = _r3_stop(df, side, entry, atr)
                _log(tr, f"R3 up-break: close {close:.2f} under line "
                         f"{lv:.2f} by {dist:.2f}xATR -> FIRE @{entry:.2f}")
                out.append(_sig(state, entry, stop, "R3",
                                f"tl-break P1={p1[2]:.2f}@{d1} "
                                f"P2={p2[2]:.2f}@{d2} fill={fill} sl={src}",
                                ("R3", "top", d1, d2)))
            else:
                _log(tr, f"R3 up-line {lv:.2f}: close {close:.2f} "
                         f"({dist:+.2f}xATR, need <-0.5) -> skip")
        else:
            dist = (close - lv) / atr
            if close > lv + R3_BREAK_ATR * atr:
                entry, fill = _r3_entry(df, side, lv, close, atr)
                stop, src = _r3_stop(df, side, entry, atr)
                _log(tr, f"R3 dn-break: close {close:.2f} over line "
                         f"{lv:.2f} by {dist:.2f}xATR -> FIRE @{entry:.2f}")
                out.append(_sig(state, entry, stop, "R3",
                                f"tl-break P1={p1[2]:.2f}@{d1} "
                                f"P2={p2[2]:.2f}@{d2} fill={fill} sl={src}",
                                ("R3", "bottom", d1, d2)))
            else:
                _log(tr, f"R3 dn-line {lv:.2f}: close {close:.2f} "
                         f"({dist:+.2f}xATR, need >+0.5) -> skip")
    _fired(tr, out)
    return out


def _channel(df: pd.DataFrame, side: str):
    """Trendline + parallel through the extreme between its anchors.
    Returns (p1, p2, top_fn) with top_fn(i) = channel-top value at bar i,
    or None. side 'up' watches the top (shorts), 'dn' the bottom (longs)."""
    tl = _trendline(df, side)
    if tl is None:
        return None
    p1, p2 = tl
    if side == "up":
        seg = df.iloc[p1[0]:p2[0] + 1]
        ext = max(float(seg["High"].max()) - _line_val(p1, p2, i)
                  for i in range(p1[0], p2[0] + 1))
        top = lambda i: _line_val(p1, p2, i) + ext  # noqa: E731
    else:
        seg = df.iloc[p1[0]:p2[0] + 1]
        ext = max(_line_val(p1, p2, i) - float(seg["Low"].min())
                  for i in range(p1[0], p2[0] + 1))
        top = lambda i: _line_val(p1, p2, i) - ext  # noqa: E731
    return p1, p2, top


def r4_channel(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R4 - channel overshoot + trend-line-break confirm (tool #3, the
    page's balanced approach: overshoot warns, break confirms). BEARISH:
    a bar in the last 5 overshoots the channel top by >=0.25xATR, then
    CLOSE breaks back under the trendline by >=0.25xATR. SL = overshoot
    extreme + buffer (invalidation = new high). BULLISH mirrors."""
    out = []
    if len(df) < 30 or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    last = len(df) - 1
    close = float(df["Close"].iloc[-1])
    tr = _new_trace(trace, "R4", close, atr)
    for side, state in (("up", "Possible upcoming Rejection"),
                        ("dn", "Possible upcoming Reversal")):
        ch = _channel(df, side)
        if ch is None:
            _log(tr, f"R4 {side}: no channel -> skip")
            continue
        p1, p2, edge = ch
        d1 = df.index[p1[0]].date().isoformat()
        d2 = df.index[p2[0]].date().isoformat()
        line_now = _line_val(p1, p2, last)
        over = None
        for i in range(max(0, last - 4), last + 1):
            px = float(df["High"].iloc[i]) if side == "up" else \
                float(df["Low"].iloc[i])
            dev = (px - edge(i)) / atr if side == "up" else \
                (edge(i) - px) / atr
            if dev >= 0.25:
                over = (i, px, dev)
        if over is None:
            _log(tr, f"R4 {side}: no channel overshoot in 5 bars -> skip")
            continue
        oi, opx, odev = over
        od = df.index[oi].date().isoformat()
        if side == "up":
            if close < line_now - 0.25 * atr:
                stop = opx + R1_SL_BUF_ATR * atr
                _log(tr, f"R4 up: overshoot {opx:.2f}@{od} "
                         f"(+{odev:.2f}xATR), close {close:.2f} under line "
                         f"{line_now:.2f} -> FIRE")
                out.append(_sig(state, close, stop, "R4",
                                f"chan-over {opx:.2f}@{od} line "
                                f"{line_now:.2f}", ("R4", "top", od)))
            else:
                _log(tr, f"R4 up: overshoot {opx:.2f}@{od} warns, no line "
                         f"break (close {close:.2f} vs {line_now:.2f}) -> skip")
        else:
            if close > line_now + 0.25 * atr:
                stop = opx - R1_SL_BUF_ATR * atr
                _log(tr, f"R4 dn: undershoot {opx:.2f}@{od} "
                         f"(+{odev:.2f}xATR), close {close:.2f} over line "
                         f"{line_now:.2f} -> FIRE")
                out.append(_sig(state, close, stop, "R4",
                                f"chan-under {opx:.2f}@{od} line "
                                f"{line_now:.2f}", ("R4", "bottom", od)))
            else:
                _log(tr, f"R4 dn: undershoot {opx:.2f}@{od} warns, no line "
                         f"break (close {close:.2f} vs {line_now:.2f}) -> skip")
    _fired(tr, out)
    return out


def _slope_flip(sma: pd.Series, direction: str, window: int = 5):
    """Most recent bar position where the 5-bar SMA slope flipped to
    `direction` ('dn'/'up') within the window, else None. The flip bar
    anchors the signal key so a persisting flip fires once, not daily."""
    last = len(sma) - 1
    want_dn = direction == "dn"
    for i in range(last, max(last - window, 5), -1):
        try:
            now = float(sma.iloc[i]) - float(sma.iloc[i - 5])
            prv = float(sma.iloc[i - 1]) - float(sma.iloc[i - 6])
        except (IndexError, ValueError):
            return None
        if want_dn and now < 0 <= prv:
            return i
        if not want_dn and now > 0 >= prv:
            return i
    return None


def r5_maslope(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R5 - moving-average direction flip (tool #4; author's SMA50, 5-bar
    slope). BEARISH: SMA50 slope freshly negative (flipped within 5 bars)
    with close under the average. SL = 10-bar high + buffer. BULLISH
    mirrors. Signal keys on the flip bar (one setup per flip)."""
    out = []
    sma = _sma50(df)
    if sma is None or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    last = len(df) - 1
    close = float(df["Close"].iloc[-1])
    ma = float(sma.iloc[-1])
    tr = _new_trace(trace, "R5", close, atr)
    slope = ma - float(sma.iloc[-6])
    flip_dn = _slope_flip(sma, "dn")
    if flip_dn is not None and close < ma:
        stop = float(df["High"].iloc[-10:].max()) + R1_SL_BUF_ATR * atr
        fd = df.index[flip_dn].date().isoformat()
        _log(tr, f"R5 bear: SMA50 {ma:.2f} slope {slope / atr:+.2f}xATR/5b "
             f"flipped @{fd}, close {close:.2f} under -> FIRE")
        out.append(_sig("Possible upcoming Rejection", close, stop, "R5",
                        f"sma50-down {ma:.2f}", ("R5", "top", fd)))
    else:
        _log(tr, f"R5 bear: slope {slope / atr:+.2f}xATR/5b, flip={flip_dn}, "
             f"close {close:.2f} vs SMA50 {ma:.2f} -> skip")
    flip_up = _slope_flip(sma, "up")
    if flip_up is not None and close > ma:
        stop = float(df["Low"].iloc[-10:].min()) - R1_SL_BUF_ATR * atr
        fd = df.index[flip_up].date().isoformat()
        _log(tr, f"R5 bull: SMA50 {ma:.2f} slope {slope / atr:+.2f}xATR/5b "
             f"flipped @{fd}, close {close:.2f} over -> FIRE")
        out.append(_sig("Possible upcoming Reversal", close, stop, "R5",
                        f"sma50-up {ma:.2f}", ("R5", "bottom", fd)))
    else:
        _log(tr, f"R5 bull: slope {slope / atr:+.2f}xATR/5b, flip={flip_up}, "
             f"close {close:.2f} vs SMA50 {ma:.2f} -> skip")
    _fired(tr, out)
    return out


def r6_donchian(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R6 - Donchian 20-day extreme break against the regime (tool #5,
    Turtle S1 lookback). BEARISH reversal: regime was UP (SMA50 rising over
    10 bars) and CLOSE breaks under the 20-day low. SL = 10-bar high +
    buffer. BULLISH: regime was DOWN and CLOSE breaks over the 20-day
    high; SL = 10-bar low - buffer. (hi20/lo20 are pre-shifted: no peek.)"""
    out = []
    sma = _sma50(df)
    if sma is None or "atr" not in df.columns or "hi20" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    last = len(df) - 1
    close = float(df["Close"].iloc[-1])
    hi20, lo20 = float(df["hi20"].iloc[-1]), float(df["lo20"].iloc[-1])
    reg = float(sma.iloc[-1]) - float(sma.iloc[-11])
    tr = _new_trace(trace, "R6", close, atr)
    if reg > 0 and close < lo20:
        stop = float(df["High"].iloc[-10:].max()) + R1_SL_BUF_ATR * atr
        _log(tr, f"R6 bear: regime UP (SMA50 +{reg / atr:.2f}xATR/10b), "
             f"close {close:.2f} under 20d-low {lo20:.2f} -> FIRE")
        out.append(_sig("Possible upcoming Rejection", close, stop, "R6",
                        f"donchian-break 20d-low {lo20:.2f}",
                        ("R6", "top", round(lo20, 2))))
    else:
        _log(tr, f"R6 bear: regime {reg / atr:+.2f}xATR/10b, close "
             f"{close:.2f} vs 20d-low {lo20:.2f} -> skip")
    if reg < 0 and close > hi20:
        stop = float(df["Low"].iloc[-10:].min()) - R1_SL_BUF_ATR * atr
        _log(tr, f"R6 bull: regime DN (SMA50 {reg / atr:.2f}xATR/10b), "
             f"close {close:.2f} over 20d-high {hi20:.2f} -> FIRE")
        out.append(_sig("Possible upcoming Reversal", close, stop, "R6",
                        f"donchian-break 20d-high {hi20:.2f}",
                        ("R6", "bottom", round(hi20, 2))))
    else:
        _log(tr, f"R6 bull: regime {reg / atr:+.2f}xATR/10b, close "
             f"{close:.2f} vs 20d-high {hi20:.2f} -> skip")
    _fired(tr, out)
    return out


def _divergence(df: pd.DataFrame, osc: pd.Series, side: str,
                points: int = 3):
    """Last-`points` price swing pivots vs oscillator values at those bars.
    Bearish: ascending price highs + descending osc. Bullish mirrors.
    Returns (pivots, ovals) or None."""
    want = "H" if side == "bear" else "L"
    cands = [p for p in fractal_pivots(df) if p[1] == want][-points:]
    if len(cands) < points:
        return None
    px = [p[2] for p in cands]
    try:
        ov = [float(osc.iloc[p[0]]) for p in cands]
    except Exception:
        return None
    if not all(np.isfinite(v) for v in ov):
        return None
    if side == "bear":
        ok = all(b > a for a, b in zip(px, px[1:])) and \
            all(b < a for a, b in zip(ov, ov[1:]))
    else:
        ok = all(b < a for a, b in zip(px, px[1:])) and \
            all(b > a for a, b in zip(ov, ov[1:]))
    return (cands, ov) if ok else None


def _crossed(df: pd.DataFrame, fast: str, slow: str, window: int = 5,
             direction: str = "down") -> bool:
    """fast crossed slow within the last `window` bars (causal)."""
    f = df[fast].to_numpy()[-window - 1:]
    s = df[slow].to_numpy()[-window - 1:]
    if not all(np.isfinite(v) for v in list(f) + list(s)):
        return False
    d = f - s
    if direction == "down":
        return bool(d[-1] < 0 and (d[:-1] > 0).any())
    return bool(d[-1] > 0 and (d[:-1] < 0).any())


def r7_macddiv(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R7 - MACD 3-point divergence + cross trigger (tool #6; the page:
    two points define it, three improve quality). BEARISH: 3 ascending
    price highs with descending MACD-line highs, then MACD crosses under
    its signal within 5 bars. SL = 3rd extreme + buffer. BULLISH mirrors."""
    out = []
    if len(df) < 60 or "atr" not in df.columns or \
            "macd" not in df.columns or "macd_sig" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    close = float(df["Close"].iloc[-1])
    tr = _new_trace(trace, "R7", close, atr)
    div = _divergence(df, df["macd"], "bear", 3)
    if div is not None and _crossed(df, "macd", "macd_sig", 5, "down"):
        pivs, _ = div
        ext = pivs[-1][2]
        dd = [df.index[p[0]].date().isoformat() for p in pivs]
        stop = ext + R1_SL_BUF_ATR * atr
        _log(tr, f"R7 bear: 3-pt divergence "
             f"{[round(p[2], 2) for p in pivs]} + MACD cross down -> FIRE")
        out.append(_sig("Possible upcoming Rejection", close, stop, "R7",
                        f"macd-3div {ext:.2f}", ("R7", "top", *dd)))
    else:
        _log(tr, "R7 bear: no 3-pt MACD divergence + fresh cross -> skip")
    div = _divergence(df, df["macd"], "bull", 3)
    if div is not None and _crossed(df, "macd", "macd_sig", 5, "up"):
        pivs, _ = div
        ext = pivs[-1][2]
        dd = [df.index[p[0]].date().isoformat() for p in pivs]
        stop = ext - R1_SL_BUF_ATR * atr
        _log(tr, f"R7 bull: 3-pt divergence "
             f"{[round(p[2], 2) for p in pivs]} + MACD cross up -> FIRE")
        out.append(_sig("Possible upcoming Reversal", close, stop, "R7",
                        f"macd-3div {ext:.2f}", ("R7", "bottom", *dd)))
    else:
        _log(tr, "R7 bull: no 3-pt MACD divergence + fresh cross -> skip")
    _fired(tr, out)
    return out


def _obv(df: pd.DataFrame) -> pd.Series | None:
    try:
        chg = df["Close"].diff().fillna(0.0)
        return (np.sign(chg) * df["Volume"]).cumsum()
    except Exception:
        return None


def r8_obv(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R8 - OBV losing steam at the extreme (tool #7; the page: ignore OBV
    values, watch direction, read it through a long-term MA). BEARISH:
    fresh 20-day price high (<=3 bars) with OBV under its EMA21. SL =
    that high + buffer. BULLISH: fresh 20-day low with OBV over its
    EMA21; SL = that low - buffer."""
    out = []
    if len(df) < 60 or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    obv = _obv(df)
    if obv is None:
        return out
    oe = obv.ewm(span=21, adjust=False).mean()
    if not np.isfinite(float(oe.iloc[-1])):
        return out
    last = len(df) - 1
    close = float(df["Close"].iloc[-1])
    tr = _new_trace(trace, "R8", close, atr)
    hi = df["High"].to_numpy()[-20:]
    lo = df["Low"].to_numpy()[-20:]
    eb_h, eb_l = int(np.argmax(hi)), int(np.argmin(lo))
    age_h, age_l = 19 - eb_h, 19 - eb_l
    steam_dn = float(obv.iloc[-1]) < float(oe.iloc[-1])
    steam_up = float(obv.iloc[-1]) > float(oe.iloc[-1])
    if age_h <= 3 and steam_dn:
        ext = float(hi[eb_h])
        dd = df.index[last - age_h].date().isoformat()
        stop = ext + R1_SL_BUF_ATR * atr
        _log(tr, f"R8 bear: fresh 20d-high {ext:.2f}@{dd}, OBV under EMA21 "
             f"-> FIRE")
        out.append(_sig("Possible upcoming Rejection", close, stop, "R8",
                        f"obv-fade 20d-high {ext:.2f}", ("R8", "top", dd)))
    else:
        _log(tr, f"R8 bear: 20d-high age {age_h}b, OBV steam "
             f"{'dn' if steam_dn else 'up'} -> skip")
    if age_l <= 3 and steam_up:
        ext = float(lo[eb_l])
        dd = df.index[last - age_l].date().isoformat()
        stop = ext - R1_SL_BUF_ATR * atr
        _log(tr, f"R8 bull: fresh 20d-low {ext:.2f}@{dd}, OBV over EMA21 "
             f"-> FIRE")
        out.append(_sig("Possible upcoming Reversal", close, stop, "R8",
                        f"obv-fade 20d-low {ext:.2f}", ("R8", "bottom", dd)))
    else:
        _log(tr, f"R8 bull: 20d-low age {age_l}b, OBV steam "
             f"{'up' if steam_up else 'dn'} -> skip")
    _fired(tr, out)
    return out


def _vol_bb(df: pd.DataFrame, n: int = 20, k: float = 2.0):
    v = df["Volume"]
    mv = v.rolling(n).mean()
    sd = v.rolling(n).std()
    return mv + k * sd


def r9_climax(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R9 - climactic volume + failure (tool #9; objective extreme via
    Bollinger Bands on volume). BEARISH: uptrend (close > SMA50) with a
    climax bar (volume > volBB upper) in the last 4, then CLOSE back under
    the climax low (buyers failed). SL = climax high + buffer. BULLISH:
    downtrend + climax + close back over the climax high; SL = climax low
    - buffer."""
    out = []
    sma = _sma50(df)
    if sma is None or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    last = len(df) - 1
    close = float(df["Close"].iloc[-1])
    tr = _new_trace(trace, "R9", close, atr)
    upper = _vol_bb(df)
    clim = None
    for i in range(max(0, last - 4), last + 1):
        try:
            if float(df["Volume"].iloc[i]) > float(upper.iloc[i]):
                clim = i
        except Exception:
            continue
    if clim is None:
        _log(tr, "R9: no climax-volume bar in 5 bars -> skip")
    elif float(sma.iloc[-1]) < close and \
            close < float(df["Low"].iloc[clim]):
        ch, cl = float(df["High"].iloc[clim]), float(df["Low"].iloc[clim])
        dd = df.index[clim].date().isoformat()
        stop = ch + R1_SL_BUF_ATR * atr
        _log(tr, f"R9 bear: climax {ch:.2f}/{cl:.2f}@{dd} in uptrend, "
             f"close {close:.2f} under climax low -> FIRE")
        out.append(_sig("Possible upcoming Rejection", close, stop, "R9",
                        f"climax-fail {cl:.2f}@{dd}", ("R9", "top", dd)))
    elif float(sma.iloc[-1]) > close and \
            close > float(df["High"].iloc[clim]):
        ch, cl = float(df["High"].iloc[clim]), float(df["Low"].iloc[clim])
        dd = df.index[clim].date().isoformat()
        stop = cl - R1_SL_BUF_ATR * atr
        _log(tr, f"R9 bull: climax {ch:.2f}/{cl:.2f}@{dd} in downtrend, "
             f"close {close:.2f} over climax high -> FIRE")
        out.append(_sig("Possible upcoming Reversal", close, stop, "R9",
                        f"climax-fail {ch:.2f}@{dd}", ("R9", "bottom", dd)))
    else:
        _log(tr, f"R9: climax @{df.index[clim].date().isoformat()} "
             f"unconfirmed (close {close:.2f}) -> skip")
    _fired(tr, out)
    return out


def _vol_osc(df: pd.DataFrame, short: int = 5, long: int = 20) -> pd.Series | None:
    try:
        fast = df["Volume"].rolling(short).mean()
        slow = df["Volume"].rolling(long).mean()
        return (fast - slow) / slow * 100.0
    except Exception:
        return None


def r10_volosc(df: pd.DataFrame, trace: dict | None = None) -> list:
    """R10 - Volume Oscillator divergence (tool #8; VO>0 = healthy trend
    either way, VO<0 = weak). BEARISH: ascending price swing highs with
    descending VO (2-pt), VO now negative. SL = higher high + buffer.
    BULLISH: descending price lows with ascending VO, VO negative... (weak
    downtrend losing sellers). SL = lower low - buffer."""
    out = []
    if len(df) < 60 or "atr" not in df.columns:
        return out
    atr = _atr_last(df)
    if atr is None:
        return out
    vo = _vol_osc(df)
    if vo is None or not np.isfinite(float(vo.iloc[-1])):
        return out
    close = float(df["Close"].iloc[-1])
    tr = _new_trace(trace, "R10", close, atr)
    div = _divergence(df, vo, "bear", 2)
    if div is not None and float(vo.iloc[-1]) < 0:
        pivs, _ = div
        ext = pivs[-1][2]
        dd = [df.index[p[0]].date().isoformat() for p in pivs]
        stop = ext + R1_SL_BUF_ATR * atr
        _log(tr, f"R10 bear: HH {pivs[0][2]:.2f}->{ext:.2f} with VO "
             f"fading, VO now {float(vo.iloc[-1]):.1f} -> FIRE")
        out.append(_sig("Possible upcoming Rejection", close, stop, "R10",
                        f"vo-div {ext:.2f}", ("R10", "top", *dd)))
    else:
        _log(tr, "R10 bear: no HH+VO-fade with VO<0 -> skip")
    div = _divergence(df, vo, "bull", 2)
    if div is not None and float(vo.iloc[-1]) < 0:
        pivs, _ = div
        ext = pivs[-1][2]
        dd = [df.index[p[0]].date().isoformat() for p in pivs]
        stop = ext - R1_SL_BUF_ATR * atr
        _log(tr, f"R10 bull: LL {pivs[0][2]:.2f}->{ext:.2f} with VO "
             f"rising, VO now {float(vo.iloc[-1]):.1f} -> FIRE")
        out.append(_sig("Possible upcoming Reversal", close, stop, "R10",
                        f"vo-div {ext:.2f}", ("R10", "bottom", *dd)))
    else:
        _log(tr, "R10 bull: no LL+VO-rise with VO<0 -> skip")
    _fired(tr, out)
    return out


def _new_trace(trace, algo: str, close: float, atr: float) -> dict | None:
    if trace is None:
        return None
    trace.update({"algo": algo, "entry": round(close, 2),
                  "atr": round(atr, 3), "checks": []})
    return trace


def _log(tr, s: str):
    if tr is not None:
        tr["checks"].append(s)


def _sig(state: str, price: float, stop: float, algo: str, note: str,
         sig_key: tuple) -> dict:
    return {"state": state, "price": round(price, 2),
            "stop": round(stop, 2), "target": "-", "note": note,
            "algo": algo, "sig_key": sig_key}


def _fired(tr, out: list):
    if tr is not None:
        tr["fired"] = [r["state"] for r in out]


REV_ALGOS = {
    "R1": r1_123,
    "R2": r2_zigzag_tema,
    "R3": r3_trendline,
    "R4": r4_channel,
    "R5": r5_maslope,
    "R6": r6_donchian,
    "R7": r7_macddiv,
    "R8": r8_obv,
    "R9": r9_climax,
    "R10": r10_volosc,
}
