"""Offline checks for the standalone reversal routines (no network).

R1 (1-2-3 swing pivots) and R2 (ATR-zigzag + TEMA cross/zone punch) run on
synthetic daily frames with a constant atr column. Gate CONTENT for the
armed analyzer stays covered by scanner/test_analyze.py; the backtest
harness around these routines is covered by test_backtest_analyzer.py.

Usage (project root, venv active):
    python scanner/test_reversals.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from reversals import (atr_zigzag, fractal_pivots, r1_123,  # noqa: E402
                       r2_zigzag_tema, tema, r3_trendline, r4_channel,
                       r5_maslope, r6_donchian, r7_macddiv, r8_obv,
                       r9_climax, r10_volosc, _r3_stop)

ET = "America/New_York"


def _daily(n: int, close, high=None, low=None, op=None, atr=1.0,
           start: str = "2026-01-05") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n, tz=ET)
    cl = np.array(close, float)
    d = pd.DataFrame({
        "Open": np.array(op, float) if op is not None else cl - 0.1,
        "High": np.array(high, float) if high is not None else cl + 0.3,
        "Low": np.array(low, float) if low is not None else cl - 0.5,
        "Close": cl, "Volume": 1_000_000.0}, index=idx)
    d["atr"] = atr
    return d


def t_fractal_pivots():
    # V with apex at bar 5, troughs at bars 0/10 (k=2 needs 2 flank bars)
    cl = [100, 102, 104, 106, 108, 110, 108, 106, 104, 102, 100]
    d = _daily(len(cl), cl)
    pivs = fractal_pivots(d, k=2)
    assert (5, "H", 110.3) in [(p, t, round(v, 1)) for p, t, v in pivs], pivs
    # monotonic run: no interior pivots at all
    d2 = _daily(10, list(np.linspace(100, 109, 10)))
    assert fractal_pivots(d2, k=2) == []


def _r1_bottom():
    n = 50
    cl = list(np.linspace(110, 101, 20))
    cl[8:12] = [106.5, 107.3, 107.7, 107.1]  # mid-trend bounce (structure)
    cl += [98.3]                              # 20: P1 extreme low
    cl += list(np.linspace(99.0, 103.9, 10))  # 21-30: rally, top 103.9
    cl += list(np.linspace(103.0, 100.5, 8))  # 31-38: dip, P3 100.5
    cl += [100.8, 101.5, 102.2, 102.9, 103.5, 103.8,  # 39-44
           104.3, 103.8, 104.9, 105.0, 105.2]        # 45-49: break
    hi = [c + 0.3 for c in cl]
    lo = [c - 0.5 for c in cl]
    hi[20], lo[20] = 98.8, 98.0     # P1 bar
    hi[30], lo[30] = 104.0, 103.0   # P2 bar (rally top)
    hi[38], lo[38] = 101.0, 100.5   # P3 bar (higher low)
    op = [c - 0.1 for c in cl]
    op[20], cl[20] = 99.0, 98.3
    return _daily(n, cl, hi, lo, op)


def t_r1_bottom_fires():
    d = _r1_bottom()
    tr: dict = {}
    res = r1_123(d, trace=tr)
    assert len(res) == 1, res
    r = res[0]
    assert r["state"] == "Possible upcoming Reversal", r
    assert r["algo"] == "R1" and r["target"] == "-"
    assert r["price"] == 105.2, r  # entry = confirming close
    assert r["stop"] == 100.05, r  # P3-anchored: 100.30 - 0.25xATR
    assert r["sig_key"][0] == "R1" and r["sig_key"][1] == "bottom"
    assert tr["fired"] == ["Possible upcoming Reversal"]
    fire = [c for c in tr["checks"] if "FIRE" in c][0]
    # P3 is the true fractal low (bar 39 dips under bar 38), not the
    # hand-placed bar: the routine follows confirmed pivots, not intent
    assert "P1=98.00" in fire and "P2=104.00" in fire and \
        "P3=100.30" in fire, fire
    assert "wick=" in fire


def t_r1_bottom_no_trigger():
    d = _r1_bottom()
    d.loc[d.index[-1], "Close"] = 103.5  # never closes beyond P2=104
    d.loc[d.index[-2], "Close"] = 103.0
    tr: dict = {}
    assert r1_123(d, trace=tr) == []
    assert tr["fired"] == []
    assert any("trig=False" in c for c in tr["checks"]), tr["checks"]


def _r1_top():
    n = 50
    cl = list(np.linspace(100, 109, 20))
    cl[8:12] = [103.5, 102.7, 102.3, 102.9]  # mid-trend dip (structure)
    cl += [111.7]                             # 20: P1 extreme high
    cl += list(np.linspace(111.0, 106.1, 10))  # 21-30: pullback to 106.1
    cl += list(np.linspace(107.0, 109.5, 8))   # 31-38: retest, P3 109.5
    cl += [109.2, 108.5, 107.8, 107.1, 106.5, 106.2,  # 39-44
           105.7, 106.2, 105.1, 105.0, 104.8]        # 45-49: break
    hi = [c + 0.3 for c in cl]
    lo = [c - 0.5 for c in cl]
    hi[20], lo[20] = 112.0, 111.2   # P1 bar
    hi[30], lo[30] = 106.6, 106.0   # P2 bar (pullback low)
    hi[38], lo[38] = 109.5, 109.0   # P3 bar (lower high)
    return _daily(n, cl, hi, lo)


def t_r1_top_fires():
    d = _r1_top()
    tr: dict = {}
    res = r1_123(d, trace=tr)
    assert len(res) == 1, res
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection", r
    assert r["price"] == 104.8 and r["stop"] == 109.75, r  # P3 + 0.25xATR
    assert "P1=112.00" in [c for c in tr["checks"] if "FIRE" in c][0]


def t_r1_top_failed_failure_is_no_trade():
    d = _r1_top()
    d.loc[d.index[38], "High"] = 112.5  # retest EXCEEDS P1: no failure
    tr: dict = {}
    assert r1_123(d, trace=tr) == []
    assert tr["fired"] == []


def t_zigzag_confirms_on_atr_move():
    n = 30
    cl = [100.0] * 10 + list(np.linspace(100, 108, 10)) + \
        list(np.linspace(108, 102, 10))
    d = _daily(n, cl)
    pivs = atr_zigzag(d)
    assert pivs == [(19, "H", 108.3)], pivs  # rise 8 >> 3xATR, then fall
    # appending flat bars never moves a confirmed pivot (no repaint)
    d2 = pd.concat([d, _daily(5, [102.0] * 5, start="2026-02-16")])
    assert atr_zigzag(d2) == pivs
    # sub-threshold wiggle confirms nothing
    d3 = _daily(20, [100.0] * 10 + list(np.linspace(100, 102, 5)) +
                [101.0] * 5)
    assert atr_zigzag(d3) == []


def t_tema_math_and_stack():
    s = pd.Series(np.linspace(100, 110, 30))
    e1 = s.ewm(span=10, adjust=False).mean()
    e2 = e1.ewm(span=10, adjust=False).mean()
    e3 = e2.ewm(span=10, adjust=False).mean()
    assert abs(float(tema(s, 10).iloc[-1]) -
               (3 * float(e1.iloc[-1]) - 3 * float(e2.iloc[-1]) +
                float(e3.iloc[-1]))) < 1e-9
    # low-lag property: TEMA hugs the last print tighter than same-span EMA
    assert abs(float(tema(s, 8).iloc[-1]) - 110.0) < \
        abs(float(s.ewm(span=8, adjust=False).mean().iloc[-1]) - 110.0)
    # NOTE: TEMA overshoots, so stack SORT is not trend-ordered on gentle
    # slopes (falls can read 8>13>20). R2 therefore triggers on the fast
    # CROSS + slow-zone punch, never on the sort; t_r2_bear_fires covers it.


def _r2_top():
    n = 71
    cl = np.full(n, 100.0)
    cl[38:61] = np.linspace(100, 115, 23)    # markup, peak bar 60
    cl[61:70] = np.linspace(115, 108, 9)     # markdown
    cl[70:] = 108.0
    return _daily(n, cl)


def t_r2_bear_fires_at_breakdown():
    d = _r2_top()
    tr: dict = {}
    res = r2_zigzag_tema(d, trace=tr)
    assert len(res) == 1, res
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection", r
    assert r["algo"] == "R2" and r["target"] == "-"
    assert r["price"] == 108.0, r  # entry = confirming close
    assert r["stop"] == round(115.3 + 0.5 * 1.0, 2), r  # pivot + buf
    assert r["sig_key"][0] == "R2" and r["sig_key"][1] == "top"
    assert "TEMA8" in [c for c in tr["checks"] if "FIRE" in c][0]


def t_r2_shallow_pullback_skips():
    d = _r2_top().iloc[:64]  # peak formed, breakdown not yet underway
    tr: dict = {}
    assert len(d) >= 60
    assert r2_zigzag_tema(d, trace=tr) == []


def _trend_frame():
    """40 bars: rising baseline with fractal anchors at bars 10 and 22
    (lows for the up-line, highs for the channel), a 4-bar crash on
    bars 35-38, then an exhaustion wick on the LAST bar: high spikes
    over any channel top while the close stays crashed under the line.
    The wick sits at the unconfirmed edge so it never becomes a pivot
    anchor itself."""
    n = 40
    base = 100 + 0.3 * np.arange(n)
    lo = base - 0.5
    hi = base + 0.3
    lo[10] -= 1.5
    lo[22] -= 1.0
    hi[10] += 1.5
    hi[22] += 1.0
    cl = np.array(list(base[:35]) + [108.5, 106.0, 104.0, 103.0, 102.0],
                 float)
    hi = np.array(hi, float)
    lo = np.array(lo, float)
    hi[35:39] = cl[35:39] + 0.3
    lo[35:39] = cl[35:39] - 0.5
    hi[39] = 118.5  # exhaustion wick over the channel top (~117.8)
    lo[39] = 101.5
    return _daily(n, cl, hi, lo)


def _fresh_break_frame():
    """30 bars: rising baseline, fractal low anchors at 8/18, last close
    0.6xATR under the up-line: a fresh break (dist in [0.5, 2.0])."""
    n = 30
    base = 100 + 0.3 * np.arange(n)
    lo = base - 0.5
    hi = base + 0.3
    lo[8] -= 1.5
    lo[18] -= 1.0
    hi[8] += 1.5
    hi[18] += 1.0
    cl = np.array(base, float)
    cl[29] = 107.15  # line(29) = 107.75 -> dist 0.6xATR
    hi = np.array(hi, float)
    lo = np.array(lo, float)
    hi[29], lo[29] = 108.0, 106.8
    return _daily(n, cl, hi, lo)


def t_r3_break_fires():
    d = _fresh_break_frame()
    tr: dict = {}
    res = r3_trendline(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection" and r["algo"] == "R3"
    assert "tl-break" in r["note"] and "fill=line" in r["note"]
    assert r["sig_key"][0] == "R3" and r["sig_key"][1] == "top"
    # entry is the line cross (107.65), not the chase-close (107.15)
    assert r["price"] > float(d["Close"].iloc[-1]), r
    assert r["price"] < float(d["High"].iloc[-10:].max()), r
    assert r["stop"] == round(float(d["High"].iloc[-10:].max()) + 0.25, 2), r
    assert "sl=hi10" in r["note"] and r["stop"] > r["price"]


def t_r3_stale_break_skips():
    # crash frame: close ~9xATR past the line (late anchors, runaway
    # move) -> stale, no setup even though the geometric break is huge
    d = _trend_frame()
    tr: dict = {}
    assert r3_trendline(d, trace=tr) == []
    assert any("stale" in c for c in tr["checks"]), tr["checks"]


def t_r3_stop_prefers_pivot():
    d = _trend_frame()
    stop, src = _r3_stop(d, "up", 100.0, 10.0)
    assert src == "pivot" and \
        abs(stop - (float(d["High"].iloc[34]) + 2.5)) < 1e-9, (stop, src)
    stop, src = _r3_stop(d, "dn", 120.0, 10.0)
    assert src == "pivot" and \
        abs(stop - (float(d["Low"].iloc[22]) - 2.5)) < 1e-9, (stop, src)
    # degenerate pinch (stop on top of entry): 0.5xATR min-risk floor
    stop, src = _r3_stop(d, "up", 110.70, 1.0)
    assert src == "minrisk" and abs(stop - 111.20) < 1e-9, (stop, src)
    stop, src = _r3_stop(d, "dn", 105.20, 1.0)
    assert src == "minrisk" and abs(stop - 104.70) < 1e-9, (stop, src)
    # stale anchor (pivot 10xATR away): 2xATR maximum cap
    stop, src = _r3_stop(d, "up", 100.0, 1.0)
    assert src == "cap" and abs(stop - 102.0) < 1e-9, (stop, src)
    stop, src = _r3_stop(d, "dn", 120.0, 1.0)
    assert src == "cap" and abs(stop - 118.0) < 1e-9, (stop, src)


def t_r3_no_break_no_trade():
    d = _trend_frame().iloc[:35]  # ends at the pre-crash top
    tr: dict = {}
    assert r3_trendline(d, trace=tr) == []
    assert tr["fired"] == []


def t_r3_bull_mirror():
    n = 30
    base = 120 - 0.3 * np.arange(n)
    lo = base - 0.5
    hi = base + 0.3
    hi[8] += 1.5
    hi[18] += 1.0
    lo[13] -= 1.5  # separating low so both highs survive the merge
    cl = np.array(base, float)
    cl[29] = 112.76  # dn-line(29) = 112.16 -> fresh break, dist 0.6xATR
    hi = np.array(hi, float)
    lo = np.array(lo, float)
    hi[29], lo[29] = 113.1, 112.2
    d = _daily(n, cl, hi, lo)
    tr: dict = {}
    res = r3_trendline(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    assert res[0]["state"] == "Possible upcoming Reversal"
    # entry is the line cross (112.26), under the chase-close (112.76)
    assert "fill=line" in res[0]["note"] and "sl=lo10" in res[0]["note"], \
        res[0]
    assert res[0]["price"] < float(d["Close"].iloc[-1]), res[0]
    assert res[0]["stop"] == round(float(d["Low"].iloc[-10:].min()) - 0.25,
                                   2), res[0]
    assert res[0]["stop"] < res[0]["price"]


def t_r4_overshoot_and_confirm():
    d = _trend_frame()  # wick high 118.5 on the last bar
    tr: dict = {}
    res = r4_channel(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["algo"] == "R4" and r["stop"] == round(118.5 + 0.25, 2), r
    assert r["sig_key"] == ("R4", "top", d.index[39].date().isoformat())
    # overshoot without the wick: ends mid-crash, no 5-bar overshoot
    tr2: dict = {}
    assert r4_channel(d.iloc[:38], trace=tr2) == []
    assert tr2["fired"] == []


def _sma_frame(direction="dn"):
    """SMA50 flat at 100 through bar 54, then 5 falling/rising bars so the
    5-bar slope flips exactly at bar 55 (the most recent flip in the
    routine's 5-bar window); close crosses the average at the same bar."""
    n = 60
    idx = pd.bdate_range("2026-01-05", periods=n, tz=ET)
    if direction == "dn":
        sma = [100.0] * 55 + [99.7, 99.3, 98.9, 98.5, 98.0]
        cl = [100.5] * 55 + [99.0, 98.5, 98.0, 97.5, 97.0]
    else:
        sma = [100.0] * 55 + [100.3, 100.7, 101.1, 101.5, 102.0]
        cl = [99.5] * 55 + [100.8, 101.2, 101.6, 102.0, 102.5]
    cl = np.array(cl, float)
    d = pd.DataFrame({"Open": cl - 0.1, "High": cl + 0.3, "Low": cl - 0.5,
                      "Close": cl, "Volume": 1_000_000.0}, index=idx)
    d["atr"] = 1.0
    d["sma50"] = sma
    return d


def t_r5_flip_fires():
    d = _sma_frame("dn")
    tr: dict = {}
    res = r5_maslope(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection" and r["algo"] == "R5"
    assert r["stop"] == round(float(d["High"].iloc[-10:].max()) + 0.25, 2)
    assert r["sig_key"] == ("R5", "top", d.index[55].date().isoformat()), \
        r["sig_key"]


def t_r5_bull_mirror():
    d = _sma_frame("up")
    tr: dict = {}
    res = r5_maslope(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Reversal"
    assert r["sig_key"] == ("R5", "bottom", d.index[55].date().isoformat()), \
        r["sig_key"]


def _donch_frame(direction="bull"):
    n = 60
    idx = pd.bdate_range("2026-01-05", periods=n, tz=ET)
    if direction == "bull":
        cl = np.linspace(110, 100, n)
        cl[-1] = 109.0  # pop over the 20d high
        sma = np.linspace(109, 99, n)
        hi20 = np.full(n, 108.0)
        lo20 = np.full(n, 95.0)
    else:
        cl = np.linspace(100, 110, n)
        cl[-1] = 101.0  # flush under the 20d low
        sma = np.linspace(101, 111, n)
        hi20 = np.full(n, 115.0)
        lo20 = np.full(n, 102.0)
    d = pd.DataFrame({"Open": cl - 0.1, "High": cl + 0.3, "Low": cl - 0.5,
                      "Close": cl, "Volume": 1_000_000.0}, index=idx)
    d["atr"] = 1.0
    d["sma50"] = sma
    d["hi20"] = hi20
    d["lo20"] = lo20
    return d


def t_r6_break_fires():
    d = _donch_frame("bull")
    tr: dict = {}
    res = r6_donchian(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Reversal" and r["algo"] == "R6"
    assert r["sig_key"] == ("R6", "bottom", 108.0), r["sig_key"]
    assert r["stop"] == round(float(d["Low"].iloc[-10:].min()) - 0.25, 2)


def t_r6_bear_mirror():
    d = _donch_frame("bear")
    tr: dict = {}
    res = r6_donchian(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    assert res[0]["state"] == "Possible upcoming Rejection"
    assert res[0]["sig_key"] == ("R6", "top", 102.0)


def _interp(n, pts):
    cl = np.zeros(n)
    for (b0, v0), (b1, v1) in zip(pts, pts[1:]):
        cl[b0:b1 + 1] = np.linspace(v0, v1, b1 - b0 + 1)
    return cl


def _div_frame(direction="bear"):
    n = 60
    idx = pd.bdate_range("2026-01-05", periods=n, tz=ET)
    if direction == "bear":
        cl = _interp(n, [(0, 94), (20, 100), (27, 96), (35, 102), (42, 97),
                         (50, 104), (59, 100)])
        macd = np.zeros(n)
        macd[20], macd[35], macd[50] = 2.0, 1.2, 0.4
        macd[51:55] = 0.38
        macd[55] = 0.4
        macd[56:] = 0.1
        sig = np.full(n, 0.3)
    else:
        cl = _interp(n, [(0, 106), (20, 100), (27, 104), (35, 98), (42, 103),
                         (50, 96), (59, 100)])
        macd = np.zeros(n)
        macd[20], macd[35], macd[50] = -1.0, -0.5, -0.1
        macd[51:55] = -0.38
        macd[55] = -0.4
        macd[56:] = 0.1
        sig = np.full(n, -0.3)
    d = pd.DataFrame({"Open": cl - 0.1, "High": cl + 0.3, "Low": cl - 0.5,
                      "Close": cl, "Volume": 1_000_000.0}, index=idx)
    d["atr"] = 1.0
    d["macd"] = macd
    d["macd_sig"] = sig
    return d


def t_r7_divergence_fires():
    d = _div_frame("bear")
    tr: dict = {}
    res = r7_macddiv(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection" and r["algo"] == "R7"
    assert r["stop"] == round(104.3 + 0.25, 2), r  # 3rd high + buffer
    assert r["sig_key"][0] == "R7" and len(r["sig_key"]) == 5


def t_r7_bull_mirror():
    d = _div_frame("bull")
    tr: dict = {}
    res = r7_macddiv(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    assert res[0]["state"] == "Possible upcoming Reversal"
    assert res[0]["stop"] == round(95.5 - 0.25, 2), res[0]  # 3rd low - buffer


def _obv_frame(direction="bear"):
    """Price grinds to a fresh 20-day extreme on the last bar while OBV
    sags under its EMA21: drift days run on light volume, counter-days
    on heavy volume. Bear: +0.5 x3 on 400k, -1.1 on 2M (net drift up,
    OBV down). Bull mirrors with heavy volume on the up-flush days."""
    n = 60
    idx = pd.bdate_range("2026-01-05", periods=n, tz=ET)
    moves, vols = [], []
    for i in range(20):
        if direction == "bear":
            m = -1.1 if i % 4 == 3 else 0.5
            v = 2_000_000.0 if i % 4 == 3 else 400_000.0
        else:
            m = 1.1 if i % 4 == 3 else -0.5
            v = 2_000_000.0 if i % 4 == 3 else 400_000.0
        moves.append(m)
        vols.append(v)
    if direction == "bear":
        base = np.linspace(95, 100, 40)
        seg = 100.0 + np.cumsum(moves)
    else:
        base = np.linspace(105, 100, 40)
        seg = 100.0 + np.cumsum(moves)
    cl = np.concatenate([base, seg])
    vol = np.concatenate([np.full(40, 1_000_000.0), np.array(vols)])
    d = pd.DataFrame({"Open": cl - 0.1, "High": cl + 0.1, "Low": cl - 0.6,
                      "Close": cl, "Volume": vol}, index=idx)
    d["atr"] = 1.0
    return d


def t_r8_fade_fires():
    d = _obv_frame("bear")
    tr: dict = {}
    res = r8_obv(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection" and r["algo"] == "R8"
    assert r["stop"] == round(float(d["High"].iloc[-20:].max()) + 0.25, 2)
    eb = int(np.argmax(d["High"].to_numpy()[-20:]))  # fresh-high bar
    assert r["sig_key"] == ("R8", "top",
                            d.index[40 + eb].date().isoformat()), r["sig_key"]


def t_r8_bull_mirror():
    d = _obv_frame("bull")
    tr: dict = {}
    res = r8_obv(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    assert res[0]["state"] == "Possible upcoming Reversal"


def _climax_frame(direction="bear"):
    n = 60
    idx = pd.bdate_range("2026-01-05", periods=n, tz=ET)
    if direction == "bear":
        cl = np.linspace(100, 106, n)
        sma = np.full(n, 99.0)
        hi = cl + 0.3
        lo = cl - 0.5
        hi[56], lo[56], cl[56] = 107.5, 106.0, 106.8
        cl[57], cl[58], cl[59] = 105.5, 105.0, 104.5
        vol = np.full(n, 1_000_000.0)
        vol[56] = 9_000_000.0
    else:
        cl = np.linspace(112, 106, n)
        sma = np.full(n, 113.0)
        hi = cl + 0.5
        lo = cl - 0.3
        hi[56], lo[56], cl[56] = 106.0, 104.5, 105.2
        cl[57], cl[58], cl[59] = 106.5, 107.0, 107.5
        vol = np.full(n, 1_000_000.0)
        vol[56] = 9_000_000.0
    d = pd.DataFrame({"Open": cl - 0.1, "High": hi, "Low": lo,
                      "Close": cl, "Volume": vol}, index=idx)
    d["atr"] = 1.0
    d["sma50"] = sma
    return d


def t_r9_climax_fires():
    d = _climax_frame("bear")
    tr: dict = {}
    res = r9_climax(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection" and r["algo"] == "R9"
    assert r["stop"] == 107.75, r
    assert r["sig_key"] == ("R9", "top", d.index[56].date().isoformat())


def t_r9_bull_mirror():
    d = _climax_frame("bull")
    tr: dict = {}
    res = r9_climax(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    assert res[0]["state"] == "Possible upcoming Reversal"
    assert res[0]["stop"] == 104.25, res[0]


def _vo_frame(direction="bear"):
    n = 60
    idx = pd.bdate_range("2026-01-05", periods=n, tz=ET)
    vol = np.full(n, 1_000_000.0)
    if direction == "bear":
        cl = _interp(n, [(0, 94), (20, 100), (30, 95), (40, 102), (59, 98)])
        vol[10:26] = 5_000_000.0
        vol[26:40] = 500_000.0
        vol[40:55] = 600_000.0
        vol[55:] = 150_000.0
    else:
        cl = _interp(n, [(0, 106), (20, 100), (30, 105), (40, 98), (59, 102)])
        vol[0:16] = 8_000_000.0
        vol[16:26] = 1_000_000.0
        vol[26:46] = 2_000_000.0
        vol[46:] = 1_000_000.0
    d = pd.DataFrame({"Open": cl - 0.1, "High": cl + 0.3, "Low": cl - 0.5,
                      "Close": cl, "Volume": vol}, index=idx)
    d["atr"] = 1.0
    return d


def t_r10_divergence_fires():
    d = _vo_frame("bear")
    tr: dict = {}
    res = r10_volosc(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    r = res[0]
    assert r["state"] == "Possible upcoming Rejection" and r["algo"] == "R10"
    assert r["stop"] == round(102.3 + 0.25, 2), r  # 2nd high + buffer


def t_r10_bull_mirror():
    d = _vo_frame("bull")
    tr: dict = {}
    res = r10_volosc(d, trace=tr)
    assert len(res) == 1, tr.get("checks")
    assert res[0]["state"] == "Possible upcoming Reversal"
    assert res[0]["stop"] == round(97.5 - 0.25, 2), res[0]  # 2nd low - buffer


TESTS = [t_fractal_pivots, t_r1_bottom_fires, t_r1_bottom_no_trigger,
         t_r1_top_fires, t_r1_top_failed_failure_is_no_trade,
         t_zigzag_confirms_on_atr_move, t_tema_math_and_stack,
         t_r2_bear_fires_at_breakdown, t_r2_shallow_pullback_skips,
         t_r3_break_fires, t_r3_stale_break_skips, t_r3_stop_prefers_pivot,
         t_r3_no_break_no_trade, t_r3_bull_mirror,
         t_r4_overshoot_and_confirm, t_r5_flip_fires, t_r5_bull_mirror,
         t_r6_break_fires, t_r6_bear_mirror, t_r7_divergence_fires,
         t_r7_bull_mirror, t_r8_fade_fires, t_r8_bull_mirror,
         t_r9_climax_fires, t_r9_bull_mirror, t_r10_divergence_fires,
         t_r10_bull_mirror]


def main() -> int:
    fails = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - test harness reports all
            fails += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(TESTS) - fails}/{len(TESTS)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
