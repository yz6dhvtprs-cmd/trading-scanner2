"""Offline checks for the earnings layer (no network).

Contract: the pure helpers read a cache dict in either shape (on-disk
{t: {fetched, dates}} or fetch_earnings()'s {t: [iso]}); sessions count
like market_calendar.trading_gap; the fetch touches the network only for
stale names, never dies on a bad name, and persists what it got.

Usage (project root, venv active):
    python algos/test_earnings.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import earnings as er  # noqa: E402

CACHE = {"AAA": {"fetched": "2026-10-10",
                 "dates": ["2026-10-29", "2026-07-30", "2026-04-30",
                           "2026-07-30"]},            # unsorted + a dupe
         "BBB": {"fetched": "2026-10-10", "dates": []},
         "CCC": ["2026-09-04", "2026-09-11"],        # fetch_earnings shape
         "DDD": {"fetched": "2026-10-10", "dates": ["2026-09-07", "bad"]}}
D = dt.date


def test_earnings_dates_sorted_dedup_both_shapes():
    assert er.earnings_dates("AAA", CACHE) == [D(2026, 4, 30), D(2026, 7, 30),
                                               D(2026, 10, 29)]
    assert er.earnings_dates("aaa", CACHE) == er.earnings_dates("AAA",
                                                                 CACHE)
    assert er.earnings_dates("CCC", CACHE) == [D(2026, 9, 4), D(2026, 9, 11)]
    assert er.earnings_dates("BBB", CACHE) == []
    assert er.earnings_dates("ZZZ", CACHE) == []        # unknown name
    assert er.earnings_dates("DDD", CACHE) == [D(2026, 9, 7)]  # bad row out


def test_next_and_last_earnings():
    assert er.next_earnings("AAA", D(2026, 7, 30), CACHE) == D(2026, 7, 30)
    assert er.next_earnings("AAA", "2026-07-31", CACHE) == \
        D(2026, 10, 29)
    assert er.next_earnings("AAA", pd.Timestamp("2026-01-05"), CACHE) == \
        D(2026, 4, 30)
    assert er.next_earnings("AAA", D(2026, 10, 30), CACHE) is None
    assert er.next_earnings("BBB", D(2026, 1, 1), CACHE) is None
    assert er.next_earnings("ZZZ", D(2026, 1, 1), CACHE) is None
    assert er.last_earnings("AAA", D(2026, 7, 30), CACHE) == D(2026, 7, 30)
    assert er.last_earnings("AAA", D(2026, 7, 29), CACHE) == D(2026, 4, 30)
    assert er.last_earnings("AAA", D(2026, 1, 1), CACHE) is None


def test_sessions_to_earnings_counts_trading_days():
    assert er.sessions_to_earnings("AAA", D(2026, 7, 30), CACHE) == 0  # same
    assert er.sessions_to_earnings("AAA", D(2026, 7, 29), CACHE) == 1
    # Fri 09-04 -> Fri 09-11 with Labor Day Mon 09-07 closed: 4 sessions
    assert er.sessions_to_earnings("CCC", D(2026, 9, 5), CACHE) == 4
    # Fri 09-04 (report day itself, 0) then the weekend doesn't count
    assert er.sessions_to_earnings("CCC", D(2026, 9, 4), CACHE) == 0
    assert er.sessions_to_earnings("CCC", D(2026, 9, 3), CACHE) == 1
    # a Labor-Day-dated row: no session between Fri and the holiday
    assert er.sessions_to_earnings("DDD", D(2026, 9, 4), CACHE) == 0
    assert er.sessions_to_earnings("AAA", D(2026, 11, 1), CACHE) is None
    assert er.sessions_to_earnings("BBB", D(2026, 1, 1), CACHE) is None
    assert er.sessions_to_earnings("ZZZ", D(2026, 1, 1), CACHE) is None


def test_earnings_between_inclusive():
    assert er.earnings_between("AAA", D(2026, 7, 30), D(2026, 7, 30), CACHE)
    assert er.earnings_between("AAA", D(2026, 7, 1), D(2026, 7, 30), CACHE)
    assert er.earnings_between("AAA", "2026-07-30", "2026-08-15", CACHE)
    assert not er.earnings_between("AAA", D(2026, 7, 31), D(2026, 10, 28),
                                   CACHE)
    assert not er.earnings_between("BBB", D(2020, 1, 1), D(2030, 1, 1), CACHE)
    assert not er.earnings_between("ZZZ", D(2020, 1, 1), D(2030, 1, 1), CACHE)


def test_frame_dates_from_yfinance_shape():
    idx = pd.to_datetime(["2026-11-02 15:00:00-05:00",
                          "2026-07-30 16:00:00-04:00",
                          "2026-07-30 16:00:00-04:00"], utc=True)
    df = pd.DataFrame({"EPS Estimate": [1.0, 2.0, 2.0]}, index=idx)
    # UTC 21:00 on 07-30 is still 07-30 in ET: ET calendar dates, deduped
    assert er.frame_dates(df) == ["2026-07-30", "2026-11-02"]
    naive = pd.DataFrame({"x": [1]}, index=pd.DatetimeIndex(["2026-01-29"]))
    assert er.frame_dates(naive) == ["2026-01-29"]
    assert er.frame_dates(None) == [] and er.frame_dates(df.iloc[:0]) == []
    nat = pd.DataFrame({"x": [1, 2]},
                       index=pd.DatetimeIndex([pd.NaT, "2026-01-29"]))
    assert er.frame_dates(nat) == ["2026-01-29"]


def test_fetch_refreshes_only_stale_names():
    today = D(2026, 10, 10)
    path = os.path.join(tempfile.mkdtemp(), "e.json")
    er.save_cache({"FRESH": {"fetched": "2026-10-05", "dates": ["2026-10-29"]},
                   "OLD": {"fetched": "2026-10-02", "dates": ["2026-01-01"]},
                   "EDGE": {"fetched": "2026-10-03", "dates": ["2026-02-02"]},
                   "EMPTY_TODAY": {"fetched": "2026-10-10", "dates": []},
                   "EMPTY_YDAY": {"fetched": "2026-10-09", "dates": []},
                   "NOSTAMP": {"dates": ["2026-03-03"]}}, path)
    calls = []

    def fake(t):
        calls.append(t)
        return [f"2026-12-{len(calls):02d}", "2026-09-01"]
    got = er.fetch_earnings(["fresh", "OLD", "EDGE", "EMPTY_TODAY",
                             "EMPTY_YDAY", "NOSTAMP", "NEW"], path,
                            max_age_days=7, pause=0, fetcher=fake,
                            today=today)
    # 7-day-old EDGE is not "older than" 7 days: kept; OLD (8d) refetched;
    # an empty entry retries the next day, not the same day
    assert calls == ["OLD", "EMPTY_YDAY", "NOSTAMP", "NEW"]
    assert got["FRESH"] == ["2026-10-29"] and got["EDGE"] == ["2026-02-02"]
    assert got["OLD"] == ["2026-09-01", "2026-12-01"]      # sorted
    assert got["EMPTY_TODAY"] == []
    assert got["NEW"] == ["2026-09-01", "2026-12-04"]
    on_disk = er.load_cache(path)
    assert on_disk["OLD"]["fetched"] == "2026-10-10"
    assert on_disk["FRESH"]["fetched"] == "2026-10-05"
    assert on_disk["NEW"]["dates"] == got["NEW"]
    # second pass the same day: nothing is stale, no call
    n = len(calls)
    er.fetch_earnings(["FRESH", "OLD", "NEW"], path, fetcher=fake, today=today,
                      pause=0)
    assert len(calls) == n
    # force refetches everything asked for
    er.fetch_earnings(["FRESH"], path, fetcher=fake, today=today, pause=0,
                      force=True)
    assert calls[-1] == "FRESH"


def test_fetch_bad_ticker_never_blocks_the_pool():
    today = D(2026, 10, 10)
    path = os.path.join(tempfile.mkdtemp(), "e.json")
    er.save_cache({"HAD": {"fetched": "2026-09-01",
                           "dates": ["2026-07-30"]}}, path)

    def fake(t):
        if t in ("BAD", "HAD"):
            raise RuntimeError("yfinance exploded")
        return ["2026-10-20"]
    got = er.fetch_earnings(["GOOD1", "BAD", "HAD", "GOOD2"], path, pause=0,
                            fetcher=fake, today=today)
    assert list(got) == ["GOOD1", "BAD", "HAD", "GOOD2"]
    assert got["BAD"] == [] and got["GOOD2"] == ["2026-10-20"]
    assert got["HAD"] == ["2026-07-30"]            # old dates survive an error
    disk = er.load_cache(path)
    assert disk["BAD"] == {"fetched": "2026-10-10", "dates": [],
                           "error": "RuntimeError: yfinance exploded"}
    assert disk["HAD"]["error"].startswith("RuntimeError")
    assert er.stale(disk["BAD"], today) is False       # not retried today
    assert er.stale(disk["BAD"], D(2026, 10, 11)) is True  # retried tomorrow
    assert er.stale(disk["HAD"], D(2026, 10, 15)) is False  # kept dates: 7d
    assert er.stale(None, today) and er.stale({"dates": ["x"]}, today)
    assert er.stale({"fetched": "garbage", "dates": ["x"]}, today)


def test_fetch_pauses_between_calls_only():
    naps = []
    real = er.time.sleep
    er.time.sleep = lambda s: naps.append(s)
    try:
        path = os.path.join(tempfile.mkdtemp(), "e.json")
        er.fetch_earnings(["A", "B", "C"], path, pause=0.3,
                          today=D(2026, 10, 10),
                          fetcher=lambda t: ["2026-11-11"])
    finally:
        er.time.sleep = real
    assert naps == [0.3, 0.3]            # between calls, not before the first


def test_coverage_flags_suspicious_entries():
    today = D(2026, 10, 10)
    cache = {"OK": {"fetched": "2026-10-10",
                    "dates": ["2026-07-30", "2026-10-29"]},
             "PAST": {"fetched": "2026-10-10", "dates": ["2026-07-30"]},
             "FAR": {"fetched": "2026-10-10", "dates": ["2028-01-15"]},
             "DUPE": {"fetched": "2026-10-10",
                      "dates": ["2026-10-28", "2026-10-30", "2027-02-01"]},
             "ERR": {"fetched": "2026-10-10", "dates": [], "error": "x"}}
    c = er.coverage(cache, today)
    assert (c["names"], c["with_dates"], c["with_future"]) == (5, 4, 3)
    assert c["far"] == [("FAR", "2028-01-15")]
    assert c["near_dupes"] == [("DUPE", "2026-10-28", "2026-10-30")]
    assert c["no_dates"] == ["ERR"] and c["errors"] == ["ERR"]
    sub = er.coverage(cache, today, tickers=["OK", "NEW"])
    assert (sub["names"], sub["with_dates"], sub["no_dates"]) == \
        (2, 1, ["NEW"])


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
