"""Offline checks for the consolidation detector (no network).

Contract: A (ATR-normalized range) gates, E (slope + residual veto)
rejects drifts, B (close clustering) is reported but never gates, and
the trailing run reports true boundaries + length.

Usage (project root, venv active):
    python scanner/test_consolidation.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from consolidation import consolidation, find_runs  # noqa: E402


def frame(rows):
    """OHLC frame from (open, high, low, close) tuples, daily index."""
    idx = pd.bdate_range("2026-01-01", periods=len(rows))
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                         "Volume": 1000}, index=idx)


def flat(px=100.0, n=30, wob=0.2):
    return [(px, px + wob, px - wob, px) for _ in range(n)]


def ramp(n=30, step=1.0):
    return [(100 + step * i, 101 + step * i, 99 + step * i,
             100 + step * i) for i in range(n)]


def test_flat_is_consolidating_with_exact_box():
    r = consolidation(frame(flat()), n=4)
    assert r["active"] is True
    assert r["lower"] == 99.8 and r["upper"] == 100.2
    assert r["bars"] == 30  # whole flat run
    assert r["width_atr"] <= 1.0


def test_drift_fails_slope_veto():
    r = consolidation(frame(ramp()), n=4)
    assert r["active"] is False
    assert r["bars"] == 0


def test_wide_range_fails():
    rows = flat(n=26) + [(100, 110, 90, 100)] * 4
    assert consolidation(frame(rows), n=4)["active"] is False


def test_wicks_fool_b_not_a():
    # closes pinned flat (B tight) but huge tails (A wide): gate stays
    # shut while the confirm flag stays informative, not decisive.
    from consolidation import window_gate
    wild = frame([(100, 105, 95, 100.0 + (0.05 if i % 2 else -0.05))
                  for i in range(4)])
    g = window_gate(wild, 0.4, 4, 1.0, 0.10, 0.50)
    assert g["active"] is False  # width 10/0.4 fails A ...
    assert g["b_pct"] <= 0.5  # ... while closes cluster (B tight)
    r = consolidation(frame(flat()), n=4, b_pct=0.5)
    assert r["active"] is True and r["b_tight"] is True


def test_too_short_is_quiet():
    assert consolidation(frame(flat(n=10)), n=4)["active"] is False


def test_find_runs_groups_and_separates():
    # long flats either side: ATR warmup (15 bars) still leaves scored
    # windows inside both flats, split by the wild middle.
    rows = flat(n=20) + [(100, 110, 90, 105)] * 3 + flat(n=20)
    runs = find_runs(frame(rows), n=4)
    assert len(runs) == 2
    assert runs[0]["bars"] >= 4 and runs[1]["bars"] >= 4
    assert runs[0]["end"] < runs[1]["end"]
    assert runs[0]["lower"] == 99.8 and runs[0]["upper"] == 100.2


def test_find_runs_empty_when_trending():
    assert find_runs(frame(ramp()), n=4) == []


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("test_") and callable(v)]


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
