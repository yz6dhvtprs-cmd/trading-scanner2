"""NYSE open check (weekends + fixed/observed US market holidays).

Usage:
    python scanner/market_calendar.py            # prints open|closed for today
    python scanner/market_calendar.py 2026-09-07
run_scan.sh skips nightly/premarket modes when this reports closed.
Saturday review and bootstrap are unaffected.
"""
from __future__ import annotations

import datetime as dt
import sys


def _observed(year: int, month: int, day: int) -> dt.date:
    d = dt.date(year, month, day)
    if d.weekday() == 5:  # Saturday -> Friday before
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday after
        return d + dt.timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    off = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=off + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 \
        else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def holidays(year: int) -> set:
    mon = 0
    return {
        _observed(year, 1, 1),                       # New Year
        _nth_weekday(year, 1, mon, 3),               # MLK
        _nth_weekday(year, 2, mon, 3),               # Presidents
        # Good Friday: derived from Easter (Anonymous/Gregorian algorithm)
        _good_friday(year),
        _last_weekday(year, 5, mon),                 # Memorial
        _observed(year, 6, 19),                      # Juneteenth
        _observed(year, 7, 4),                       # Independence
        _nth_weekday(year, 9, mon, 1),               # Labor
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving (Thursday=3)
        _observed(year, 12, 25),                     # Christmas
    }


def _good_friday(year: int) -> dt.date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f, g = divmod(b * 8 + 13, 25)
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day) - dt.timedelta(days=2)


def is_open(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    day = dt.date.fromisoformat(arg)
    print("open" if is_open(day) else "closed")
    return 0 if is_open(day) else 1


if __name__ == "__main__":
    sys.exit(main())
