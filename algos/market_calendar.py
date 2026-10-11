"""NYSE open check (weekends + fixed/observed US market holidays).

Usage:
    python algos/market_calendar.py            # prints open|closed for today
    python algos/market_calendar.py 2026-09-07
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
    f = (b + 8) // 25          # Meeus/Jones/Butcher: not divmod(8b+13, 25),
    g = (b - f + 1) // 3        # which put Good Friday on the wrong Friday every year
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day) - dt.timedelta(days=2)


def is_open(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def pt_now():
    """Wall clock in America/Los_Angeles (DST-aware: a fixed UTC-7 shifted
    the whole session window an hour after 2026-11-01)."""
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/Los_Angeles"))


def market_open(now=None) -> bool:
    """True inside the 06:30-13:00 PT tape (intraday + Friday live)."""
    now = now or pt_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 6 * 60 + 30 <= mins <= 13 * 60 + 5  # 6:30a-1:05p PT


def trading_gap(prev: dt.date, today: dt.date) -> int:
    """Trading sessions in (prev, today]: 0 same-day, 1 next session."""
    n, d = 0, prev + dt.timedelta(days=1)
    while d <= today:
        if is_open(d):
            n += 1
        d += dt.timedelta(days=1)
    return n


def add_trading_days(day: dt.date, n: int) -> dt.date:
    """Date n trading sessions after `day` (skips weekends/holidays)."""
    d = day
    while n > 0:
        d += dt.timedelta(days=1)
        if is_open(d):
            n -= 1
    return d


def afterhours(now: dt.datetime | None = None) -> bool:
    """True outside the 06:30-13:00 PT session window or on closed days.
    PT wall clock, matching the runner's intraday gate."""
    now = now or pt_now()
    if not is_open(now.date()):
        return True
    mins = now.hour * 60 + now.minute
    return mins < 6 * 60 + 30 or mins > 13 * 60


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    day = dt.date.fromisoformat(arg)
    print("open" if is_open(day) else "closed")
    return 0 if is_open(day) else 1


if __name__ == "__main__":
    sys.exit(main())
