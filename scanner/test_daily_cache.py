"""Offline checks for the rotating daily-bar cache (no network).

Contract: per-ticker CSVs keep the newest KEEP bars, the forming bar is
never stored, the 15-min loop pays network once a day (stamp gate), and a
rewritten adjusted history triggers a full re-bootstrap, not a merge.

Usage (project root, venv active):
    python scanner/test_daily_cache.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily_cache as dc

TODAY = dt.date(2026, 9, 18)  # a Friday: weekdays only, no holiday math


def setup_tmp():
    tmp = tempfile.mkdtemp(prefix="dcache-")
    dc.CACHE_ROOT = os.path.join(tmp, "scanner")
    return tmp


def frame(closes, end=None, ticker="AAA"):
    """MultiIndex batch-style frame (like yfinance group_by=ticker)."""
    end = end or (TODAY - dt.timedelta(days=1))
    idx = pd.bdate_range(end=end, periods=len(closes))
    cols = pd.MultiIndex.from_product([[ticker], ["Open", "High", "Low",
                                                  "Close", "Volume"]])
    data = [[c + 0.5, c + 1.0, c - 1.0, c, 1000.0] for c in closes]
    return pd.DataFrame(data, index=idx, columns=cols)


def closes(n, start=100.0, step=0.5):
    return [start + step * i for i in range(n)]


def dl_factory(px, calls=None):
    def dl(ts, **kw):
        if calls is not None:
            calls.append((list(ts), kw.get("period")))
        return px
    return dl


def test_store_load_roundtrip_and_rotation():
    setup_tmp()
    n = dc.store("AAA", frame(closes(15)), keep=10, today=TODAY)
    assert n == 10
    f = dc.load("AAA")
    assert len(f) == 10
    assert list(f.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert float(f["Close"].iloc[0]) == closes(15)[5]  # oldest dropped
    assert float(f["Close"].iloc[-1]) == closes(15)[-1]


def test_forming_bar_never_stored():
    setup_tmp()
    idx = pd.bdate_range(end=TODAY, periods=5)  # last bar IS today
    cols = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low",
                                                 "Close", "Volume"]])
    import numpy as np
    px = pd.DataFrame(np.full((5, 5), 100.0), index=idx, columns=cols)
    n = dc.store("AAA", px, today=TODAY)
    assert n == 4
    assert dc.load("AAA").index.max().date() < TODAY


def test_refresh_merges_and_stamp_gates_second_call():
    setup_tmp()
    calls = []
    old = frame(closes(30))  # through 09-17
    dc.store("AAA", old, today=TODAY)
    new = frame(closes(30) + [115.5, 116.0])  # overlap + 2 new bars
    s = dc.refresh(["AAA"], downloader=dl_factory(new, calls), today=TODAY)
    assert s["ok"] == ["AAA"] and not s["missing"] and not s["resynced"]
    f = dc.load("AAA")
    assert float(f["Close"].iloc[-1]) == 116.0
    assert len(f) == 32
    r = dc.sync_pool(["AAA"], today=TODAY,
                     downloader=dl_factory(new, calls))
    assert r["refreshed"] is True  # first sync: stamp was absent
    n_calls = len(calls)
    r2 = dc.sync_pool(["AAA"], today=TODAY,
                      downloader=dl_factory(new, calls))
    assert r2["refreshed"] is False and len(calls) == n_calls  # local-only


def test_rewritten_history_resyncs_instead_of_merging():
    setup_tmp()
    dc.store("AAA", frame(closes(30)), today=TODAY)  # closes ~100-114
    calls = []
    rewritten = frame([c * 0.5 for c in closes(30)])  # 2:1 split, same dates
    s = dc.refresh(["AAA"], downloader=dl_factory(rewritten, calls),
                   today=TODAY)
    assert s["resynced"] == ["AAA"] and not s["ok"]
    f = dc.load("AAA")
    assert float(f["Close"].iloc[-1]) < 60.0  # replaced, not merged
    assert any(p == "1y" for _, p in calls)  # full re-pull ran


def test_ensure_bootstraps_only_thin_tickers():
    setup_tmp()
    dc.store("AAA", frame(closes(300)), today=TODAY)
    full = frame(closes(300), ticker="BBB")
    got = dc.ensure(["AAA", "BBB"], min_bars=260,
                    downloader=dl_factory(full), today=TODAY)
    assert got == ["BBB"]
    assert len(dc.load("BBB")) == 300


def weekly_frame(dates):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    cols = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low",
                                                 "Close", "Volume"]])
    import numpy as np
    return pd.DataFrame(np.full((len(dates), 5), 100.0), index=idx,
                        columns=cols)


def test_weekly_cutoff_drops_current_week():
    setup_tmp()  # TODAY is Fri 09-18: current week starts Mon 09-14
    px = weekly_frame(["2026-09-07", "2026-09-14", "2026-09-21"])
    n = dc.store("AAA", px, today=TODAY, tf="1w")
    assert n == 1  # only the completed 09-07 week kept
    assert dc.load("AAA", "1w").index[-1].date() == dt.date(2026, 9, 7)


def test_hourly_cutoff_drops_current_hour():
    setup_tmp()
    now = dt.datetime(2026, 9, 18, 10, 45)
    idx = pd.date_range("2026-09-18 07:00", periods=5, freq="h")
    cols = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low",
                                                 "Close", "Volume"]])
    import numpy as np
    px = pd.DataFrame(np.full((5, 5), 100.0), index=idx, columns=cols)
    n = dc.store("AAA", px, today=now, tf="1h")
    assert n == 3  # 07/08/09 kept; 10 forming, 11 future dropped
    assert dc.load("AAA", "1h").index[-1] == pd.Timestamp("2026-09-18 09:00")


def test_timeframes_stored_separately():
    setup_tmp()
    dc.store("AAA", frame(closes(30)), today=TODAY)  # 1d only
    assert len(dc.load("AAA", "1w")) == 0
    assert dc.coverage(["AAA"], tf="1w")["missing"] == 1
    assert dc.coverage(["AAA"], tf="1d")["full"] == 0  # 30 < 200
    assert dc.coverage(["AAA"], tf="1d", min_bars=20)["full"] == 1


def test_hourly_tz_aware_strips_to_et_wall():
    import datetime as dtm
    setup_tmp()
    idx = pd.date_range("2026-09-18 14:00", periods=4, freq="h",
                        tz="America/New_York")  # 14..17 ET
    cols = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low",
                                                 "Close", "Volume"]])
    import numpy as np
    px = pd.DataFrame(np.full((4, 5), 100.0), index=idx, columns=cols)
    now = dtm.datetime(2026, 9, 18, 16, 30,
                       tzinfo=dtm.timezone.utc)  # 12:30 ET
    n = dc.store("AAA", px, today=now, tf="1h")
    assert n == 0  # 14..17 ET all after the 12:00 ET cutoff: nothing kept


def test_hourly_tz_aware_keeps_completed():
    import datetime as dtm
    setup_tmp()
    idx = pd.date_range("2026-09-18 09:00", periods=5, freq="h",
                        tz="America/New_York")  # 09..13 ET
    cols = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low",
                                                 "Close", "Volume"]])
    import numpy as np
    px = pd.DataFrame(np.full((5, 5), 100.0), index=idx, columns=cols)
    now = dtm.datetime(2026, 9, 18, 17, 30,
                       tzinfo=dtm.timezone.utc)  # 13:30 ET
    n = dc.store("AAA", px, today=now, tf="1h")
    assert n == 4  # 09..12 kept, 13:00 forming dropped
    assert dc.load("AAA", "1h").index[-1] == pd.Timestamp("2026-09-18 12:00")


def test_weekly_refresh_merges():
    setup_tmp()
    mon = dt.date(2026, 9, 21)  # next Monday: 09-14 week is completed
    dc.store("AAA", weekly_frame(["2026-09-07"]), today=mon, tf="1w")
    new = weekly_frame(["2026-09-07", "2026-09-14", "2026-09-21"])
    s = dc.refresh(["AAA"], downloader=dl_factory(new), today=mon,
                   tf="1w")
    assert s["ok"] == ["AAA"] and not s["missing"]
    f = dc.load("AAA", "1w")
    assert f.index[-1].date() == dt.date(2026, 9, 14)  # 09-21 forming cut


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
