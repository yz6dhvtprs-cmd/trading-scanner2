"""Offline checks for the weekly anchor-box signal (no network).

Contract: week-n box + two inside closes + week-n+3 print inside the
box completes it; alert additionally needs price above weekly EMA20 and
weekly MACD bull above 0; above-EMA10 prints as a strength star.

Usage (project root, venv active):
    python scanner/test_weekly_box.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weekly_box import alert_line, anchor_box_complete  # noqa: E402
from weekly_box import evaluate, ticker_verdict  # noqa: E402


def weeks_frame(extra):
    """44-bar climb (warm MACD/EMAs) + the given (O,H,L,C) tail weeks."""
    base = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(44)]
    rows = base + extra
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="W-MON")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                         "Volume": 1000}, index=idx)


def box_tail():
    # anchor + two inside closes; frame ends at week n+2, live is n+3.
    return [(143.0, 146.0, 142.0, 143.0),
            (144.0, 145.0, 143.0, 144.0),
            (145.0, 146.0, 144.0, 145.0)]


def test_anchor_box_edges_inclusive():
    assert anchor_box_complete(146.0, 142.0, [142.0, 146.0, 144.0])
    assert not anchor_box_complete(146.0, 142.0, [143.0, 141.9, 144.0])
    assert not anchor_box_complete(146.0, 142.0, [143.0, 144.0, 146.1])


def test_full_pattern_alerts_with_star():
    f = weeks_frame(box_tail())
    r = evaluate(f, 145.2, live=True)
    assert r["box"] is True
    assert r["lower"] == 142.0 and r["upper"] == 146.0
    assert r["alert"] is True
    assert r["gates"]["above_ema10"] is True
    line = alert_line("DELL", r)
    assert line.startswith("DELL box 142.00\u2013146.00")
    assert "\u2605>EMA10" in line and "(live)" in line


def test_live_outside_kills_box():
    f = weeks_frame(box_tail())
    r = evaluate(f, 150.0, live=True)
    assert r["box"] is False and r["alert"] is False


def test_inside_close_outside_kills_box():
    tail = [(143.0, 146.0, 142.0, 143.0),
            (144.0, 145.0, 143.0, 140.0),  # n+1 breaks under
            (145.0, 146.0, 144.0, 145.0)]
    r = evaluate(weeks_frame(tail), 145.2, live=True)
    assert r["box"] is False and r["alert"] is False


def test_below_ema20_blocks_alert():
    f = weeks_frame(box_tail())
    r = evaluate(f, 130.0, live=True)  # under the box and the line
    assert r["box"] is False
    assert r["gates"]["above_ema20"] is False and r["alert"] is False


def test_macd_bear_blocks_alert():
    # long slide then a flat box: box completes, trend lines fail.
    rows = [(200 - i, 201 - i, 199 - i, 200 - i) for i in range(44)]
    rows += [(157.0, 158.0, 156.0, 157.0),
             (157.2, 158.0, 156.0, 157.2),
             (157.1, 158.0, 156.0, 157.1)]
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="W-MON")
    o, h, l, c = zip(*rows)
    f = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                      "Volume": 1000}, index=idx)
    r = evaluate(f, 157.0, live=True)
    assert r["box"] is True
    # flattening lifts MACD over its signal (bull) while still negative
    # and under the EMA20 — both backup gates hold the alert shut.
    assert r["gates"]["macd_bull"] is True
    assert r["gates"]["macd_pos"] is False
    assert r["gates"]["above_ema20"] is False and r["alert"] is False


def test_thin_history_gates_shut():
    rows = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(7)]
    rows += [(107.0, 108.0, 106.0, 107.0)] * 3  # flat inside-weeks
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="W-MON")
    o, h, l, c = zip(*rows)
    f = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                      "Volume": 1000}, index=idx)
    r = evaluate(f, 107.0, live=True)
    assert r["box"] is True  # pattern reads on any 3 bars ...
    assert r["alert"] is False  # ... but unsettled lines never page


def test_wick_outside_breaks_box():
    tail = [(143.0, 146.0, 142.0, 143.0),
            (144.0, 147.0, 143.0, 144.0),  # close in, high pokes out
            (145.0, 146.0, 144.0, 145.0)]
    r = evaluate(weeks_frame(tail), 145.2, live=True)
    assert r["box"] is False and r["alert"] is False


def test_fallback_uses_last_daily_close():
    f = weeks_frame(box_tail())
    dailies = pd.DataFrame(
        {"Close": [140.0, 144.4]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-09-24"),
                                pd.Timestamp("2026-09-25")]))
    r = ticker_verdict("DELL", f, dailies, asof=dt.date(2026, 9, 25))
    assert r["live"] is False and r["price"] == 144.4
    assert r["box"] is True


def test_weeks_asof_hides_future():
    from weekly_box import weeks_asof
    f = weeks_frame(box_tail())
    past = weeks_asof(f, dt.date(2024, 12, 31))
    assert len(past) == len(f)  # all history visible
    mid = f.index[20].date()
    assert len(weeks_asof(f, mid)) == 21
    assert weeks_asof(f, mid).index[-1].date() == mid


def test_span8_needs_seven_inside_weeks():
    tail = [(140.0, 150.0, 138.0, 140.0)]  # anchor
    tail += [(143.0, 146.0, 142.0, 143.0 + 0.2 * i) for i in range(6)]
    f = weeks_frame(tail)
    assert evaluate(f, 144.0, live=True, span=8)["box"] is True
    assert evaluate(f, 144.0, live=True, span=4)["box"] is True
    assert evaluate(f, 144.0, live=True)["box"] is True  # default 4
    assert evaluate(f, 144.0, live=True, span=2)["box"] is False


def test_weekly_batch_text_shape():
    from weekly_box import weekly_batch_text
    assert weekly_batch_text(["AAA", "BBB"], ["xyz"]) == \
        "Weekly Consolidation(4 bars) above EMA10:\nAAA\nBBB\n" \
        "Only above EMA20:\nxyz"
    assert weekly_batch_text(["AAA"], []) == \
        "Weekly Consolidation(4 bars) above EMA10:\nAAA"
    assert weekly_batch_text([], ["xyz"], span=8) == \
        "Weekly Consolidation(8 bars) above EMA20:\nxyz"


def test_fired_state_roundtrip():
    import tempfile
    import weekly_box as wb
    real, tmp = wb.WBOX_STATE, tempfile.mkdtemp()
    wb.WBOX_STATE = os.path.join(tmp, "wbox.json")
    try:
        assert wb.load_fired() == {}
        fired = {}
        wb.apply_refire(fired, "AAA", 1.0, 2.0, "2026-09-25")
        wb.save_fired(fired)
        assert wb.load_fired() == fired
    finally:
        wb.WBOX_STATE = real


def test_refire_suppresses_same_grind():
    from weekly_box import apply_refire, boxes_overlap
    assert boxes_overlap(34.0, 38.0, 33.0, 37.0)  # shared prices
    assert boxes_overlap(34.0, 38.0, 38.0, 42.0)  # edge touch counts
    assert not boxes_overlap(34.0, 38.0, 46.0, 49.0)  # escaped grind
    fired = {}
    assert apply_refire(fired, "MGM", 34.0, 38.0, "2026-03-09") is True
    assert apply_refire(fired, "MGM", 35.0, 38.0, "2026-03-16") is False
    assert apply_refire(fired, "MGM", 36.0, 40.0, "2026-03-23") is False
    assert apply_refire(fired, "MGM", 46.0, 49.0, "2026-06-22") is True
    assert fired["MGM"]["week"] == "2026-06-22"


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
