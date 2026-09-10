"""Standalone checks for the analyzer gates (no network, no arguments).

Usage (project root, venv active):
    python scanner/test_analyze.py

Covers: rejection = up context (D+1h UP) + 15m flip down at resistance;
reversal = down context (D+1h DN) + 15m flip up at support; Long coexists
with rejection/reversal; breakout RVOL relaxation (default 2.0 preserved,
live 1.5); pre-multistate STATE file migration.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
import analyze as _anz  # noqa: E402
from analyze import analyze  # noqa: E402
from combos import signal_mask  # noqa: E402

N = 130  # swing_levels needs a 120-bar lookback


def _daily(**kw) -> pd.DataFrame:
    # Wide synthetic swing (99/90) so swing Fib/supports sit well clear of
    # close and the RR>=1 gate behaves like a real tape, not merged noise.
    base = dict(Open=99.0, High=99.0, Low=90.0, Close=98.9, ema9=97.5,
                ema21=97.5, ema50=97.0, ema200=94.0, sma50=96.0, sma200=95.0,
                rsi=60.0, atr=1.0, rvol=0.5, hi20=99.5, lo20=90.0, adx=25.0,
                bull=True, macd=0.1, macd_sig=0.0)
    base.update(kw)
    return pd.DataFrame({k: np.full(N, float(v)) if not isinstance(v, bool)
                         else np.full(N, v) for k, v in base.items()})


def _tf(close: float, ema21: float, up: bool) -> pd.DataFrame:
    n = 30
    macd, sig = (0.1, 0.0) if up else (-0.1, 0.0)
    return pd.DataFrame({"Close": np.full(n, close),
                         "ema21": np.full(n, ema21),
                         "macd": np.full(n, macd),
                         "macd_sig": np.full(n, sig)})


def _rejection_frames(rvol=0.5, hi20=99.5, m15_dn=True):
    """At the 20d high, bearish engulfing, RSI 60, D+1h UP."""
    d = _daily(rvol=rvol, hi20=hi20)
    d.loc[N - 2, ["Open", "Close"]] = [99.0, 99.8]  # prior bull bar
    d.loc[N - 1, ["Open", "High", "Low", "Close"]] = [100.0, 100.8, 99.0, 98.9]
    h1 = _tf(100.0, 99.0, True)
    m15 = _tf(100.0, 101.0, False) if m15_dn else _tf(101.0, 100.0, True)
    return d, h1, m15


def _reversal_frames():
    """At the 20d low, bullish engulfing, RSI 40, D+1h DN, 15m UP."""
    d = _daily(Close=91.6, High=97.0, Low=90.8, ema9=93.0, ema21=93.5,
               ema50=94.0, ema200=96.0, sma50=95.0, sma200=97.0, rsi=40.0,
               hi20=100.0, lo20=91.0, bull=False, macd=-0.1)
    d.loc[N - 2, ["Open", "Close"]] = [91.5, 90.8]  # prior bear bar
    d.loc[N - 1, ["Open", "High", "Low", "Close"]] = [90.6, 91.9, 90.5, 91.6]
    h1 = _tf(90.0, 91.0, False)
    m15 = _tf(91.0, 90.0, True)
    return d, h1, m15


def _states(results):
    if isinstance(results, dict):  # pre-multistate analyze() returned one dict
        results = [results]
    return [r["state"] for r in results]


def t_rejection_fires():
    states = _states(analyze("T", *_rejection_frames()))
    assert states == ["Possible upcoming Rejection"], states


def t_rejection_needs_15m_flip():
    states = _states(analyze("T", *_rejection_frames(m15_dn=False)))
    assert "Possible upcoming Rejection" not in states, states


def t_reversal_fires():
    states = _states(analyze("T", *_reversal_frames()))
    assert states == ["Possible upcoming Reversal"], states


def t_long_coexists_with_rejection():
    # close sits above the 20d high (breakout, RVOL>=live min) AND near it
    # (rejection zone) with the 15m flipped down -> both states fire.
    d, h1, m15 = _rejection_frames(rvol=2.1, hi20=98.5)
    states = _states(analyze("T", d, h1, m15))
    assert "Long" in states and "Possible upcoming Rejection" in states, states


def t_rvol_default_preserved_but_relaxable():
    f = pd.DataFrame({"Close": [101.0], "Open": [99.0], "High": [101.0],
                      "Low": [98.0], "ema9": [99.0], "ema21": [98.0],
                      "ema50": [97.0], "sma200": [95.0], "rsi": [60.0],
                      "rvol": [1.6], "hi20": [100.0], "lo20": [90.0],
                      "adx": [25.0], "bull": [True]})
    assert not bool(signal_mask(f, "breakout", 1, 20.0)[0])  # default 2.0
    assert bool(signal_mask(f, "breakout", 1, 20.0, 1.5)[0])  # live 1.5


def t_state_migration():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        json.dump({"A": {"state": "Long", "note": "n1"},
                   "B": {"state": "-", "note": "x"},
                   "C": {"Long": "n2"}}, open(path, "w"))
        old, _anz.STATE = _anz.STATE, path
        try:
            got = _anz._load_state()
        finally:
            _anz.STATE = old
    finally:
        os.remove(path)
    assert got == {"A": {"Long": "n1"}, "B": {}, "C": {"Long": "n2"}}, got


TESTS = [t_rejection_fires, t_rejection_needs_15m_flip, t_reversal_fires,
         t_long_coexists_with_rejection, t_rvol_default_preserved_but_relaxable,
         t_state_migration]


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
