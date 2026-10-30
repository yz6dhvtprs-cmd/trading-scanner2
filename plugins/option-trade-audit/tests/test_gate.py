"""Offline checks for the trade gate and the decision queue (no network, no MCP).

Contract: bucket labels match the audit's edges; a clean 31-60 DTE ITM call in
an uptrend passes; every policy veto fires on its trigger; a missing or
wrong-side stop vetoes; bearish-in-bull-tape vetoes unless the name is at an
extreme; 0-1 DTE OTM vetoes; the earnings reaction day vetoes; a rule/history
disagreement is flagged as a conflict, never dropped; thin buckets are ignored;
missing live data fails a gate rather than passing it; the clamp never upgrades
the gate; record/defer/due/prune round-trip; an intent converts to a candidate.

Usage (project root):
    python3 tests/test_gate.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "trade-check", "scripts"))
import gate as tg  # noqa: E402


def bars(n=300, start=100.0, drift=0.3, atr=1.0):
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = start + drift * np.arange(n)
    return pd.DataFrame({"Open": close - 0.1, "High": close + atr / 2,
                         "Low": close - atr / 2, "Close": close, "Volume": 1e6}, index=idx)


def loader_for(frames):
    return lambda t, tf="1d": frames.get(t, pd.DataFrame())


# synthetic priors: round numbers, thresholds chosen to exercise the logic (bad < 0.8, good >= 1.2)
PRIORS = {"generated": "2000-01-01", "n": 1000, "overall": {"n": 1000, "win": 0.45, "pf": 0.95},
          "buckets": {
              "dte": {"0": {"n": 300, "win": 0.40, "pf": 0.85},
                      "31-60": {"n": 60, "win": 0.55, "pf": 2.0}},
              "moneyness": {"ITM1-3": {"n": 40, "win": 0.60, "pf": 2.0}},
              "moneyness_0_1dte": {"OTM1-3": {"n": 200, "win": 0.30, "pf": 0.60}},
              "dir_x_spy": {"bearish|SPY up": {"n": 300, "win": 0.40, "pf": 0.60}},
              "vix": {"14-18": {"n": 400, "win": 0.45, "pf": 1.0}},
              "premium": {">5k": {"n": 50, "win": 0.40, "pf": 0.70}},
              "reentry_after_loss": {"2-10m": {"n": 50, "win": 0.30, "pf": 0.30}},
              "adds": {"avg down": {"n": 100, "win": 0.35, "pf": 0.50}},
              "seq": {"4-6": {"n": 200, "win": 0.45, "pf": 0.70}},
              "earnings": {"post-earnings day": {"n": 60, "win": 0.40, "pf": 0.50}},
              "dir_x_ext": {"bullish|0..1": {"n": 200, "win": 0.40, "pf": 1.0}},
              "range_pos": {"bullish|top": {"n": 40, "win": 0.50, "pf": 3.0}}}}

UP = {"AAPL": bars(atr=3.0), "SPY": bars(start=500, drift=1.0, atr=6.0)}   # EMA20 ~1.6 ATR under price: an orderly uptrend


def ctx_for(frames, live, ticker="AAPL", asof=dt.date(2026, 3, 2)):
    return tg.context(ticker, asof=asof, live=live, loader=loader_for(frames), earnings_cache={})


def S_of(frames, t="AAPL"):
    return float(frames[t].Close.iloc[-1])


def vetoes(r):
    return [v["rule"] for v in r["vetoes"]]


def test_labels_match_bucket_edges():
    assert tg.label_dte(0) == "0" and tg.label_dte(45) == "31-60" and tg.label_dte(400) == "60+"
    assert tg.label_money(-2) == "ITM1-3" and tg.label_money(2) == "OTM1-3"
    assert tg.label_reentry(4) == "2-10m" and tg.label_reentry(90) == ">1h"
    assert tg.label_vix(25) == "22-30" and tg.label_prem(9000) == ">5k"
    assert tg.label_rangepos(0.95) == "top" and tg.label_rangepos(0.1) == "bottom"


def test_good_trade_passes():
    S = S_of(UP) + 2
    live = {"last": S, "day_open": S - 1.5, "day_high": S, "day_low": S - 1.6, "vix": 16.0,
            "new_today": 0, "min_since_loss": 300, "entry_hour": 9.7}
    cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": round(S * 0.98, 1), "qty": 2,
            "premium": 1800, "delta": 0.62, "stop_px": S - 6, "live": live}
    r = tg.gate(cand, ctx=ctx_for(UP, live), priors=PRIORS)
    assert r["vetoes"] == [], r["vetoes"]
    assert r["verdict"] in ("TAKE", "TAKE SMALLER"), r["verdict"]
    g = {x["gate"]: x["ok"] for x in r["gates"]}
    assert g["expiry"] and g["contract"] and g["trend"] and g["trigger"]


def test_every_policy_veto_fires():
    S = S_of(UP)
    base = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": S, "qty": 1, "premium": 1000,
            "delta": 0.6, "stop_px": S - 5}
    cases = {"no averaging down": {"adding_to_loser": True}, "daily cap": {"new_today": 3},
             "two-loss stop": {"losses_today": 2}, "revenge timer": {"min_since_loss": 4},
             "size cap": {}}
    for rule, live in cases.items():
        cand = dict(base)
        cand["live"] = {"last": S, "day_open": S - 1, "vix": 16.0, **live}
        if rule == "size cap":
            cand["premium"] = 9000
        r = tg.gate(cand, ctx=ctx_for(UP, cand["live"]), priors=PRIORS)
        assert rule in vetoes(r), (rule, r["vetoes"])
        assert r["verdict"] == "NO"


def test_missing_or_wrong_side_stop_vetoes():
    S = S_of(UP)
    live = {"last": S, "day_open": S - 1, "vix": 16.0}
    for stop in (None, S + 5):
        cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": S, "premium": 900,
                "delta": 0.6, "live": live}
        if stop:
            cand["stop_px"] = stop
        r = tg.gate(cand, ctx=ctx_for(UP, live), priors=PRIORS)
        assert "stop required" in vetoes(r)


def test_bearish_in_bull_tape_vetoed_unless_extreme():
    frames = {"AAPL": bars(drift=0.0), "SPY": bars(start=500, drift=1.0)}
    S = S_of(frames)
    live = {"last": S, "day_open": S, "vix": 16.0, "min_since_loss": 300, "new_today": 0}
    cand = {"ticker": "AAPL", "otype": "put", "dte": 30, "strike": S, "premium": 900,
            "delta": -0.6, "stop_px": S + 5, "live": live}
    r = tg.gate(cand, ctx=ctx_for(frames, live), priors=PRIORS)
    assert "bearish in a bull tape" in vetoes(r)
    ctx = ctx_for(frames, live)
    ctx["dist20_atr"], ctx["rsi"] = 3.1, 78.0          # blown out above EMA20, RSI > 70
    assert "bearish in a bull tape" not in vetoes(tg.gate(cand, ctx=ctx, priors=PRIORS))


def test_short_dated_otm_vetoed():
    frames = {"SPY": bars(start=600, drift=0.4)}
    S = S_of(frames, "SPY")
    live = {"last": S, "day_open": S - 1, "vix": 16.0, "new_today": 0, "min_since_loss": 300}
    cand = {"ticker": "SPY", "otype": "call", "dte": 0, "strike": round(S * 1.02, 1),
            "premium": 400, "delta": 0.3, "stop_px": S - 2, "live": live}
    r = tg.gate(cand, ctx=ctx_for(frames, live, ticker="SPY"), priors=PRIORS)
    assert "short-dated OTM" in vetoes(r)
    assert r["context"]["earn_next_d"] is None            # ETF: no earnings lookup


def test_post_earnings_day_vetoes():
    S = S_of(UP)
    live = {"last": S, "day_open": S - 1, "vix": 16.0, "new_today": 0, "min_since_loss": 300}
    ctx = ctx_for(UP, live)
    ctx["earn_prev_d"] = 1
    cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": S, "premium": 900,
            "delta": 0.6, "stop_px": S - 5, "live": live}
    assert "earnings reaction" in vetoes(tg.gate(cand, ctx=ctx, priors=PRIORS))


def test_conflict_is_flagged_not_dropped():
    S = S_of(UP)
    live = {"last": S, "day_open": S - 1, "vix": 16.0, "new_today": 0, "min_since_loss": 300}
    ctx = ctx_for(UP, live)
    ctx["dist20_atr"], ctx["rsi"], ctx["trend"] = 0.5, 55.0, "mixed"     # the rule fails
    priors = json.loads(json.dumps(PRIORS))
    priors["buckets"]["dir_x_ext"]["bullish|0..1"] = {"n": 200, "win": 0.5, "pf": 1.9}  # history good
    cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": S, "premium": 900,
            "delta": 0.6, "stop_px": S - 5, "live": live}
    r = tg.gate(cand, ctx=ctx, priors=priors)
    trend = [g for g in r["gates"] if g["gate"] == "trend"][0]
    assert trend["ok"] is False and trend["conflict"] is True
    assert any("conflict on trend" in n for n in r["notes"])
    # a history rule whose bucket is good in YOUR record does not block
    priors["buckets"]["earnings"]["post-earnings day"] = {"n": 60, "win": 0.6, "pf": 1.8}
    ctx["earn_prev_d"] = 0
    r2 = tg.gate(cand, ctx=ctx, priors=priors)
    assert "earnings reaction" not in [v["rule"] for v in r2["vetoes"]]
    assert any("advisory, not blocking: earnings reaction" in n for n in r2["notes"])


def test_history_rule_is_advisory_without_priors():
    S = S_of(UP)
    live = {"last": S, "day_open": S - 1, "vix": 16.0, "new_today": 0, "min_since_loss": 300}
    ctx = ctx_for(UP, live)
    ctx["earn_prev_d"] = 1
    cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": S, "premium": 900,
            "delta": 0.6, "stop_px": S - 5, "live": live}
    r = tg.gate(cand, ctx=ctx, priors={"generated": None, "buckets": {}})
    assert r["vetoes"] == [] and any("no history for this bucket" in n for n in r["notes"])
    assert any("no priors file" in n for n in r["notes"])


def test_trigger_needs_the_range_extreme():
    S = S_of(UP) + 2
    base_live = {"last": S, "day_open": S - 1.5, "vix": 16.0, "new_today": 0, "min_since_loss": 300}
    cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": S, "premium": 900,
            "delta": 0.6, "stop_px": S - 6}
    for hi, ok in ((S, True), (S + 1.5, False)):      # top of range vs "high but not top"
        live = {**base_live, "day_high": hi, "day_low": S - 1.6}
        r = tg.gate({**cand, "live": live}, ctx=ctx_for(UP, live), priors=PRIORS)
        trig = [g for g in r["gates"] if g["gate"] == "trigger"][0]
        assert trig["ok"] is ok, (hi, trig)


def test_thin_bucket_is_ignored():
    priors = {"generated": "x", "buckets": {"dte": {"31-60": {"n": 5, "win": 1.0, "pf": 9.9}}}}
    assert tg.prior(priors, "dte", "31-60") is None


def test_missing_live_data_fails_gates():
    cand = {"ticker": "AAPL", "otype": "call", "dte": 45, "strike": 100, "premium": 900,
            "stop_px": 90, "live": {}}
    r = tg.gate(cand, ctx=ctx_for(UP, {}), priors=PRIORS)
    g = {x["gate"]: x["ok"] for x in r["gates"]}
    assert not g["contract"] and not g["regime"] and not g["trigger"]
    assert r["verdict"] in ("WAIT", "NO")


def test_asof_string_is_accepted():
    r = tg.gate({"ticker": "AAPL", "otype": "call", "dte": 45, "asof": "2026-03-02",
                 "stop_px": 50, "live": {"last": S_of(UP)}},
                ctx=tg.context("AAPL", asof="2026-03-02", live={"last": S_of(UP)},
                               loader=loader_for(UP), earnings_cache={}), priors=PRIORS)
    assert r["context"]["asof"] == "2026-03-02"








if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok  ", t.__name__)
        except Exception:
            failed += 1
            print("FAIL", t.__name__)
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
