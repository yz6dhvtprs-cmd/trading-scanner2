"""Offline checks for the NYSE calendar helpers (no network).

Covers trading_gap, which drives the live 3-trading-day alert dedupe:
sessions are counted exclusive of the previous alert date, inclusive of
today, skipping weekends and market holidays.

Usage (project root, venv active):
    python scanner/test_market_calendar.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from market_calendar import add_trading_days, trading_gap


def test_same_day_is_zero():
    assert trading_gap(dt.date(2026, 9, 11), dt.date(2026, 9, 11)) == 0


def test_next_session_is_one():
    assert trading_gap(dt.date(2026, 9, 10), dt.date(2026, 9, 11)) == 1


def test_weekend_counts_one():
    # Fri 09-11 -> Mon 09-14: only Monday is a session
    assert trading_gap(dt.date(2026, 9, 11), dt.date(2026, 9, 14)) == 1


def test_midweek_run():
    # Mon 09-07 (Labor Day, closed) -> Fri 09-11: Tue..Fri = 4 sessions
    assert trading_gap(dt.date(2026, 9, 7), dt.date(2026, 9, 11)) == 4


def test_july_fourth_week_2026():
    # Thu 07-02 -> Mon 07-06: Fri 07-03 is the observed holiday, so only
    # Monday counts
    assert trading_gap(dt.date(2026, 7, 2), dt.date(2026, 7, 6)) == 1


def test_add_trading_days_skips_labor_day():
    # Fri 09-04 + 5 sessions: weekend + Labor Day Mon skipped -> Mon 09-14
    assert add_trading_days(dt.date(2026, 9, 4), 1) == dt.date(2026, 9, 8)
    assert add_trading_days(dt.date(2026, 9, 4), 5) == dt.date(2026, 9, 14)


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
