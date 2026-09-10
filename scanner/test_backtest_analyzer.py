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


def t_grade_filter():
    assert _bt.parse_grade("b") == "B"
    assert _bt.parse_grade("B+") == "B+"
    try:
        _bt.parse_grade("D")
    except ValueError:
        pass
    else:
        raise AssertionError("parse_grade('D') should raise")
    ranks = [_bt.grade_rank(g) for g in ("C", "B", "B+", "A", "A+")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 5, ranks
    assert _bt.grade_rank("B (thin)") == _bt.grade_rank("B")
    assert _bt.grade_rank("?") < _bt.grade_rank("C")

    def hit(i, g):
        return {"id": i, "grade": g}
    hits = [hit(1, "C"), hit(2, "B"), hit(3, "B+"), hit(4, "A"),
            hit(5, "A+"), hit(6, "?")]
    shown, hidden = _bt.filter_hits([dict(h) for h in hits], "B")
    assert [h["grade"] for h in shown] == ["B", "B+", "A", "A+"], shown
    assert hidden == 2, hidden  # C and ? stay out
    assert [h["id"] for h in shown] == [1, 2, 3, 4], "ids renumber shown rows"
    shown, hidden = _bt.filter_hits([dict(h) for h in hits], "")
    assert len(shown) == 6 and hidden == 0
    assert [h["id"] for h in shown] == [1, 2, 3, 4, 5, 6]
    shown, hidden = _bt.filter_hits([dict(h) for h in hits], "A+")
    assert [h["grade"] for h in shown] == ["A+"] and hidden == 5


def _r1_sig(state="Possible upcoming Reversal", key=("R1", "bottom")):
    return {"state": state, "price": 100.0, "target": "-", "stop": 99.0,
            "note": "123-bottom test", "algo": "R1", "sig_key": key}


def t_walk_r1_sigkey_change_only():
    d, h1, m15 = _synth()
    k1 = ("R1", "bottom", "d1", "d2", "d3")
    k2 = ("R1", "bottom", "d1", "d2", "d4-new")
    seq = [[_r1_sig(key=k1)], [_r1_sig(key=k1)], [_r1_sig(key=k2)]]
    calls = {"n": 0}

    def stub(_d, trace=None):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, 2)]

    old = _bt.r1_123
    _bt.r1_123 = stub
    try:
        o15, od = _bt.MIN_TF, _bt.MIN_DAILY
        _bt.MIN_TF, _bt.MIN_DAILY = 1, 1
        try:
            hits, _ = _bt.walk("T", d, h1, m15, algos={"R1"})
        finally:
            _bt.MIN_TF, _bt.MIN_DAILY = o15, od
    finally:
        _bt.r1_123 = old
    assert [h["id"] for h in hits] == [1, 2], hits  # repeat suppressed
    assert all(h["algo"] == "R1" and h["grade"] == "B" for h in hits)


def t_cooldown_suppresses_same_day_refire():
    # same pattern key flickering back within 26 bars is one setup, not two;
    # returning after the cooldown is a new setup.
    d, h1, m15 = _synth()
    k1 = ("R1", "bottom", "d1", "d2", "d3")
    seq = [[_r1_sig(key=k1)], [_r1_sig(key=k1)]] + [[ ]] * 30 + \
        [[_r1_sig(key=k1)]] * 70
    calls = {"n": 0}

    def stub(_d, trace=None):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    old = _bt.r1_123
    _bt.r1_123 = stub
    try:
        o15, od = _bt.MIN_TF, _bt.MIN_DAILY
        _bt.MIN_TF, _bt.MIN_DAILY = 1, 1
        try:
            hits, _ = _bt.walk("T", d, h1, m15, algos={"R1"})
        finally:
            _bt.MIN_TF, _bt.MIN_DAILY = o15, od
    finally:
        _bt.r1_123 = old
    assert [h["id"] for h in hits] == [1, 2], hits
    assert hits[0]["ts"] == m15.index[3]
    assert hits[1]["ts"] == m15.index[35], hits[1]["ts"]


def t_window_bars():
    _, _, m15 = _synth()  # 4 dates x 26 bars
    w = _bt.window_bars(m15, 2)
    assert len(w) == 52, len(w)
    assert w.index[0].date() == sorted(set(m15.index.date))[-2]
    assert w.index[-1] == m15.index[-1]


def t_eval_bar_dispatch():
    seen = []

    def mk(name):
        def stub(*a, **k):
            seen.append(name)
            return []
        return stub

    old = (_bt.analyze, _bt.r1_123, _bt.r2_zigzag_tema)
    _bt.analyze, _bt.r1_123, _bt.r2_zigzag_tema = \
        mk("OLD"), mk("R1"), mk("R2")
    try:
        _bt.eval_bar("OLD", "T", None, None, None)
        _bt.eval_bar("R1", "T", None, None, None)
        _bt.eval_bar("R2", "T", None, None, None)
    finally:
        (_bt.analyze, _bt.r1_123, _bt.r2_zigzag_tema) = old
    assert seen == ["OLD", "R1", "R2"], seen


