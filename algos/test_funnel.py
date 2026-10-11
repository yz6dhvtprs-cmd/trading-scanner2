"""Offline checks for the swing funnel (no network).

Contract: features are causal; each family fires on its synthetic
pattern and nowhere else; fills book at the open only within 1 ATR of the
level and never through the stop; the chandelier only tightens; texts
and paper rows have the agreed shape.

Usage (project root, venv active):
    python algos/test_funnel.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import funnel as fn  # noqa: E402


def frame(closes, rng=1.0, vol=None, start="2024-01-02"):
    """Daily OHLCV from a close path: open = prior close, high/low = close
    +- rng (then widened to contain the open)."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(c, o) + rng
    lo = np.minimum(c, o) - rng
    v = np.full(len(c), 1_000_000.0) if vol is None else np.asarray(vol, float)
    idx = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c,
                         "Volume": v}, index=idx)


def trend(n=260, slope=0.3, noise=0.0, seed=1):
    rs = np.random.RandomState(seed)
    return 100 + slope * np.arange(n) + noise * rs.randn(n)


def test_features_causal_and_shapes():
    d = fn.features(frame(trend(300, noise=0.5)))
    for col in ("sq_on", "sq_days", "mom", "rng20", "vol5_20", "ret63",
                "rc_long", "rc_short", "sq_long", "sq_short", "adx", "bull"):
        assert col in d.columns
    # causality: truncating the history never changes earlier rows
    d2 = fn.features(frame(trend(300, noise=0.5))[:250])
    cols = ["sq_days", "mom", "rc_long", "sq_long", "rng20"]
    a = d[cols].iloc[:250].fillna(-9).to_numpy()
    b = d2[cols].fillna(-9).to_numpy()
    assert np.allclose(a.astype(float), b.astype(float))


def test_squeeze_release_fires_once_in_direction():
    # 230 bars of drift, 25 flat bars (the 20-day Bollinger needs a full
    # window to collapse inside the Keltner), then an up-thrust
    path = list(trend(230, slope=0.2))
    path += [path[-1]] * 25
    path += [path[-1] + 2.5 * k for k in range(1, 7)]
    d = fn.features(frame(path, rng=0.3))
    assert bool(d["sq_on"].iloc[254]) is True     # squeeze on at the end of the flat
    assert not d["sq_on"].iloc[:230].any()        # never during the drift
    sq = fn.family_signals(d, "SQ", 1)
    idx = np.flatnonzero(sq)
    assert len(idx) >= 1
    assert 255 <= idx[0] <= 261                   # fires on the release
    assert int(d["sq_days"].shift(1).iloc[idx[0]]) >= fn.SQ_MIN_DAYS
    assert not fn.family_signals(d, "SQ", -1).any()  # never the wrong way
    s = fn.setup_at(d, int(idx[0]), "SQ", 1)
    assert s["exit"] == "fixed" and s["exit_mult"] == 2.0 and s["risk"] > 0
    assert s["grade"] == "B+" and s["paused"] is False
    assert s["sq_days"] >= fn.SQ_MIN_DAYS and s["rvol"] is not None
    s["ticker"] = "AAA"
    line = fn.fmt_setup(s)
    assert f"- sq {s['sq_days']}d" in line
    assert ("+vol" in line) == (s["rvol"] >= fn.SQ_VOL_MARK)


def test_undercut_reclaim_long_and_mirror():
    path = list(trend(240, slope=0.1, noise=0.3, seed=3))
    base = path[-1]
    path += [base - 0.5] * 20            # 20-day floor near base-0.5
    lo20 = base - 0.5 - 1.0             # frame() lows sit rng below close
    # undercut day: close well under the 20-day low, then an up-close back
    # above it the next day
    path += [lo20 - 1.5, lo20 + 1.2]
    d = fn.features(frame(path, rng=1.0))
    rc = fn.family_signals(d, "RC", 1)
    assert bool(rc[-1]) is True          # the reclaim bar
    assert not bool(rc[-2])              # not the undercut bar itself
    s = fn.setup_at(d, len(d) - 1, "RC", 1)
    assert s["side"] == "long" and s["exit"] == "fixed" and s["time"] == 15
    # risk is the distance to the undercut low, ATR-clamped
    assert 0 < s["risk"] <= 3 * s["atr"] + 1e-9
    # mirror: overshoot of the 20-day high then a down-close back under
    path2 = list(-np.asarray(path) + 400)   # flip the path
    d2 = fn.features(frame(path2, rng=1.0))
    assert bool(fn.family_signals(d2, "RC", -1)[-1]) is True


