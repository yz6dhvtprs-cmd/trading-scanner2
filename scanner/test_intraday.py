"""Offline checks for intraday text batching (no network).

Contract: one 15-min run pages all its EMA names in a single text headed
"D_RSI 40-45, WRSI>55, EMA50", one ticker per line — never one text per
ticker.

Usage (project root, venv active):
    python scanner/test_intraday.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))

from intraday import ema_batch_text, live_touch_decisions  # noqa: E402


def test_batch_shape():
    assert ema_batch_text(["XYZ", "PQR", "ABC"]) == \
        "D_RSI 40-45, WRSI>55, EMA50\nXYZ\nPQR\nABC"


def test_batch_single():
    assert ema_batch_text(["AWK"]) == "D_RSI 40-45, WRSI>55, EMA50\nAWK"


def test_batch_one_ticker_per_line():
    body = ema_batch_text(["A", "B", "C", "D"])
    lines = body.split("\n")
    assert lines[0] == "D_RSI 40-45, WRSI>55, EMA50"
    assert lines[1:] == ["A", "B", "C", "D"]


TODAY = dt.date(2026, 9, 25)


def live_facts(**kw):
    d = {"ema50": 100.0, "atr": 2.0, "rsi": 42.0, "wrsi": 60.0,
         "shorts_below": True, "prev_close": 101.0}
    d.update(kw)
    return {"AAA": d}


def live_dst(alerted=None, ignored=None):
    return {"alerted": alerted or {}, "ignored": ignored or {}}


def decide(facts, quotes, dst):
    return live_touch_decisions(facts, quotes, dst, TODAY)


def test_live_touch_hits_inside_band():
    hits, parks, muted = decide(live_facts(), {"AAA": (100.5, 101.0)},
                                live_dst())
    assert (hits, parks, muted) == (["AAA"], [], [])


def test_live_touch_band_edges_inclusive():
    for live in (98.0, 102.0):  # exactly +-1 ATR still a touch
        hits, _, _ = decide(live_facts(), {"AAA": (live, 101.0)},
                            live_dst())
        assert hits == ["AAA"]
    hits, parks, muted = decide(live_facts(), {"AAA": (102.1, 101.0)},
                                live_dst())
    assert (hits, parks, muted) == ([], [], [])


def test_live_deep_break_parks():
    hits, parks, muted = decide(live_facts(), {"AAA": (95.0, 101.0)},
                                live_dst())
    assert (hits, parks, muted) == ([], ["AAA"], [])


def test_live_dedup_mutes():
    dst = live_dst(alerted={"AAA": TODAY.isoformat()})
    assert decide(live_facts(), {"AAA": (100.5, 101.0)},
                  dst) == ([], [], ["AAA"])


def test_live_stale_alert_pages_again():
    dst = live_dst(alerted={"AAA": "2026-09-01"})
    hits, _, _ = decide(live_facts(), {"AAA": (100.5, 101.0)}, dst)
    assert hits == ["AAA"]


def test_live_parked_stays_parked():
    dst = live_dst(ignored={"AAA": {"until": "2026-09-30"}})
    assert decide(live_facts(), {"AAA": (99.5, 101.0)},
                  dst) == ([], [], [])


def test_live_recovery_unparks_and_pages():
    dst = live_dst(ignored={"AAA": {"until": "2026-09-30"}})
    hits, parks, _ = decide(live_facts(), {"AAA": (100.5, 101.0)}, dst)
    assert (hits, parks) == (["AAA"], [])


def test_live_gates_mirror_daily():
    base = {"AAA": (100.5, 101.0)}
    assert decide(live_facts(rsi=39.9), base, live_dst())[0] == []
    assert decide(live_facts(rsi=45.1), base, live_dst())[0] == []
    assert decide(live_facts(wrsi=55.0), base, live_dst())[0] == []
    assert decide(live_facts(shorts_below=False), base,
                  live_dst())[0] == []
    assert decide(live_facts(prev_close=100.0), base,
                  live_dst())[0] == []  # at, not above
    assert decide(live_facts(), {"AAA": (100.5, 100.0)},
                  live_dst())[0] == []  # gapped open under the line


def test_live_missing_print_changes_nothing():
    assert decide(live_facts(), {}, live_dst()) == ([], [], [])


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
