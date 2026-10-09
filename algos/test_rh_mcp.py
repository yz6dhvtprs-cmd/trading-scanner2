"""Offline checks for the Robinhood-MCP hybrid seam (no network).

Covers chunk batching (the array shape RH accepts), the yfinance->RH
interval map (incl. the 15m aggregation flag), period->range conversion,
scalar-only call plans (regression: the agent bridge rejects array
params), result merging, local-fallback parity with simple_scan, and
the drift comparator.

Usage (project root, venv active):
    python algos/test_rh_mcp.py
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rh_mcp
from rh_mcp import aggregate_bars, apply_remote, array_specs
from rh_mcp import call_plan, chunk, compare, load_overrides
from rh_mcp import local_technicals, merge_technicals, needs_aggregation
from rh_mcp import period_to_range, rh_interval, to_15m
from simple_scan import add_ind


def t_chunk_sizes():
    assert chunk([f"T{i}" for i in range(25)], 10) == [
        [f"T{i}" for i in range(10)], [f"T{i}" for i in range(10, 20)],
        [f"T{i}" for i in range(20, 25)]]
    assert chunk([], 10) == []
    assert chunk(["aapl", "AAPL", " msft "]) == [["AAPL", "MSFT"]]


def t_chunk_default_cap():
    assert all(len(b) <= rh_mcp.RH_MAX_SYMBOLS
               for b in chunk([f"T{i}" for i in range(517)]))
    assert len(chunk([f"T{i}" for i in range(517)])) == 52


def t_interval_map():
    assert rh_interval("1d") == ("day", 1)
    assert rh_interval("1h") == ("hour", 1)
    assert rh_interval("1wk") == ("week", 1)
    assert rh_interval("15m") == ("5minute", 3)
    assert needs_aggregation("15m") is True
    assert needs_aggregation("1d") is False
    try:
        rh_interval("3m")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown interval should raise")


def t_period_range():
    s, e = period_to_range("5d")
    assert s.endswith("Z") and e.endswith("Z") and s < e
    s2, e2 = period_to_range("2y")
    import datetime as dt
    f = lambda v: dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    assert (f(e2) - f(s2)).days == 730
    try:
        period_to_range("9d")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown period should raise")


def t_call_plan_scalar_only():
    specs = call_plan(["AAPL", "msft"], "intraday",
                      start_time="2026-09-01T00:00:00Z")
    assert len(specs) == 2 * 7  # 4 emas + rsi + atr + weekly rsi per name
    wrsi = [s for s in specs if s["params"]["interval"] == "week"]
    assert len(wrsi) == 2 and all(
        s["params"]["type"] == "rsi" for s in wrsi)
    for s in specs:
        assert s["tool"] == "get_equity_technical_indicators"
        for v in s["params"].values():
            assert not isinstance(v, (list, dict)), \
                f"array param would be bridge-rejected: {s!r}"
    macd = [s for s in call_plan(["AAPL"], "weekly",
                                 start_time="2025-10-02T00:00:00Z")
            if s["params"]["type"] == "macd"]
    assert macd and macd[0]["params"]["fast_period"] == 12
    assert macd[0]["params"]["slow_period"] == 26
    assert macd[0]["params"]["signal_period"] == 9
    try:
        call_plan(["AAPL"], "nope")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown preset should raise")


def t_array_specs_batched():
    specs = array_specs([f"T{i}" for i in range(25)],
                        "get_equity_historicals",
                        {"interval": "day"})
    assert len(specs) == 3
    assert all(len(s["params"]["symbols"]) <= 10 for s in specs)
    assert specs[0]["params"]["interval"] == "day"


def t_merge_fixture():
    results = [
        {"data": {"symbol": "AAPL", "interval": "day", "bounds": "regular",
                  "indicators": [
                      {"type": "rsi", "params": {"period": 14},
                       "series": [{"begins_at": "2026-10-01T00:00:00Z",
                                   "value": 53.10}]},
                      {"type": "ema", "params": {"period": 50},
                       "series": [{"begins_at": "2026-10-01T00:00:00Z",
                                   "value": 321.5}]},
                      {"type": "macd",
                       "params": {"fast_period": 12, "signal_period": 9,
                                  "slow_period": 26},
                       "series": [{"begins_at": "2026-10-01T00:00:00Z",
                                   "histogram": -0.99, "macd": 4.08,
                                   "signal": 5.07}]}]}}]
    m = merge_technicals(results)
    assert m["AAPL"]["rsi"] == 53.10
    assert m["AAPL"]["ema50"] == 321.5
    assert m["AAPL"]["macd"] == 4.08
    assert m["AAPL"]["signal"] == 5.07
    assert merge_technicals([{"data": {"symbol": "", "indicators": []}}]) == {}


def t_local_parity():
    import math
    idx = pd.date_range("2026-01-05", periods=120, freq="B")
    close = pd.Series([100.0 + i * 0.3 + (i % 5) for i in range(120)],
                      index=idx)
    frame = pd.DataFrame({"Open": close - 0.5, "High": close + 1.0,
                          "Low": close - 1.0, "Close": close,
                          "Volume": 1_000_000})
    ref = add_ind(frame).iloc[-1]
    got = local_technicals(frame)
    for k in ("ema8", "ema13", "ema21", "ema50", "rsi", "atr"):
        assert math.isclose(got[k], float(ref[k]), rel_tol=1e-9), k


def t_compare():
    assert compare({"rsi": 50.0}, {"rsi": 50.5})["ok"] is True
    # RSI is absolute points: 2.5pts inside a 5-wide band is fine even
    # though it reads 5%+ relative.
    assert compare({"rsi": 41.0}, {"rsi": 43.5})["ok"] is True
    assert compare({"rsi": 41.0}, {"rsi": 45.0})["breaches"] == ["rsi"]
    bad = compare({"rsi": 50.0, "ema50": 100.0},
                  {"rsi": 50.0, "ema50": 110.0})
    assert bad["ok"] is False and bad["breaches"] == ["ema50"]
    assert compare({"rsi": 50.0}, {})["drift"] == {}


def _bars_5m():
    idx = pd.to_datetime(["2026-10-02 09:30", "2026-10-02 09:35",
                          "2026-10-02 09:40", "2026-10-02 09:45",
                          "2026-10-02 09:50", "2026-10-02 09:55"])
    return pd.DataFrame({"Open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
                         "High": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
                         "Low": [99.5, 100.5, 101.5, 102.5, 103.5, 104.5],
                         "Close": [100.2, 101.2, 102.2, 103.2, 104.2, 105.2],
                         "Volume": [10, 20, 30, 40, 50, 60]}, index=idx)


def t_aggregate_15m_values():
    agg = to_15m(_bars_5m())
    assert len(agg) == 2
    first, second = agg.iloc[0], agg.iloc[1]
    assert first["Open"] == 100.0 and first["Close"] == 102.2
    assert first["High"] == 102.5 and first["Low"] == 99.5
    assert first["Volume"] == 60
    assert second["Open"] == 103.0 and second["Close"] == 105.2
    assert second["Volume"] == 150
    assert str(agg.index[0])[11:16] == "09:30"
    assert str(agg.index[1])[11:16] == "09:45"


def t_aggregate_edges():
    assert len(to_15m(_bars_5m().iloc[:1])) == 1  # partial bucket kept
    novol = _bars_5m().drop(columns=["Volume"])
    assert "Volume" not in to_15m(novol).columns
    rev = _bars_5m().iloc[::-1]
    assert to_15m(rev).index.is_monotonic_increasing
    assert len(aggregate_bars(_bars_5m().iloc[:0])) == 0
    try:
        aggregate_bars(_bars_5m().drop(columns=["Close"]))
    except ValueError:
        pass
    else:
        raise AssertionError("missing Close should raise")


def t_merge_wrsi():
    m = merge_technicals([{"data": {
        "symbol": "AAPL", "interval": "week", "indicators": [
            {"type": "rsi", "params": {"period": 14},
             "series": [{"begins_at": "2026-09-21T00:00:00Z",
                         "value": 58.5}]}]}}])
    assert m["AAPL"]["wrsi"] == 58.5
    assert "rsi" not in m["AAPL"]


def _facts_row():
    return {"ema50": 100.0, "atr": 2.0, "rsi": 42.0, "wrsi": 60.0,
            "shorts_below": True, "prev_close": 101.0}


def t_apply_remote_full():
    facts = {"AAL": _facts_row()}
    rem = {"AAL": {"ema50": 102.0, "atr": 2.5, "rsi": 44.0,
                   "wrsi": 61.0, "junk": "x"}}
    out = apply_remote(facts, rem)
    assert out["AAL"]["ema50"] == 102.0
    assert out["AAL"]["atr"] == 2.5 and out["AAL"]["rsi"] == 44.0
    assert out["AAL"]["wrsi"] == 61.0
    assert out["AAL"]["_src"] == "rh"
    # local-only context keys survive untouched
    assert out["AAL"]["shorts_below"] is True
    assert out["AAL"]["prev_close"] == 101.0


def t_apply_remote_partial_stays_local():
    facts = {"AAL": _facts_row(), "ZZZ": _facts_row()}
    rem = {"AAL": {"ema50": 102.0, "atr": 2.5}}  # rsi missing
    out = apply_remote(facts, rem)
    assert out["AAL"]["ema50"] == 100.0  # untouched, never half-remote
    assert out["AAL"]["_src"] == "local"
    assert out["ZZZ"]["_src"] == "local"  # unknown ticker stays local


def t_load_overrides():
    import datetime as dt
    import tempfile
    today = dt.date(2026, 10, 2)
    doc = {"date": "2026-10-02", "tickers": {"AAL": {"rsi": 41.0}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as f:
        json.dump(doc, f)
        path = f.name
    assert load_overrides(path, today) == {"AAL": {"rsi": 41.0}}
    assert load_overrides(path, dt.date(2026, 10, 3)) == {}  # stale
    assert load_overrides(path + ".nope", today) == {}  # missing
    with open(path, "w") as f:
        f.write("{broken")
    assert load_overrides(path, today) == {}  # corrupt


TESTS = [t_chunk_sizes, t_chunk_default_cap, t_interval_map,
         t_period_range, t_call_plan_scalar_only, t_array_specs_batched,
         t_merge_fixture, t_merge_wrsi, t_local_parity, t_compare,
         t_aggregate_15m_values, t_aggregate_edges,
         t_apply_remote_full, t_apply_remote_partial_stays_local,
         t_load_overrides]


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
