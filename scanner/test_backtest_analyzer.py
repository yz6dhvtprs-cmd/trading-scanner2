"""Offline checks for the analyzer backtest harness (no network).

Covers: causal slicing (no future rows in any frame, forming-bar OHLC,
completed-1h-only cutoff) and change-only hit reporting, using synthetic
frames plus a stubbed gate function. Gate CONTENT stays covered by
scanner/test_analyze.py; this covers the harness around it.

Usage (project root, venv active):
    python scanner/test_backtest_analyzer.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_analyzer as _bt  # noqa: E402

ET = "America/New_York"


def _synth():
    days = list(pd.bdate_range("2026-08-10", periods=12, tz=ET))
    d = pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Volume": 1_000_000.0}, index=days)
    h1_idx, m15_idx = [], []
    for day in days[-4:]:
        base = day + pd.Timedelta(hours=9, minutes=30)
        for i in range(7):
            h1_idx.append(base + pd.Timedelta(hours=i))
        for i in range(26):
            m15_idx.append(base + pd.Timedelta(minutes=15 * i))
    h1 = pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Volume": 100_000.0}, index=pd.DatetimeIndex(h1_idx))
    m15 = pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Volume": 10_000.0}, index=pd.DatetimeIndex(m15_idx))
    return d, h1, m15


def t_slices_causal():
    d, h1, m15 = _synth()
    ts = m15.index[40]  # second day of the 15m window
    ds, h1s, m15s = _bt.slices_for(d, h1, m15, ts)
    assert ds.index.max().date() == ts.date(), ds.index.max()
    assert (m15s.index <= ts).all()
    # 1h bars complete by this 15m bar's close only
    assert (h1s.index <= ts - pd.Timedelta(minutes=45)).all()
    # daily slice never reaches past the bar's date
    assert (ds.index.date <= ts.date()).all()


def t_forming_bar_ohlc():
    d, h1, m15 = _synth()
    ts = m15.index[30]
    daybars = m15[(m15.index.date == ts.date()) & (m15.index <= ts)]
    ds, _, _ = _bt.slices_for(d, h1, m15, ts)
    last = ds.iloc[-1]
    assert last["Open"] == daybars["Open"].iloc[0]
    assert last["High"] == daybars["High"].max()
    assert last["Low"] == daybars["Low"].min()
    assert last["Close"] == daybars["Close"].iloc[-1]
    assert last["Volume"] == daybars["Volume"].sum()


def t_change_only_hits():
    d, h1, m15 = _synth()
    calls = {"n": 0}

    def stub(t, dd, hh, mm):
        calls["n"] += 1
        if calls["n"] <= 2:
            return [{"state": "Long", "price": 100.0, "target": "110.00",
                     "stop": 99.0, "note": "stub"}]
        return []

    old = _bt.analyze
    _bt.analyze = stub
    try:
        # shrink warmup minimums so the stub sees every bar
        o15, od = _bt.MIN_TF, _bt.MIN_DAILY
        _bt.MIN_TF, _bt.MIN_DAILY = 1, 1
        try:
            hits, stats = _bt.walk("T", d, h1, m15)
        finally:
            _bt.MIN_TF, _bt.MIN_DAILY = o15, od
    finally:
        _bt.analyze = old
    assert stats["scanned"] == len(m15), stats
    assert len(hits) == 1, [h["state"] for h in hits]  # Long fires twice
    assert hits[0]["state"] == "Long"
    assert hits[0]["id"] == 1 and "ts" in hits[0]
    assert hits[0]["grade"] == "B"  # stub note is not a backtested trigger
    # hit timestamp = bar close of the first firing bar (bars 0-2 skip: no
    # completed 1h bar yet, so evaluation starts at bar 3)
    assert hits[0]["time"] == m15.index[3] + pd.Timedelta(minutes=15)


def t_reappearing_state_rehits():
    d, h1, m15 = _synth()
    seq = [[{"state": "Long", "price": 1.0, "target": "2",
             "stop": 0.5, "note": "s"}],
           [],
           [{"state": "Possible upcoming Rejection", "price": 1.0,
             "target": "0.5", "stop": 1.5, "note": "s"}]]
    calls = {"n": 0}

    def stub(t, dd, hh, mm):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, 2)]

    old = _bt.analyze
    _bt.analyze = stub
    try:
        o15, od = _bt.MIN_TF, _bt.MIN_DAILY
        _bt.MIN_TF, _bt.MIN_DAILY = 1, 1
        try:
            hits, _ = _bt.walk("T", d, h1, m15)
        finally:
            _bt.MIN_TF, _bt.MIN_DAILY = o15, od
    finally:
        _bt.analyze = old
    states = [h["state"] for h in hits]
    assert states[0] == "Long", states
    assert "Possible upcoming Rejection" in states, states
    assert len(states) == 2, states  # no per-bar repeats


def t_fmt_shape():
    hit = {"id": 7, "time": pd.Timestamp("2026-09-08 16:15", tz=ET),
           "state": "Possible upcoming Rejection", "price": 100.0,
           "target": "90.00", "stop": 101.0, "note": "n",
           "grade": "C", "grade_why": "w"}
    line = _bt.fmt("XOM", hit)
    assert line.startswith('#7 2026-09-08 13:15PT "[C] XOM - Possible '
                           'upcoming Rejection - Short @ 100.0 - SL 101.0 - '
                           'target 90.00. (n)"'), line


def t_grades_mirror_main():
    # Long takes its backtested variant grade from algo.json
    g, why = _bt.grade_of("Long", "breakout trigger D/UP 1h/UP 15m/UP")
    assert g == "A", (g, why)
    g, why = _bt.grade_of("Long", "pullback trigger D/UP 1h/UP 15m/UP")
    assert g == "B+", (g, why)
    # short-bias rejection sits at C (paused), exactly like the main book
    g, why = _bt.grade_of("Possible upcoming Rejection", "doji at 20d-high")
    assert g == "C", (g, why)
    # reversal is a valid pattern with no backtested variant -> B (thin)
    g, why = _bt.grade_of("Possible upcoming Reversal", "hammer at support")
    assert g == "B", (g, why)


def t_parse_ids():
    assert _bt.parse_ids("1") == [1]
    assert _bt.parse_ids("1,3-4") == [1, 3, 4]
    assert _bt.parse_ids(" 2 - 3 ,2") == [2, 3]
    for bad in ("", "0", "x", "1-"):
        try:
            _bt.parse_ids(bad)
        except ValueError:
            continue
        raise AssertionError(f"parse_ids({bad!r}) should raise")


def t_trace_rejection_fires():
    import test_analyze as _ta
    from analyze import analyze as _analyze
    d, h1, m15 = _ta._rejection_frames()
    tr: dict = {}
    res = _analyze("T", d, h1, m15, trace=tr)
    assert [r["state"] for r in res] == ["Possible upcoming Rejection"], res
    assert tr["votes"] == {"D": "UP", "1h": "UP", "15m": "DN"}, tr["votes"]
    assert tr["fired"] == ["Possible upcoming Rejection"]
    fire = [c for c in tr["checks"] if "FIRE" in c]
    assert len(fire) == 1 and "20d-high" in fire[0], tr["checks"]
    assert any(c.startswith("base breakout:") for c in tr["checks"])
    assert tr["m15_last"], tr


def t_trace_skip_reasons():
    import test_analyze as _ta
    from analyze import analyze as _analyze
    d, h1, m15 = _ta._rejection_frames(m15_dn=False)  # 15m never flips
    tr: dict = {}
    res = _analyze("T", d, h1, m15, trace=tr)
    assert res == [], res
    assert tr["fired"] == []
    assert any("15m not DN" in c for c in tr["checks"]), tr["checks"]


TESTS = [t_slices_causal, t_forming_bar_ohlc, t_change_only_hits,
         t_reappearing_state_rehits, t_fmt_shape, t_parse_ids,
         t_trace_rejection_fires, t_trace_skip_reasons, t_grades_mirror_main]


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
