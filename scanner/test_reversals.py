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
                       r2_zigzag_tema, tema)

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


TESTS = [t_fractal_pivots, t_r1_bottom_fires, t_r1_bottom_no_trigger,
         t_r1_top_fires, t_r1_top_failed_failure_is_no_trade,
         t_zigzag_confirms_on_atr_move, t_tema_math_and_stack,
         t_r2_bear_fires_at_breakdown, t_r2_shallow_pullback_skips]


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
