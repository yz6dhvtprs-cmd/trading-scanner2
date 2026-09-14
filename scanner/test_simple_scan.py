"""Offline checks for the EMA50+RSI40 pullback scanner (no network).

Contract: Close below plain EMA 8/13/21, within 1xATR of EMA50, RSI14
inside target +/- tol (default 40 +/- 2). Covers setup_row edges, the
plain-EMA (not DEMA) indicator, and first-bar-only fresh_signals.

Usage (project root, venv active):
    python scanner/test_simple_scan.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple_scan import add_ind, ema, fresh_signals, live_sim, mark_pages
from simple_scan import setup_row


def row(close=100.0, e8=101.0, e13=102.0, e21=103.0, e50=100.5,
        atr=2.0, rsi=40.0, wrsi=60.0) -> dict:
    return {"Close": close, "ema8": e8, "ema13": e13, "ema21": e21,
            "ema50": e50, "atr": atr, "rsi": rsi, "wrsi": wrsi}


def prv(r, prev_close=101.0, **kw):
    """setup_row with an explicit previous close (strictly-above gate)."""
    return setup_row(r, prev_close=prev_close, **kw)


def test_prev_close_gate():
    assert prv(row()) is True
    assert prv(row(), 100.5) is False  # at the line, not above
    assert prv(row(), 99.0) is False   # below the line
    assert prv(row(), None) is False   # first bar: no previous close


def test_valid_bar():
    assert prv(row()) is True


def test_rsi_band_edges():
    assert prv(row(rsi=38.0)) is True
    assert prv(row(rsi=42.0)) is True
    assert prv(row(rsi=37.9)) is False
    assert prv(row(rsi=42.1)) is False
    assert prv(row(rsi=61.5)) is False  # hot: not a 40-zone pullback


def test_rsi_off():
    assert prv(row(rsi=61.5), rsi_on=False) is True


def test_weekly_gate():
    assert prv(row(wrsi=56.0)) is True
    assert prv(row(wrsi=55.0)) is False  # strictly above 55
    assert prv(row(wrsi=50.0)) is False
    assert prv(row(wrsi=50.0), rsi_on=False) is True
    assert prv(row(wrsi=56.0), wrsi_min=60.0) is False


def test_weekly_rsi_flat_is_50():
    d = add_ind(frame(150))
    assert (d["wrsi"].iloc[-20:] == 50.0).all()


def test_weekly_rsi_ramp_is_hot():
    idx = pd.bdate_range("2026-01-01", periods=150)
    d = add_ind(pd.DataFrame({
        "Open": 1.0, "High": 2.0, "Low": 0.5,
        "Close": [100.0 + 0.5 * i for i in range(150)],
        "Volume": 1000}, index=idx))
    assert d["wrsi"].iloc[-1] > 55.0


def test_weekly_rsi_forming_week_moves_same_day():
    idx = pd.bdate_range("2026-01-01", periods=100)
    d = add_ind(pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0,
        "Close": [100.0] * 99 + [110.0], "Volume": 1000}, index=idx))
    assert d["wrsi"].iloc[-2] == 50.0
    assert d["wrsi"].iloc[-1] > 50.0


def test_custom_band():
    assert prv(row(rsi=50.0), rsi_target=50.0, rsi_tol=2.0) is True
    assert prv(row(rsi=47.9), rsi_target=50.0, rsi_tol=2.0) is False


def test_short_trend_gate():
    assert prv(row(close=102.5)) is False  # above ema13
    assert prv(row(close=103.5)) is False  # above ema21 too


def test_value_band():
    assert prv(row(close=98.4)) is False  # >1 ATR under ema50
    assert prv(row(close=102.4)) is False  # >1 ATR over ema50


def test_plain_ema_not_dema():
    # rising ramp: plain EMA trails price, faster spans sit higher
    s = pd.Series([float(i) for i in range(1, 201)])
    e8, e50 = ema(s, 8).iloc[-1], ema(s, 50).iloc[-1]
    assert e8 > e50
    assert e50 < s.iloc[-1]
    assert ema(s, 50).iloc[-1] == s.ewm(span=50, adjust=False).mean().iloc[-1]


def frame(n=120, rsi=40.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
        "Volume": 1000}, index=idx)


def test_add_ind_columns():
    d = add_ind(frame())
    for c in ("ema8", "ema13", "ema21", "ema50", "rsi", "atr", "wrsi"):
        assert c in d.columns, c
    assert (d["ema50"] == 100.0).all()  # flat: EMA pins to price


def test_fresh_first_bar_only():
    d = add_ind(frame())
    # only the last 3 bars are valid setups (below shorts, on value,
    # rsi 40); earlier bars fail the RSI band so the run starts in-window
    d["rsi"] = 60.0
    for c in ("ema8", "ema13", "ema21"):
        d[c] = 101.0
    d["ema50"] = 100.0
    d["atr"] = 2.0
    d["wrsi"] = 60.0
    d.loc[d.index[-4], "Close"] = 101.0  # pre-run bar above the line
    d.loc[d.index[-3:], "rsi"] = 40.0
    sig = fresh_signals(d, 10)
    hits = sig.index[sig["sig"]].tolist()
    assert len(hits) == 1 and hits[0] == sig.index[-3]


def test_mark_pages_gap():
    assert mark_pages([]) == []
    assert mark_pages([10]) == [True]
    assert mark_pages([10, 12]) == [True, False]   # 2 sessions later: mute
    assert mark_pages([10, 13]) == [True, False]   # exactly 3: still mute
    assert mark_pages([10, 14]) == [True, True]    # 4 sessions: page again
    assert mark_pages([0, 1, 2, 3, 4]) == [True, False, False,
                                           False, True]


def sim_frame():
    # 12 business days, no holidays: closes sit above the 50 line so the
    # first-touch gate passes; validity is forced per-bar below
    idx = pd.bdate_range("2026-10-05", periods=12)
    d = add_ind(pd.DataFrame({
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Volume": 1000}, index=idx))
    for c in ("ema8", "ema13", "ema21"):
        d[c] = 101.0
    d["ema50"] = 100.0
    d["atr"] = 2.0
    d["rsi"] = 60.0
    d["wrsi"] = 60.0
    return d


def sim_days(d):
    import datetime as dt
    start = d.index[0].date()
    stop = d.index[-1].date() + dt.timedelta(days=2)
    out, day = [], start
    while day <= stop:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def test_live_sim_page_mute_page():
    d = sim_frame()
    d.loc[d.index[[2, 3, 7]], "rsi"] = 40.0
    rows = [(r["text"], r["bar"], r["status"])
            for r in live_sim(d, sim_days(d))]
    assert rows == [("2026-10-08", "2026-10-07", "PAGED"),
                    ("2026-10-09", "2026-10-08", "MUTED-DEDUP"),
                    ("2026-10-15", "2026-10-14", "PAGED")]


def test_live_sim_park_silence_recover():
    # crash bar parks; the next bars arrive from below the line so the
    # first-touch gate keeps them silent; arrival from above pages
    d = sim_frame()
    d["atr"] = 5.0
    d.loc[d.index[2], "Close"] = 90.0    # deep break: parks on 10-08 eval
    d.loc[d.index[3], "Close"] = 100.0
    d.loc[d.index[[3, 4, 5]], "rsi"] = 40.0
    rows = [(r["text"], r["status"], r["detail"])
            for r in live_sim(d, sim_days(d))]
    assert rows == [("2026-10-13", "PAGED", "text would go out")]


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
