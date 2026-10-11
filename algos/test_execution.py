"""Offline checks for the options execution layer (no network, no MCP).

Contract: ids and ref_ids are deterministic; expiry picks are third-Friday
monthlies in the DTE band; the contract pick uses the MCP delta and the
settled liquidity gate; the greek price map, planned loss, sizing tiers,
ladders and the resting-stop prices follow docs/strategies/EXECUTION.md;
EXIT intents fire on stop/target/time/dte; state writes are atomic.

Usage (project root, venv active):
    python algos/test_execution.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import execution as ex  # noqa: E402


def q(strike, delta, bid, ask, oi, iv=0.30, gamma=0.02, theta=-0.05,
      mark=None):
    return {"instrument_id": f"id-{strike}", "strike": strike, "expiry": "2026-12-18",
            "type": "call", "delta": delta, "gamma": gamma, "theta": theta,
            "vega": 0.3, "iv": iv, "bid": bid, "ask": ask,
            "mark": mark if mark is not None else (bid + ask) / 2,
            "mid": (bid + ask) / 2, "spread": ask - bid,
            "spread_pct": 100 * (ask - bid) / ((bid + ask) / 2),
            "oi": oi, "volume": 100, "updated_at": None}


def test_ids_deterministic():
    a = ex.intent_id(dt.date(2026, 10, 13), "pg")
    assert a == "FNLX-20261013-PG-SQ-L" == ex.intent_id("2026-10-13", "PG")
    assert ex.exit_intent_id(a, "stop") == a + "-X-stop"
    r1, r2 = ex.ref_id(a, 1), ex.ref_id(a, 2)
    assert r1 == ex.ref_id(a, 1) and r1 != r2 and len(r1) == 36


def test_expiry_rules():
    assert ex.third_friday(2026, 12) == dt.date(2026, 12, 18)
    assert ex.third_friday(2027, 1) == dt.date(2027, 1, 15)
    today = dt.date(2026, 10, 12)
    assert ex.expiry_hint(today, 20) == "2026-12-18"   # 67 DTE >= 49
    exps = ["2026-10-16", "2026-10-23", "2026-11-20", "2026-12-18",
            "2027-01-15", "2027-03-19"]
    assert ex.pick_expiry(exps, today) == "2026-12-18"
    # a weekly inside the band never qualifies; nothing in band -> None
    assert ex.pick_expiry(["2026-12-04", "2026-12-11"], today) is None
    assert ex.pick_expiry(["2027-06-18"], today) is None  # beyond min+60


def test_norm_quote_from_mcp_strings():
    raw = {"instrument_id": "abc", "delta": "0.655", "gamma": "0.012",
           "theta": "-0.21", "implied_volatility": "0.33", "bid_price": "19.75",
           "ask_price": "19.95", "mark_price": "19.85", "open_interest": 59217,
           "volume": 1200, "updated_at": "2026-10-09T19:21:56Z"}
    inst = {"id": "abc", "strike_price": "220.0000",
            "expiration_date": "2026-12-18", "type": "call"}
    n = ex.norm_quote(raw, inst)
    assert n["strike"] == 220.0 and n["delta"] == 0.655 and n["oi"] == 59217
    assert abs(n["mid"] - 19.85) < 1e-9 and abs(n["spread_pct"] - 1.0076) < 0.01
    assert n["expiry"] == "2026-12-18"


def test_price_map_and_planned_loss():
    qq = q(220, 0.65, 19.75, 19.95, 50000, gamma=0.012, theta=-0.21)
    # stock 230 -> stop 214.19: linear -10.3, convexity +1.5 (< half the
    # linear move, so uncapped), then theta over 10 days -2.1
    lin = 19.85 + 0.65 * (214.19 - 230.0)
    p0 = ex.price_at_stock(qq, 230.0, 214.19, days=0)
    assert lin < p0 < 19.85 and abs(p0 - (lin + 0.5 * 0.012 * 15.81 ** 2)) < 1e-6
    p10 = ex.price_at_stock(qq, 230.0, 214.19, days=10)
    assert abs(p10 - (p0 - 2.1)) < 1e-9
    pl = ex.planned_loss(qq, 230.0, 214.19)
    assert abs(pl - 100 * (19.85 - p10)) < 1e-6 and 1000 < pl < 1100
    # far from the money the convexity cap holds and the floor is intrinsic
    assert ex.price_at_stock(qq, 230.0, 100.0) == 0.01
    assert ex.price_at_stock(qq, 230.0, 300.0) >= 80.0   # intrinsic floor 300-220


def test_liquidity_gate_binding_is_absolute():
    good = q(220, 0.65, 19.75, 19.95, 50000)
    assert ex.liquidity_ok(good, 230.0, 15.9)[0]
    wide = q(220, 0.65, 18.0, 21.0, 50000)           # 15% of mid: the % gate first
    assert ex.liquidity_ok(wide, 230.0, 15.9) == (False, "spread_pct")
    # 9.5% of mid passes the % gate but $0.20 on 0.65 x $1.00 risk = 0.31R
    cheap = q(20, 0.65, 2.00, 2.20, 50000)
    assert ex.liquidity_ok(cheap, 21.0, 1.0) == (False, "spread_r")
    assert ex.liquidity_ok(cheap, 21.0, 3.0)[0]      # 0.10R on a $3 risk: fine
    thin = q(220, 0.65, 19.75, 19.95, 100)
    assert ex.liquidity_ok(thin, 230.0, 15.9)[1].startswith("oi<")
    near = q(230, 0.55, 14.0, 14.2, 300)              # within 2.5% of spot: needs 500
    assert ex.liquidity_ok(near, 230.0, 15.9)[1] == "oi<500"
    pct = q(5, 0.6, 0.20, 0.40, 1000)                 # 67% of mid
    assert ex.liquidity_ok(pct, 10.0, 0.5)[1] == "spread_pct"
    nod = q(220, float("nan"), 19.75, 19.95, 50000)
    assert ex.liquidity_ok(nod, 230.0, 15.9) == (False, "no_delta")


def test_pick_contract_by_delta_and_oi():
    quotes = [q(210, 0.78, 26.0, 26.4, 9000),     # delta too high
              q(215, 0.72, 23.0, 23.3, 12000),
              q(220, 0.65, 19.75, 19.95, 59217),  # most liquid in band
              q(225, 0.58, 16.5, 16.8, 30000),
              q(230, 0.50, 13.5, 13.8, 40000),    # below band
              q(222.5, float("nan"), 18.0, 18.2, 80000)]  # no delta: never
    pick, why = ex.pick_contract(quotes, 230.0, 15.9)
    assert why == "ok" and pick["strike"] == 220
    # rich regime floor 0.65 excludes the 0.58
    pick, _ = ex.pick_contract(quotes[3:], 230.0, 15.9, delta_floor=0.65)
    assert pick is None
    pick, _ = ex.pick_contract(quotes, 230.0, 15.9, delta_floor=0.65)
    assert pick["strike"] == 220
    none, why = ex.pick_contract([quotes[0], quotes[4]], 230.0, 15.9)
    assert none is None and why.startswith("no_admissible")


def test_iv_regime_uses_hv60_floor():
    assert ex.iv_regime(0.30, 0.25, 0.28)[0] == "single"       # 1.07
    kind, ratio = ex.iv_regime(0.40, 0.20, 0.30)              # 1.33 on HV60, 2.0 on HV20
    assert kind == "rich" and abs(ratio - 1.333) < 0.01
    assert ex.iv_regime(0.60, 0.20, 0.30)[0] == "none"
    assert ex.iv_regime(float("nan"), 0.2, 0.3)[0] == "none"


def test_sizing_tiers_and_caps():
    # $1,000: shadow tier, never a live contract
    assert ex.size(1000, 1000, 150, 400) == (0, "shadow_tier")
    # bootstrap $4,000: RB = min(5%, $250) = $200; PL 150 -> 1 contract
    assert ex.risk_budget(4000) == 200
    assert ex.size(4000, 4000, 150, 390) == (1, "ok")
    # tolerance: PL 240 <= 1.25 x 200 -> 1; PL 260 -> unfunded
    assert ex.size(4000, 4000, 240, 390)[0] == 1
    assert ex.size(4000, 4000, 260, 390) == (0, "unfunded")
    # premium cap 10% of 4000 = 400 < premium 450 -> prem_pos
    assert ex.size(4000, 4000, 150, 450) == (0, "prem_pos")
    # standard $25k: RB 2% = 500; PL 240 -> 2; premium 1500 x2 = 3000 > 5% (1250) -> cap to 0 -> prem_pos
    assert ex.size(25000, 25000, 240, 1500) == (0, "prem_pos")
    assert ex.size(25000, 25000, 240, 600) == (2, "ok")
    assert ex.size(25000, 25000, 240, 600, n_open=4) == (0, "max_open")
    assert ex.size(25000, 100, 240, 600) == (0, "buying_power")


def test_ladders_and_stop_order():
    buy = ex.ladder_prices(19.75, 19.95, 0.05, "buy")
    assert buy == [19.85, 19.9, 19.95]          # +0.33/+0.67 half round up onto 19.90, then ask
    sell = ex.ladder_prices(19.75, 19.95, 0.05, "sell")
    assert sell == [19.85, 19.8, 19.75]
    qq = q(220, 0.65, 19.75, 19.95, 50000, gamma=0.012, theta=-0.21)
    so = ex.stop_order(qq, 230.0, 214.19, 0.05)
    assert so["reason"] == "ok" and so["stop_price"] < 19.75
    assert so["type"] == "stop_market" and so["tif"] == "gfd"   # day-only, user decision
    mapped = ex.price_at_stock(qq, 230.0, 214.19, 0.0)
    assert abs(so["stop_price"] - ex.round_tick(0.90 * mapped, 0.05, False)) < 1e-9
    # stock already far through the stop: the mapped trigger sits above the
    # ask (the MCP rejects it) -> no resting order, the print path exits now
    so2 = ex.stop_order(qq, 200.0, 214.19, 0.05)
    assert so2["stop_price"] is None and so2["reason"] == "trigger>=ask"
    assert ex.DEFAULT_CONFIG["regime"]["bull_only"] is True             # user decision
    assert ex.restrike_needed(10.0, 10.4) is False and ex.restrike_needed(10.0, 10.6)


def test_day_trades_count_all_fills():
    today = dt.date(2026, 10, 15)
    fills = [{"instrument": "NVDA 220C", "side": "buy", "date": "2026-10-13"},
             {"instrument": "NVDA 220C", "side": "sell", "date": "2026-10-13"},   # day trade
             {"instrument": "PG", "side": "buy", "date": "2026-10-14"},
             {"instrument": "PG", "side": "sell", "date": "2026-10-14"},           # user equity day trade
             {"instrument": "MU", "side": "buy", "date": "2026-09-01"},
             {"instrument": "MU", "side": "sell", "date": "2026-09-01"},           # too old
             {"instrument": "AAPL 337.5C", "side": "buy", "date": "2026-10-15"}]  # open only
    assert ex.day_trades(fills, today) == 2


def test_entry_intents_from_scan_and_ordering():
    today = dt.date(2026, 10, 13)
    base = {"family": "SQ", "side": "long", "level": 151.23, "risk": 7.27, "atr": 3.12,
            "exit": "fixed", "exit_mult": 2.0, "time": 20, "grade": "B+", "paused": False,
            "bull": True, "rvol": 1.1, "sq_days": 11, "asof": "2026-10-12",
            "fill_date": "2026-10-13"}
    st = {"pending": [dict(base, ticker="PG"),
                      dict(base, ticker="PM", sq_days=6, rvol=1.4),
                      dict(base, ticker="HUM", family="BO", exit="trail", exit_mult=3.0, time=40),
                      dict(base, ticker="XX", grade="B"),
                      dict(base, ticker="YY", fill_date="2026-10-14"),
                      dict(base, ticker="ZZ", paused=True, grade="C")]}
    ints = ex.entry_intents_from_scan(st, today, hv_fn=lambda t: (0.17, 0.21))
    assert [i["ticker"] for i in ints] == ["PM", "PG"]   # early release + rvol first
    i = ints[1]
    assert i["intent_id"] == "FNLX-20261013-PG-SQ-L" and i["kind"] == "ENTRY"
    assert i["expiry_hint"] == "2026-12-18" and i["vol"] == {"hv20": 0.17, "hv60": 0.21}
    assert i["stock"]["time_bars"] == 20 and i["window_pt"] == ["06:33", "06:50"]
    cfg = ex.load_config()
    cfg["regime"]["bull_only"] = True
    st2 = {"pending": [dict(base, ticker="PG", bull=False)]}
    assert ex.entry_intents_from_scan(st2, today, cfg) == []


def test_exit_intents_and_time_alignment():
    ledger = {"positions": {
        "FNLX-20261013-PG-SQ-L": {"ticker": "PG", "status": "open", "fill_date": "2026-10-13",
                                  "stock_stop": 143.3, "stock_target": 165.1, "time_bars": 20,
                                  "expiry": "2026-12-18"},
        "FNLX-20261013-PM-SQ-L": {"ticker": "PM", "status": "closed", "fill_date": "2026-10-13",
                                  "stock_stop": 185.0, "stock_target": 226.0, "time_bars": 20,
                                  "expiry": "2026-12-18"}}}
    noon = dt.datetime(2026, 10, 14, 9, 0)
    assert ex.exit_intents(ledger, {"PG": (150.0, 150.0, 1e6)}, dt.date(2026, 10, 14), noon) == []
    stop = ex.exit_intents(ledger, {"PG": (143.0, 150.0, 1e6)}, dt.date(2026, 10, 14), noon)
    assert [i["reason"] for i in stop] == ["stop"] and stop[0]["intent_id"].endswith("-X-stop")
    tgt = ex.exit_intents(ledger, {"PG": (166.0, 150.0, 1e6)}, dt.date(2026, 10, 14), noon)
    assert [i["reason"] for i in tgt] == ["target"]
    # time stop: 19 sessions after the 10/13 fill = 2026-11-09, after 12:30 only
    from market_calendar import add_trading_days
    d19 = add_trading_days(dt.date(2026, 10, 13), 19)
    early = dt.datetime(d19.year, d19.month, d19.day, 9, 0)
    late = dt.datetime(d19.year, d19.month, d19.day, 12, 45)
    assert ex.exit_intents(ledger, {"PG": (150.0, 150.0, 1e6)}, d19, early) == []
    assert [i["reason"] for i in ex.exit_intents(ledger, {"PG": (150.0, 150.0, 1e6)}, d19, late)] == ["time"]
    # dte: 20 days before expiry
    assert "dte" in [i["reason"] for i in ex.exit_intents(
        ledger, {"PG": (150.0, 150.0, 1e6)}, dt.date(2026, 11, 30), noon)]


def test_state_files_and_alarms():
    tmp = tempfile.mkdtemp()
    real = (ex.INTENTS, ex.TRADES)
    ex.INTENTS, ex.TRADES = os.path.join(tmp, "intents.json"), os.path.join(tmp, "trades.csv")
    try:
        today = dt.date(2026, 10, 13)
        e = {"intent_id": "FNLX-20261013-PG-SQ-L", "kind": "ENTRY"}
        x = {"intent_id": "FNLX-20261013-PG-SQ-L-X-stop", "kind": "EXIT"}
        cur = ex.write_intents([e], [], today)
        assert len(cur["intents"]) == 1
        cur = ex.write_intents([e], [x], today)   # merge by id, never duplicate
        assert sorted(i["intent_id"] for i in cur["intents"]) == sorted([e["intent_id"], x["intent_id"]])
        cur = ex.write_intents([e], [], dt.date(2026, 10, 14))  # new session date resets
        assert len(cur["intents"]) == 1
        assert ex.load_json(ex.INTENTS, {})["session_date"] == "2026-10-14"
        pos = {"position_id": "FNLX-20261013-PG-SQ-L", "ticker": "PG", "fill_date": "2026-10-13",
               "expiry": "2026-12-18", "strike": 150.0, "qty": 1, "fill_price": 6.0,
               "planned_loss": 368.0, "stock_stop": 143.3, "stock_target": 165.1}
        row = ex.trade_row(pos, "target", 13.5, dt.date(2026, 10, 20), stock_R=2.0)
        assert row["pnl_usd"] == 750.0 and abs(row["option_R"] - 750 / 368) < 1e-3
        assert row["fidelity_err"] == round(750 / 368 - 2.0, 3) and row["day_trade"] is False
        ex.append_trade(row); ex.append_trade(dict(row, mode="shadow", option_R=-1.0, pnl_usd=-368.0))
        s = ex.trades_summary()
        assert s["live"]["n"] == 1 and s["shadow"]["n"] == 1
    finally:
        ex.INTENTS, ex.TRADES = real
    now = dt.datetime(2026, 10, 13, 9, 0)
    hb = {"last_run": "2026-10-13T08:30:00"}
    assert ex.heartbeat_stale(hb, now) and not ex.heartbeat_stale({"last_run": "2026-10-13T08:50:00"}, now)
    ledger = {"positions": {"p": {"status": "open"}}}
    assert ex.alarms(ledger, hb, 0, now)[0].startswith("EXEC session down: 1 open")
    assert ex.alarms({"positions": {}}, hb, 0, now) == []


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
