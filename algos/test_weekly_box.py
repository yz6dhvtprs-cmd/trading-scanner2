"""Offline checks for the weekly anchor-box signal (no network).

Contract: week-n box + two inside closes + week-n+3 print inside the
box completes it; alert additionally needs price above weekly EMA20 and
weekly MACD bull above 0; above-EMA10 prints as a strength star. Tiers
(BUY/WATCH/EMA20), grades (A/B+/B), the breakout-watch decisions and
the paper-row shape are pure functions checked here on synthetic bars.

Usage (project root, venv active):
    python algos/test_weekly_box.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weekly_box import alert_line, anchor_box_complete  # noqa: E402
from weekly_box import evaluate, session_now, ticker_verdict  # noqa: E402


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


def test_wick_outside_passes_when_body_inside():
    tail = [(143.0, 146.0, 142.0, 143.0),
            (144.0, 147.0, 141.0, 144.0),  # wicks poke out, O+C inside
            (145.0, 146.0, 144.0, 145.0)]
    r = evaluate(weeks_frame(tail), 145.2, live=True)
    assert r["box"] is True and r["alert"] is True


def test_session_now_resolves():
    # regression: session rule lives in market_calendar (post-restructure
    # the Friday --scan path crashed importing it from ops/intraday).
    assert isinstance(session_now(), bool)


def test_open_outside_kills_box():
    tail = [(143.0, 146.0, 142.0, 143.0),
            (147.0, 147.5, 143.0, 144.0),  # open gaps out, close back in
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


def test_replay_universe_names_or_pool():
    from weekly_box import replay_universe
    from screener import scan_pool
    assert replay_universe(["dell"]) == ["DELL"]
    assert replay_universe([]) == scan_pool()
    assert len(scan_pool()) > 517 and "DELL" in scan_pool()


def test_week_ending_display():
    from weekly_box import week_ending
    assert week_ending(pd.Timestamp("2026-08-31")) == dt.date(2026, 9, 4)
    assert week_ending(pd.Timestamp("2026-09-14")) == dt.date(2026, 9, 18)


def test_weeks_asof_hides_future():
    from weekly_box import weeks_asof
    f = weeks_frame(box_tail())
    past = weeks_asof(f, dt.date(2024, 12, 31))
    assert len(past) == len(f)  # all history visible
    mid = f.index[20].date()  # a Monday stamp: that week is forming
    assert len(weeks_asof(f, mid)) == 20
    fri = mid + dt.timedelta(days=4)  # its Friday: still forming at noon
    assert len(weeks_asof(f, fri)) == 20
    assert weeks_asof(f, fri).index[-1].date() == f.index[19].date()


def test_replay_mirrors_live_window():
    # the Friday run sees three completed weeks + the live print; a
    # replay of week i must score weeks[:i] with week i's close, never
    # week i as both an inside bar and the print (a 3-week pattern).
    f = weeks_frame(box_tail())
    live = evaluate(f, 145.2, live=True)
    full = pd.concat([f, pd.DataFrame(
        {"Open": [145.1], "High": [146.0], "Low": [144.5],
         "Close": [145.2], "Volume": [1000]},
        index=[f.index[-1] + pd.Timedelta(days=7)])])
    i = len(full) - 1
    replay = evaluate(full.iloc[:i], float(full["Close"].iloc[i]),
                      live=False)
    assert (replay["box"], replay["alert"], replay["lower"],
            replay["upper"]) == (live["box"], live["alert"],
                                 live["lower"], live["upper"])
    both = evaluate(full.iloc[:i + 1], float(full["Close"].iloc[i]),
                    live=False)
    assert (both["lower"], both["upper"]) != (live["lower"],
                                              live["upper"])


def test_span8_needs_seven_inside_weeks():
    tail = [(140.0, 150.0, 138.0, 140.0)]  # anchor
    tail += [(143.0, 146.0, 142.0, 143.0 + 0.2 * i) for i in range(6)]
    f = weeks_frame(tail)
    assert evaluate(f, 144.0, live=True, span=8)["box"] is True
    assert evaluate(f, 144.0, live=True, span=4)["box"] is True
    assert evaluate(f, 144.0, live=True)["box"] is True  # default 4
    assert evaluate(f, 144.0, live=True, span=2)["box"] is False


def test_weekly_batch_text_shape():
    from weekly_box import buy_label, weekly_batch_text
    head = "Weekly Consolidation(4 bars) BUY zone (top of box, EMA10 rising)"
    assert weekly_batch_text(["AAA A*", "BBB B"], ["xyz"], ["qqq"]) == \
        f"{head}:\nAAA A*\nBBB B\nWatch (above EMA10):\nxyz\n" \
        "Only above EMA20:\nqqq"
    assert weekly_batch_text(["AAA A"], [], []) == f"{head}:\nAAA A"
    assert weekly_batch_text([], ["xyz"], [], span=8) == \
        "Weekly Consolidation(8 bars) watch (above EMA10):\nxyz"
    assert weekly_batch_text([], [], ["qqq"]) == \
        "Weekly Consolidation(4 bars) above EMA20:\nqqq"
    assert weekly_batch_text([], ["xyz"], ["qqq"]) == \
        "Weekly Consolidation(4 bars) watch (above EMA10):\nxyz\n" \
        "Only above EMA20:\nqqq"
    assert weekly_batch_text(["AAA A"], [], [], paused=True) == \
        f"{head} [PAUSED: paper off]:\nAAA A"
    assert buy_label("DELL", "A", True) == "DELL A*"
    assert buy_label("GILD", "B+", False) == "GILD B+"


def weeks_frame_v(extra):
    """weeks_frame with a per-row volume: (O, H, L, C, V) tuples."""
    base = [(100 + i, 101 + i, 99 + i, 100 + i, 1000) for i in range(44)]
    rows = base + extra
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="W-MON")
    o, h, l, c, v = zip(*rows)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                         "Volume": v}, index=idx)


def tight_tail():
    # same closes as box_tail (143, 144, 145: the gates read identically)
    # but a 2-point anchor range (~1 weekly ATR of the climb) and inside
    # weeks on half the anchor's volume: tight + drying = grade A.
    return [(143.0, 145.0, 143.0, 143.0, 4000),
            (144.0, 145.0, 143.5, 144.0, 2000),
            (145.0, 145.0, 144.0, 145.0, 2000)]


def test_features_grade_and_tiers():
    from weekly_box import classify
    r = evaluate(weeks_frame_v(tight_tail()), 144.9, live=True)
    assert r["alert"] is True
    f = r["features"]
    assert abs(f["pos"] - 0.95) < 1e-6  # (144.9 - 143) / 2
    assert f["vol_ratio"] == 0.5 and f["width_atr"] <= 1.3
    assert f["e10_rising"] is True and isinstance(f["hist_rising"], bool)
    assert (r["tier"], r["grade"]) == ("BUY", "A")
    # same box, print near the low: WATCH, grade unchanged
    low = evaluate(weeks_frame_v(tight_tail()), 143.2, live=True)
    assert low["alert"] is True and low["tier"] == "WATCH"
    assert low["grade"] == "A" and abs(low["features"]["pos"] - 0.1) < 1e-6
    # the default frame: wide box (~2 ATRs), flat volume -> BUY, grade B
    wide = evaluate(weeks_frame(box_tail()), 145.2, live=True)
    assert (wide["tier"], wide["grade"]) == ("BUY", "B")
    assert wide["features"]["vol_ratio"] == 1.0
    # classify on bare dicts: EMA20-only tier, B+ on one marker, NaN fails
    nan = float("nan")
    ok = {"pos": 0.9, "e10_rising": True, "width_atr": 1.0,
          "vol_ratio": 0.5}
    assert classify({"gates": {"above_ema10": False},
                     "features": ok}) == ("EMA20", "A")
    assert classify({"gates": {"above_ema10": True},
                     "features": dict(ok, vol_ratio=1.2)}) == ("BUY", "B+")
    assert classify({"gates": {"above_ema10": True},
                     "features": dict(ok, width_atr=nan, vol_ratio=nan)}
                    ) == ("BUY", "B")
    assert classify({"gates": {"above_ema10": True},
                     "features": dict(ok, e10_rising=False)}
                    )[0] == "WATCH"
    assert classify({"gates": {"above_ema10": True},
                     "features": dict(ok, pos=0.59)})[0] == "WATCH"
    line = alert_line("DELL", r)
    assert "BUY/A" in line and "pos=0.95" in line and "vol=0.50" in line


def test_breakout_decisions():
    from weekly_box import breakout_decisions, breakout_text
    today = dt.date(2026, 10, 12)
    box = {"lower": 100.0, "upper": 110.0, "week": "2026-10-09"}
    watch = {"AAA": dict(box, tier="BUY", grade="A"), "BBB": dict(box),
             "CCC": dict(box), "DDD": dict(box),
             "EEE": dict(box, paged="2026-10-09"),
             "FFF": dict(box, week="2026-08-01"), "GGG": dict(box)}
    prints = {"AAA": (111.0, 109.0, 600000.0), "BBB": (99.0, 101.0, 1e6),
              "CCC": (114.0, 109.0, 1e6), "DDD": (111.0, 109.0, 100000.0),
              "EEE": (111.0, 109.0, 1e6), "FFF": (111.0, 109.0, 1e6),
              "GGG": (105.0, 104.0, 1e6)}
    avg = {t: 1e6 for t in watch}
    pages, failed, missed = breakout_decisions(watch, prints, avg, today,
                                               elapsed=0.25)
    # AAA: 600k a quarter into the day on a 1M average = RVOL 2.4
    assert [p["ticker"] for p in pages] == ["AAA"]
    p = pages[0]
    assert (p["stop"], p["target"], p["rvol"], p["tier"]) == (
        105.0, 120.0, 2.4, "BUY")
    assert failed == ["BBB"]  # printed under the box low
    assert missed == ["CCC"]  # 3.6% over the top: chased, dropped
    # DDD quiet break (RVOL 0.4), EEE already paged, FFF aged out, GGG
    # still inside the box: none of them page
    assert breakout_decisions({"AAA": watch["AAA"]}, prints, {},
                              today)[0] == []  # unknown volume: no page
    assert breakout_decisions(watch, {}, avg, today) == ([], [], [])
    assert breakout_text(pages) == \
        "WBOX breakout (stop = box mid):\n" \
        "AAA 111.00 rvol 2.4 stop 105.00 tgt 120.00"


def test_watch_prune_and_roundtrip():
    import tempfile
    import weekly_box as wb
    today = dt.date(2026, 10, 12)
    w = {"OLD": {"lower": 1, "upper": 2, "week": "2026-09-01"},
         "NEW": {"lower": 1, "upper": 2, "week": "2026-10-09"},
         "BAD": {"lower": 1, "upper": 2, "week": "soon"}}
    assert list(wb.prune_watch(w, today)) == ["NEW"]
    real, tmp = wb.WBOX_WATCH, tempfile.mkdtemp()
    wb.WBOX_WATCH = os.path.join(tmp, "watch.json")
    try:
        assert wb.load_watch() == {}
        wb.save_watch({"NEW": w["NEW"]})
        assert wb.load_watch() == {"NEW": w["NEW"]}
    finally:
        wb.WBOX_WATCH = real


def test_wbox_rows_and_log_dedupe():
    import tempfile
    from weekly_box import LOG_COLS, log_signal_rows, wbox_row
    today = dt.date(2026, 10, 9)
    r = wbox_row("DELL", 525.85, 595.51, 586.38, today, "A",
                 note="pos=0.87")
    assert (r["strategy"], r["side"], r["status"], r["grade"]) == (
        "WBOX", "long", "ENTERED", "A")
    assert r["stop"] == 525.85 and r["target"] == 665.17
    assert r["notes"] == "box 525.85-595.51 pos=0.87"
    bo = wbox_row("DELL", 525.85, 595.51, 596.4, today, "A",
                  strategy="WBOX-BO")
    assert bo["stop"] == 560.68 and bo["target"] == 665.17  # midpoint stop
    path = os.path.join(tempfile.mkdtemp(), "log.csv")
    assert log_signal_rows([r, bo], path) == 2
    assert log_signal_rows([r], path) == 0  # same open box: skipped
    log = pd.read_csv(path)
    assert list(log.columns) == LOG_COLS and len(log) == 2
    assert log["outcome_R"].isna().all()


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