def test_pb_bo_delegate_to_algo_masks():
    from combos import signal_mask
    d = fn.features(frame(trend(300, noise=1.0, seed=5)))
    assert np.array_equal(fn.family_signals(d, "PB", 1),
                          signal_mask(d, "pullback", 1, fn.ADX_MIN))
    assert np.array_equal(fn.family_signals(d, "BO", -1),
                          signal_mask(d, "breakout", -1, fn.ADX_MIN,
                                      fn.RVOL_MIN))
    assert fn.GRADES[("BO", "long")] == "B+" and ("BO", "short") in fn.PAUSED
    assert ("RC", "long") in fn.PAUSED and fn.GRADES[("SQ", "short")] == "B"
    # text policy: B+ and up are texted, B is paper only, C never
    p = {"ticker": "AAA", "family": "SQ", "side": "long", "level": 1.0,
         "risk": 0.5, "grade": "B+", "paused": False, "sq_days": 7,
         "rvol": 1.0}
    assert fn.texted(p) and not fn.texted(dict(p, grade="B"))
    assert not fn.texted(dict(p, grade="A", paused=True))
    txt = fn.setups_text([p, dict(p, ticker="BBB", grade="B")], "2026-10-12")
    assert "AAA" in txt and "BBB" not in txt


def test_simulate_fixed_target_and_time_stop():
    # flat tape, one signal, price then rallies to +2R: target hit
    path = [100.0] * 60 + [100 + 0.6 * k for k in range(1, 30)]
    d = fn.features(frame(path, rng=0.2))
    d["atr"] = 1.0
    d["sq_lo"] = 98.0  # risk = 2 (clamped 0.2..3 ATR)
    out = fn.simulate(d, np.array([59]), "SQ", 1, ("fixed", 2.0, 20))
    assert len(out) == 1
    t = out[0]
    assert t["entry_bar"] == 60 and abs(t["R"] - (2.0 - 2 * 2.5e-4 * 100 / 2)) < 1e-6
    assert t["hold"] >= 1
    # no move: closes at the time stop near 0R
    flat = fn.features(frame([100.0] * 100, rng=0.2))
    flat["atr"] = 1.0
    flat["sq_lo"] = 98.0
    out = fn.simulate(flat, np.array([59]), "SQ", 1, ("fixed", 2.0, 20))
    assert out[0]["exit_bar"] == 79 and abs(out[0]["R"]) < 0.05


def test_simulate_chase_guard_skips_gaps():
    path = [100.0] * 60 + [110.0] * 30
    df = frame(path, rng=0.2)
    df.iloc[60, df.columns.get_loc("Open")] = 110.0   # gaps 10 points at the fill
    df.iloc[60, df.columns.get_loc("High")] = 110.2
    d = fn.features(df)
    d["atr"] = 1.0
    assert fn.simulate(d, np.array([59]), "RC", 1, ("fixed", 2.0, 15)) == []
    # the same signal with a normal open fills
    d2 = fn.features(frame(path, rng=0.2))
    d2["atr"] = 1.0
    assert len(fn.simulate(d2, np.array([59]), "RC", 1, ("fixed", 2.0, 15))) == 1


def test_fill_decisions_open_rules():
    today = dt.date(2026, 10, 12)
    base = {"ticker": "AAA", "family": "SQ", "side": "long", "level": 100.0,
            "risk": 2.0, "atr": 1.0, "exit": "fixed", "exit_mult": 2.0,
            "time": 20, "grade": "B", "paused": False, "bull": True,
            "fill_date": "2026-10-12"}
    pend = [dict(base), dict(base, ticker="BBB"), dict(base, ticker="CCC"),
            dict(base, ticker="DDD", paused=True),
            dict(base, ticker="EEE", fill_date="2026-10-13"),
            dict(base, ticker="FFF", side="short")]
    prints = {"AAA": (100.8, 100.5, 1e6),   # inside the chase band: fill
              "BBB": (103.0, 101.5, 1e6),   # open 1.5 ATR over: invalid
              "CCC": (97.0, 97.9, 1e6),     # open under level-risk=98: invalid
              "DDD": (100.5, 100.5, 1e6), "EEE": (100.5, 100.5, 1e6),
              "FFF": (99.0, 99.5, 1e6)}
    fills, invalid = fn.fill_decisions(pend, prints, today)
    assert [f["ticker"] for f in fills] == ["AAA", "FFF"]
    a = fills[0]
    assert (a["entry"], a["stop"], a["target"]) == (100.5, 98.5, 104.5)
    f = fills[1]
    assert (f["entry"], f["stop"], f["target"]) == (99.5, 101.5, 95.5)
    assert sorted(p["ticker"] for p, _ in invalid) == ["BBB", "CCC"]
    assert "beyond 1 ATR" in dict((p["ticker"], w) for p, w in invalid)["BBB"]
    assert "stop" in dict((p["ticker"], w) for p, w in invalid)["CCC"]
    # no print -> untouched, missing day -> untouched
    assert fn.fill_decisions(pend, {}, today) == ([], [])
    row = fn.fnl_row(a, today)
    assert row["strategy"] == "FNL-SQ" and row["status"] == "ENTERED"
    assert row["target"] == 104.5 and row["regime"] == "bull"
    assert fn.fnl_plan(row["notes"]) == {"trail": None, "time": 20}
    trail_fill = dict(a, family="PB", exit="trail", exit_mult=2.5, time=40,
                      target="-")
    r2 = fn.fnl_row(trail_fill, today)
    assert fn.fnl_plan(r2["notes"]) == {"trail": 2.5, "time": 40}
    assert r2["target"] == "-" and "FNL-PB" == r2["strategy"]


