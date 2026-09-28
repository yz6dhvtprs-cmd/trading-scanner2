"""Offline checks for the Monday screener (no network).

Contract: cap band 5B-1T, RVOL20 Friday-vs-20d in 1-10, equity-only, and
weekly rotation that adds new passers and drops tracked names that fail
— spy+qqq membership never enters the equation.

Usage (project root, venv active):
    python algos/test_screener.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screener import cap_pass, drop_suffix  # noqa: E402
from screener import rotate, rvol, rvol_pass, yf_symbol  # noqa: E402


def vol_series(last=100.0, base=50.0, n=25):
    idx = pd.bdate_range("2026-01-01", periods=n + 1)
    return pd.Series([base] * n + [last], index=idx)


def test_rvol_friday_over_mean():
    assert rvol(vol_series(100.0, 50.0)) == 2.0
    assert rvol(vol_series(25.0, 50.0)) == 0.5
    assert rvol_pass(2.0) and not rvol_pass(0.5) and not rvol_pass(10.1)
    assert rvol(pd.Series([1.0])) != rvol(pd.Series([1.0]))  # NaN: thin


def test_cap_band():
    assert cap_pass(5e9) and cap_pass(1e12) and cap_pass(5e10)
    assert not cap_pass(4.9e9) and not cap_pass(1.1e12)
    assert not cap_pass(0) and not cap_pass(None)


def test_symbols():
    assert yf_symbol("BRK/A") == "BRK-A"
    assert yf_symbol("AAPL") == "AAPL"
    assert drop_suffix("XYZ-W") and drop_suffix("ABC-U")
    assert not drop_suffix("BRK-B")


def test_drop_name():
    from screener import drop_name
    assert drop_name("AGNC Investment Corp. Depositary Shares")
    assert drop_name("Duke Energy 5.625% Junior Subordinated Debenture")
    assert drop_name("AT&T Inc. 5.350% Global Notes due 2066")
    assert drop_name("ProShares Ultra VIX ETF")
    assert not drop_name("Unity Software Inc. Common Stock")
    assert not drop_name("United Bankshares Inc. Common Stock")
    assert not drop_name("Healthcare Realty Trust Incorporated Common Stock")
    assert not drop_name("Energy Transfer LP Common Units")
    assert not drop_name("")


def test_rotate_adds_and_removes():
    tracked = {"OLD": {"added": "2026-09-18", "last_pass": "2026-09-18"}}
    pool, added, removed = rotate(tracked, {"OLD", "NEW"}, "2026-09-25")
    assert sorted(pool) == ["NEW", "OLD"] and added == ["NEW"]
    assert removed == [] and tracked["NEW"]["added"] == "2026-09-25"
    pool, added, removed = rotate(tracked, {"NEW"}, "2026-10-02")
    assert pool == ["NEW"] and removed == ["OLD"] and "OLD" not in tracked


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