def t_rps_pair_and_combo():
    # tournament pair wired: RPS resolves to the R3+R10 agreement combo,
    # which fires R3's signal only with same-bar/same-side R10 agreement.
    assert _bt.RPS_PAIR == ("R3", "R10"), _bt.RPS_PAIR
    assert _bt.parse_algos("rps") == {"RPS"}
    assert _bt.parse_algos("RPS") == {"RPS"}
    assert _bt.parse_algos("rps2") == {"RPS"}
    assert _bt.parse_algos("rps1") == {"R3+R10"}
    sig_a = {"state": "Possible upcoming Reversal", "price": 100.0,
             "stop": 98.0, "note": "tl-break", "sig_key": ("R3", "x")}
    sig_b = {"state": "Possible upcoming Reversal", "price": 100.5,
             "stop": 99.0, "note": "vo-div", "sig_key": ("R10", "y")}
    sig_c = {"state": "Possible upcoming Rejection", "price": 100.5,
             "stop": 102.0, "note": "vo-div", "sig_key": ("R10", "z")}
    old3, old10 = _bt.r3_trendline, _bt.r10_volosc
    try:
        _bt.r3_trendline = lambda *a, **k: [dict(sig_a)]
        _bt.r10_volosc = lambda *a, **k: [dict(sig_b)]
        got = _bt._eval_combo("R3+R10", "T", None, None, None, None)
        assert len(got) == 1 and got[0]["algo"] == "R3+R10", got
        assert got[0]["price"] == 100.0 and "R10-agree" in got[0]["note"]
        _bt.r10_volosc = lambda *a, **k: [dict(sig_c)]  # wrong side: no pair
        assert _bt._eval_combo("R3+R10", "T", None, None, None, None) == []
    finally:
        _bt.r3_trendline, _bt.r10_volosc = old3, old10


def t_eval_rps_two_step():
    # agreement + washout + turn -> pass; same agreement on a
    # never-washed frame -> filtered.
    import test_reversals as _tr
    sig = {"state": "Possible upcoming Reversal", "price": 100.0,
           "stop": 98.0, "note": "tl-break", "sig_key": ("R3", "x")}
    old3, old10 = _bt.r3_trendline, _bt.r10_volosc
    m15, h1 = _tr._intra_frames("long")
    flat = m15.copy()
    flat["Close"] = np.linspace(90, 100, 80)
    _bt.r3_trendline = lambda *a, **k: [dict(sig)]
    _bt.r10_volosc = lambda *a, **k: [dict(sig, sig_key=("R10", "y"))]
    try:
        got = _bt._eval_rps("T", None, h1, m15, None)
        assert len(got) == 1 and "rsi15-wash" in got[0]["note"], got
        assert _bt._eval_rps("T", None, h1, flat, None) == []
    finally:
        _bt.r3_trendline, _bt.r10_volosc = old3, old10


def t_score_hits():
    base = pd.Timestamp("2026-09-08 09:30", tz=ET)
    idx = [base + pd.Timedelta(minutes=15 * i) for i in range(30)]
    H = [100.0] * 6 + [100.5, 101.0, 102.5] + [100.5] * 21
    L = [100.0] * 6 + [99.5, 99.0, 99.5] + [99.5] * 21
    m15 = pd.DataFrame({"High": H, "Low": L,
                        "Open": 100.0, "Close": 100.0}, index=idx)
    hits = [
        {"id": 1, "ts": idx[5], "price": 100.0, "stop": 98.0,
         "state": "Possible upcoming Reversal"},   # +1R at bar 3
        {"id": 2, "ts": idx[5], "price": 100.0, "stop": 102.0,
         "state": "Possible upcoming Rejection"},  # SL at bar 3
        {"id": 3, "ts": idx[5], "price": 100.0, "stop": 90.0,
         "state": "Long"},                          # never touches
        {"id": 4, "ts": idx[28], "price": 100.0, "stop": 98.0,
         "state": "Long"},                          # window runs out
    ]
    got = {s["id"]: s for s in _bt.score_hits(hits, m15, fwd=10)}
    assert got[1]["outcome"] == "win" and got[1]["bars"] == 3, got[1]
    assert got[1]["mfe"] == 1.25, got[1]
    assert got[2]["outcome"] == "loss" and got[2]["bars"] == 3, got[2]
    assert got[3]["outcome"] == "open" and got[3]["mfe"] == 0.25, got[3]
    assert got[4]["outcome"] == "recent", got[4]


TESTS = [t_slices_causal, t_forming_bar_ohlc, t_change_only_hits,
         t_reappearing_state_rehits, t_fmt_shape, t_parse_ids,
         t_trace_rejection_fires, t_trace_skip_reasons, t_grades_mirror_main,
         t_grade_filter, t_walk_r1_sigkey_change_only, t_eval_bar_dispatch,
         t_score_hits, t_cooldown_suppresses_same_day_refire,
         t_window_bars, t_rps_pair_and_combo, t_eval_rps_two_step]


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