def test_trail_only_tightens():
    assert fn.trail_stop("long", 95.0, 110.0, 2.0, 2.5) == 105.0
    assert fn.trail_stop("long", 106.0, 110.0, 2.0, 2.5) == 106.0  # never loosens
    assert fn.trail_stop("short", 105.0, 90.0, 2.0, 2.5) == 95.0
    assert fn.trail_stop("short", 94.0, 90.0, 2.0, 2.5) == 94.0


def test_watch_flags_and_text():
    # 25 flat bars after a climb with volume fading in the last 5:
    # squeeze on, base near the high, dry
    path = list(trend(230, slope=0.2)) + [146.0] * 25
    vol = [1e6] * 250 + [5e5] * 5
    d = fn.features(frame(path, rng=0.3, vol=vol))
    w = fn.watch_flags(d, len(d) - 1, rs_median=0.0)
    assert any(f.startswith("squeeze") for f in w["flags"])
    assert "base-hi" in w["flags"] and "dry" in w["flags"]
    assert w["bias"] == "long" and w["score"] >= 2.5
    txt = fn.watch_text({"AAA": w, "BBB": dict(w, flags=["spring"],
                                               score=1.0)}, 200)
    assert txt.startswith("FNL watch 2/200:")
    assert "squeeze: AAA" in txt and "spring: BBB" in txt
    # a plain uptrend is not a coil: no structural flag, only RS+
    q = fn.watch_flags(fn.features(frame(trend(300, slope=0.3))), 299)
    assert q["flags"] in ([], ["RS+"]) and q["score"] < fn.WATCH_MIN
    # the reclaim pattern shows up as a spring flag
    path = list(trend(240, slope=0.1, noise=0.3, seed=3))
    base_px = path[-1]
    path += [base_px - 0.5] * 20 + [base_px - 3.0, base_px - 0.3]
    s = fn.watch_flags(fn.features(frame(path, rng=1.0)), 261)
    assert "spring" in s["flags"]


def test_options_plan_and_setup_text():
    assert "debit spread 14-21" in fn.options_plan("RC", "long", "B")
    assert fn.options_plan("SQ", "short", "B").startswith("put debit spread")
    assert "60-90 DTE" in fn.options_plan("BO", "long", "A")
    assert "long call" in fn.options_plan("PB", "long", "A")
    assert "long call" not in fn.options_plan("PB", "long", "B+")
    p = {"ticker": "DELL", "family": "PB", "side": "long", "level": 586.4,
         "risk": 8.2, "grade": "B+", "paused": False}
    assert fn.fmt_setup(p).startswith(
        "[B+] - DELL LONG - trend pullback - lvl 586.4 - risk 8.2 - opt: ")
    assert fn.fmt_setup(dict(p, paused=True)).endswith("(paused, paper off)")


def test_log_rows_one_open_per_family(tmp=None):
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "log.csv")
    today = dt.date(2026, 10, 12)
    f = {"ticker": "AAA", "family": "SQ", "side": "long", "level": 100.0,
         "risk": 2.0, "atr": 1.0, "exit": "fixed", "exit_mult": 2.0,
         "time": 20, "grade": "B", "bull": True, "entry": 100.5,
         "stop": 98.5, "target": 104.5}
    r = fn.fnl_row(f, today)
    assert fn.log_rows([r], path) == 1
    assert fn.log_rows([r], path) == 0
    r2 = fn.fnl_row(dict(f, family="RC"), today)
    assert fn.log_rows([r2], path) == 1
    log = pd.read_csv(path)
    assert list(log.columns) == fn.LOG_COLS and len(log) == 2


def test_watch_state_roundtrip():
    import tempfile
    real = fn.FUNNEL_WATCH
    fn.FUNNEL_WATCH = os.path.join(tempfile.mkdtemp(), "w.json")
    try:
        assert fn.load_watch() == {}
        fn.save_watch({"asof": "2026-10-09", "watch": {"AAA": {"score": 1}},
                       "pending": []})
        assert fn.load_watch()["watch"]["AAA"]["score"] == 1
    finally:
        fn.FUNNEL_WATCH = real


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
