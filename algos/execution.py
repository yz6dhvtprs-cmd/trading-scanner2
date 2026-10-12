"""Options execution layer for the swing funnel — the loop side.

Spec: docs/strategies/EXECUTION.md. Two halves live here:
  1. the Python loop's duties (no broker access): config, deterministic
     intent ids, ENTRY intents from the premarket setups, EXIT intents
     and alarms from the 15-min print, atomic state files under
     data/exec/;
  2. the pure math the executor session applies to Robinhood MCP data:
     expiry pick, contract pick by MCP delta and liquidity, the greek
     price map (P at the stock stop), planned loss, sizing tiers,
     ladders, the resting stop's prices, PDT count. Every function takes
     plain dicts/numbers so the session can call it on get_option_quotes
     output and the tests can call it offline. Nothing here calls the
     MCP; the session (.claude/skills/fnl-exec) does.

Usage:
    python algos/execution.py --status          # config, intents, ledger
    python algos/execution.py --init-config     # write defaults if absent
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import uuid

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXEC_DIR = os.path.join(ROOT_DIR, "data", "exec")
CONFIG = os.path.join(EXEC_DIR, "config.json")
INTENTS = os.path.join(EXEC_DIR, "intents.json")
LEDGER = os.path.join(EXEC_DIR, "ledger.json")
TRADES = os.path.join(EXEC_DIR, "trades.csv")
HEARTBEAT = os.path.join(EXEC_DIR, "heartbeat.json")
HALT = os.path.join(EXEC_DIR, "halt")
NAMESPACE_EXEC = uuid.UUID("6f1b7d0e-3c2a-4b9e-9d4f-5a7c8e2f1a3b")

DEFAULT_CONFIG = {
    "armed": False, "account_number": None, "notify_to": None,
    "alert_allowlist": [],
    "families": ["SQ"], "sides": ["long"], "min_grade": "B+",
    "entry_window_pt": ["06:33", "06:50"], "entry_chase_atr": 1.0,
    "entry_max_above_open_atr": 0.5,
    "expiry": {"min_dte": 49, "max_dte_over_min": 60, "monthly_only": True,
               "close_dte": 21},
    "delta": {"lo": 0.55, "hi": 0.75, "target": 0.65, "rich_floor": 0.65},
    "iv": {"single_max": 1.2, "rich_max": 1.6, "vix_halve": 30,
           "vix_stop": 40},
    "liquidity": {"oi_min": 250, "oi_min_near_spot": 500,
                  "spread_pct_max": 10.0, "spread_r_max": 0.20,
                  "bid_min": 0.10, "quote_max_age_s": 300},
    "sizing": {"tiers": [
        {"min": 0, "rb_pct": 0.0, "prem_pos": 0.0, "prem_sum": 0.0,
         "pl_sum": 0.0, "max_open": 0},
        {"min": 2500, "rb_pct": 0.05, "rb_cap": 250, "prem_pos": 0.10,
         "prem_sum": 0.10, "pl_sum": 0.07, "max_open": 1},
        {"min": 5000, "rb_pct": 0.03, "prem_pos": 0.10, "prem_sum": 0.25,
         "pl_sum": 0.10, "max_open": 2},
        {"min": 10000, "rb_pct": 0.03, "prem_pos": 0.10, "prem_sum": 0.25,
         "pl_sum": 0.10, "max_open": 3},
        {"min": 25000, "rb_pct": 0.02, "prem_pos": 0.05, "prem_sum": 0.20,
         "pl_sum": 0.10, "max_open": 4},
        {"min": 50000, "rb_pct": 0.015, "prem_pos": 0.04, "prem_sum": 0.20,
         "pl_sum": 0.08, "max_open": 6}],
        "qty_tolerance": 1.25, "pl_days": 10, "corr_max": 0.75},
    # user decision 2026-10-11: day-only stop_market, re-placed every
    # session morning and after each fill; buffer keeps a quote blip from
    # converting a touch into a market sell [guess]
    "stops": {"type": "stop_market", "tif": "gfd", "buffer": 0.90,
              "restrike_pct": 5.0, "near_stop_r": 0.1},
    "ladder": {"buy": [0.0, 0.33, 0.67, 1.0], "buy_wait_s": [45, 45, 45, 30],
               "ask_cap": 1.02, "budget_cap": 1.10,
               "sell": [0.0, -0.33, -0.67, -1.0], "sell_wait_s": 30,
               "market_after_sell_ladder": ["stop", "time", "dte"]},
    "loss_caps": {"day_rb": 1.5, "consec_stops_halve": 3, "halt_dd": 0.15},
    "pdt": {"entry_max_used": 2, "exempt_equity": 25000},
    "earnings": {"entry_min_sessions": 0, "expiry_after_report_days": 7},
    "regime": {"bull_only": True},  # user decision 2026-10-11
    "stale": {"stock_quote_s": 60, "heartbeat_s": 2400},  # session runs every 30 min
}
GRADE_RANK = {"A+": 0, "A": 1, "B+": 2, "B": 3, "C": 4}


# ---------- state files ----------
def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_json(path: str, obj) -> None:
    """One writer per file; tmp + os.replace so a reader never sees a
    half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def load_config() -> dict:
    """Defaults overlaid with data/exec/config.json (one level deep)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = load_json(CONFIG, {})
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


# ---------- ids ----------
def intent_id(fill_date, ticker: str, family: str = "SQ",
              side: str = "long") -> str:
    """Deterministic: regenerating the intents file reproduces the id."""
    d = fill_date.isoformat().replace("-", "") if hasattr(
        fill_date, "isoformat") else str(fill_date).replace("-", "")
    return f"FNLX-{d}-{ticker.upper()}-{family}-{'L' if side == 'long' else 'S'}"


def exit_intent_id(position_id: str, reason: str) -> str:
    return f"{position_id}-X-{reason}"


def ref_id(intent: str, attempt: int) -> str:
    """Idempotency key for place_option_order: same intent + attempt ->
    same uuid, so a transport retry re-sends the identical key and a new
    ladder step gets a new one."""
    return str(uuid.uuid5(NAMESPACE_EXEC, f"{intent}:{int(attempt)}"))


# ---------- expiry ----------
def third_friday(year: int, month: int) -> dt.date:
    first = dt.date(year, month, 1)
    offset = (4 - first.weekday()) % 7  # Friday = 4
    return first + dt.timedelta(days=offset + 14)


def expiry_hint(today, horizon_bars: int, cfg: dict = None) -> str:
    """First third-Friday monthly with DTE >= min_dte (loop-side hint;
    the session confirms it is listed)."""
    cfg = cfg or DEFAULT_CONFIG
    min_dte = int(cfg["expiry"].get("min_dte") or math.ceil(
        1.4 * horizon_bars) + 21)
    y, m = today.year, today.month
    for _ in range(18):
        tf = third_friday(y, m)
        if (tf - today).days >= min_dte:
            return tf.isoformat()
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return None


def pick_expiry(expiries: list, today, cfg: dict = None) -> str:
    """From a chain's expiration_dates: the nearest third-Friday monthly
    inside [min_dte, min_dte + max_dte_over_min]; None when nothing
    qualifies (then no trade)."""
    cfg = cfg or DEFAULT_CONFIG
    lo = int(cfg["expiry"]["min_dte"])
    hi = lo + int(cfg["expiry"]["max_dte_over_min"])
    best = None
    for e in expiries:
        d = dt.date.fromisoformat(str(e)[:10])
        dte = (d - today).days
        if cfg["expiry"].get("monthly_only", True) and d != third_friday(
                d.year, d.month):
            continue
        if lo <= dte <= hi and (best is None or dte < best[0]):
            best = (dte, d.isoformat())
    return best[1] if best else None


# ---------- realized vol ----------
def hv(close: pd.Series, n: int) -> float:
    """Annualized stdev of daily log close-to-close returns over n bars."""
    c = pd.Series(close).astype(float).dropna()
    if len(c) < n + 1:
        return float("nan")
    r = np.log(c / c.shift(1)).dropna().tail(n)
    return float(r.std(ddof=1) * math.sqrt(252))


def iv_regime(iv: float, hv20: float, hv60: float, cfg: dict = None) -> tuple:
    """('single' | 'rich' | 'none', ratio) from the chosen strike's MCP
    implied_volatility against max(HV20, HV60) — HV20 alone is depressed
    inside a squeeze by construction."""
    cfg = cfg or DEFAULT_CONFIG
    denom = max(x for x in (hv20, hv60) if x == x) if any(
        x == x for x in (hv20, hv60)) else float("nan")
    if not (iv == iv) or not (denom == denom) or denom <= 0:
        return "none", float("nan")
    ratio = float(iv) / denom
    if ratio <= cfg["iv"]["single_max"]:
        return "single", ratio
    if ratio <= cfg["iv"]["rich_max"]:
        return "rich", ratio
    return "none", ratio


# ---------- quotes (MCP get_option_quotes -> plain floats) ----------
def norm_quote(q: dict, instrument: dict = None) -> dict:
    """MCP quote (strings) + its instrument -> floats the math uses."""
    def f(k, src=q):
        v = src.get(k) if src else None
        try:
            return float(v) if v is not None else float("nan")
        except (TypeError, ValueError):
            return float("nan")
    inst = instrument or {}
    out = {"instrument_id": q.get("instrument_id") or inst.get("id"),
           "strike": f("strike_price", inst), "expiry": inst.get(
               "expiration_date"), "type": inst.get("type"),
           "delta": f("delta"), "gamma": f("gamma"), "theta": f("theta"),
           "vega": f("vega"), "iv": f("implied_volatility"),
           "bid": f("bid_price"), "ask": f("ask_price"),
           "mark": f("mark_price"), "oi": f("open_interest"),
           "volume": f("volume"), "updated_at": q.get("updated_at")}
    out["mid"] = (out["bid"] + out["ask"]) / 2 if (
        out["bid"] == out["bid"] and out["ask"] == out["ask"]) else out["mark"]
    out["spread"] = out["ask"] - out["bid"]
    out["spread_pct"] = (100 * out["spread"] / out["mid"]
                         if out["mid"] and out["mid"] > 0 else float("nan"))
    return out


def price_at_stock(q: dict, s_now: float, s_then: float,
                   days: float = 0.0) -> float:
    """Option price mapped to another stock price with the quote's own
    greeks (second-order Taylor + theta): a price map, not a greek
    derivation. Floored at 0.01."""
    d = float(s_then) - float(s_now)
    g = q["gamma"] if q.get("gamma") == q.get("gamma") else 0.0
    th = q["theta"] if q.get("theta") == q.get("theta") else 0.0
    lin = q["delta"] * d
    # gamma is not constant: far from the money the quadratic term would
    # overtake the linear one and price the option UP on a crash. Cap the
    # convexity at half the linear move and floor at intrinsic value.
    conv = min(0.5 * g * d * d, 0.5 * abs(lin))
    p = q["mid"] + lin + conv + th * float(days)
    intrinsic = max(0.0, (float(s_then) - q["strike"]) if q.get("type", "call")
                    == "call" else (q["strike"] - float(s_then))) \
        if q.get("strike") == q.get("strike") else 0.0
    return max(0.01, intrinsic, float(p))


def planned_loss(q: dict, s_now: float, s_stop: float,
                 cfg: dict = None) -> float:
    """Dollars per contract from the fill to the stock stop after the
    median losing hold (pl_days)."""
    cfg = cfg or DEFAULT_CONFIG
    p_stop = price_at_stock(q, s_now, s_stop, cfg["sizing"]["pl_days"])
    return max(0.0, 100.0 * (q["mid"] - p_stop))


def liquidity_ok(q: dict, s_now: float, stock_risk: float,
                 cfg: dict = None, now=None) -> tuple:
    """(ok, reason). The binding gate is absolute: the full-cross spread
    may cost at most 0.20 x delta x stock risk (0.2R worst case)."""
    cfg = cfg or DEFAULT_CONFIG
    L = cfg["liquidity"]
    if not (q["bid"] == q["bid"]) or q["bid"] < L["bid_min"]:
        return False, "bid"
    if not (q["bid"] <= q["mark"] <= q["ask"]) if q["mark"] == q["mark"] \
            else False:
        return False, "mark_outside"
    near = abs(q["strike"] - s_now) <= 0.025 * s_now
    oi_min = L["oi_min_near_spot"] if near else L["oi_min"]
    if not (q["oi"] >= oi_min):
        return False, f"oi<{oi_min}"
    if q["spread_pct"] > L["spread_pct_max"]:
        return False, "spread_pct"
    if not (q["delta"] == q["delta"]):
        return False, "no_delta"
    if q["spread"] > L["spread_r_max"] * q["delta"] * float(stock_risk):
        return False, "spread_r"
    if now is not None and q.get("updated_at"):
        try:
            ts = dt.datetime.fromisoformat(str(q["updated_at"]).replace(
                "Z", "+00:00"))
            if (now - ts).total_seconds() > L["quote_max_age_s"]:
                return False, "stale_quote"
        except ValueError:
            pass
    return True, "ok"


def pick_contract(quotes: list, s_now: float, stock_risk: float,
                  cfg: dict = None, delta_floor: float = None) -> tuple:
    """(quote | None, reason). Admissible = MCP delta in the band (no
    delta -> never) and passes liquidity; pick the highest open interest,
    ties within 20% -> tighter spread %, then delta nearest target."""
    cfg = cfg or DEFAULT_CONFIG
    lo = max(cfg["delta"]["lo"], delta_floor or 0.0)
    hi = cfg["delta"]["hi"]
    tgt = cfg["delta"]["target"]
    cands, why = [], {}
    for q in quotes:
        if not (q["delta"] == q["delta"]):
            why[q.get("strike")] = "no_delta"
            continue
        if not (lo <= q["delta"] <= hi):
            why[q.get("strike")] = "delta"
            continue
        ok, r = liquidity_ok(q, s_now, stock_risk, cfg)
        if not ok:
            why[q.get("strike")] = r
            continue
        cands.append(q)
    if not cands:
        return None, "no_admissible:" + ",".join(
            f"{k}:{v}" for k, v in sorted(why.items(), key=lambda kv: kv[0]
                                         if kv[0] == kv[0] else 0))
    top_oi = max(q["oi"] for q in cands)
    tier = [q for q in cands if q["oi"] >= 0.8 * top_oi]
    tier.sort(key=lambda q: (round(q["spread_pct"], 2),
                             abs(q["delta"] - tgt)))
    return tier[0], "ok"


# ---------- sizing ----------
def tier_for(equity: float, cfg: dict = None) -> dict:
    cfg = cfg or DEFAULT_CONFIG
    chosen = cfg["sizing"]["tiers"][0]
    for t in cfg["sizing"]["tiers"]:
        if equity >= t["min"]:
            chosen = t
    return chosen


def risk_budget(equity: float, cfg: dict = None) -> float:
    t = tier_for(equity, cfg)
    rb = t["rb_pct"] * equity
    if t.get("rb_cap"):
        rb = min(rb, t["rb_cap"])
    return rb


def size(equity: float, options_bp: float, pl: float, premium: float,
         open_prem_sum: float = 0.0, open_pl_sum: float = 0.0,
         n_open: int = 0, cfg: dict = None) -> tuple:
    """(qty, reason). pl and premium are per contract in dollars."""
    cfg = cfg or DEFAULT_CONFIG
    t = tier_for(equity, cfg)
    if t["max_open"] == 0:
        return 0, "shadow_tier"
    if n_open >= t["max_open"]:
        return 0, "max_open"
    rb = risk_budget(equity, cfg)
    if pl <= 0:
        return 0, "pl<=0"
    qty = int(rb // pl)
    if qty == 0 and pl <= cfg["sizing"]["qty_tolerance"] * rb:
        qty = 1
    if qty == 0:
        return 0, "unfunded"
    cap_pos = int((t["prem_pos"] * equity) // premium) if premium > 0 else 0
    cap_sum = int(max(0.0, t["prem_sum"] * equity - open_prem_sum) // premium) \
        if premium > 0 else 0
    cap_pl = int(max(0.0, t["pl_sum"] * equity - open_pl_sum) // pl)
    cap_bp = int(options_bp // premium) if premium > 0 else 0
    q = min(qty, cap_pos, cap_sum, cap_pl, cap_bp)
    if q <= 0:
        lim = min((cap_pos, "prem_pos"), (cap_sum, "prem_sum"),
                  (cap_pl, "pl_sum"), (cap_bp, "buying_power"))
        return 0, lim[1]
    return q, "ok"


# ---------- orders ----------
def round_tick(price: float, tick: float, up: bool) -> float:
    if tick <= 0:
        return round(price, 2)
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 4)


def ladder_prices(bid: float, ask: float, tick: float, side: str,
                  cfg: dict = None) -> list:
    """Buy: mid -> +0.33 half -> +0.67 half -> ask (rounded up). Sell:
    mid -> -0.33 half -> -0.67 half -> bid (rounded down)."""
    cfg = cfg or DEFAULT_CONFIG
    mid, half = (bid + ask) / 2, (ask - bid) / 2
    steps = cfg["ladder"]["buy" if side == "buy" else "sell"]
    out = []
    for s in steps:
        p = round_tick(mid + s * half, tick, up=(side == "buy"))
        p = min(p, ask) if side == "buy" else max(p, bid)
        if not out or p != out[-1]:
            out.append(round(p, 4))
    return out


def stop_order(q: dict, s_now: float, s_stop: float, tick: float,
               cfg: dict = None) -> dict:
    """Resting day-only stop_market sell-to-close (user decision): trigger
    = buffer x the option price mapped to the stock stop with today's
    greeks. Re-placed every session morning (GFD expires) and after each
    fill. stop_price None when the trigger sits at/above the ask (the MCP
    rejects it): the caller exits now if the stock is within near_stop_r
    of its stop, else leaves the position to the print-based path today."""
    cfg = cfg or DEFAULT_CONFIG
    trig = round_tick(cfg["stops"]["buffer"] * price_at_stock(
        q, s_now, s_stop, 0.0), tick, up=False)
    if not (trig < q["ask"]):
        return {"stop_price": None, "type": cfg["stops"]["type"],
                "tif": cfg["stops"]["tif"], "reason": "trigger>=ask"}
    return {"stop_price": max(trig, tick), "type": cfg["stops"]["type"],
            "tif": cfg["stops"]["tif"], "reason": "ok"}


def restrike_needed(old_stop: float, new_stop: float, cfg: dict = None) -> bool:
    cfg = cfg or DEFAULT_CONFIG
    if not old_stop:
        return True
    return abs(new_stop - old_stop) / old_stop * 100 > cfg["stops"]["restrike_pct"]


# ---------- PDT ----------
def day_trades(fills: list, today, sessions: int = 5) -> int:
    """Round trips opened and closed on the same session over the last
    `sessions` trading days, from ALL fills in the account (options and
    equities, every placed_agent). fills: dicts with instrument, side
    (buy/sell), date (ISO)."""
    from market_calendar import trading_gap  # noqa: E402
    by_day = {}
    for f in fills:
        try:
            d = dt.date.fromisoformat(str(f["date"])[:10])
        except ValueError:
            continue
        if trading_gap(d, today) >= sessions:
            continue
        key = (d, f["instrument"])
        by_day.setdefault(key, {"buy": 0, "sell": 0})
        by_day[key][f["side"]] = by_day[key].get(f["side"], 0) + 1
    return sum(1 for v in by_day.values() if v["buy"] and v["sell"])


# ---------- intents ----------
def grade_ok(grade: str, cfg: dict) -> bool:
    return GRADE_RANK.get(grade, 9) <= GRADE_RANK.get(cfg["min_grade"], 2)


def entry_intents_from_scan(st: dict, today, cfg: dict = None,
                            hv_fn=None, earnings_cache: dict = None) -> list:
    """ENTRY intents for today's texted setups of the configured
    families/sides. st = funnel.scan() result. hv_fn(ticker) -> (hv20,
    hv60); earnings_cache from algos/earnings.py (optional)."""
    cfg = cfg or DEFAULT_CONFIG
    out = []
    for p in st.get("pending", []):
        if p.get("paused") or p["family"] not in cfg["families"] \
                or p["side"] not in cfg["sides"]:
            continue
        if not grade_ok(p.get("grade", "C"), cfg):
            continue
        if p.get("fill_date") != today.isoformat():
            continue
        if cfg["regime"].get("bull_only") and not p.get("bull"):
            continue
        hv20 = hv60 = float("nan")
        if hv_fn:
            try:
                hv20, hv60 = hv_fn(p["ticker"])
            except Exception:
                pass
        earn = {"next": None, "sessions_to": None, "inside_hold": None}
        if earnings_cache:
            try:
                from earnings import next_earnings, sessions_to_earnings  # noqa: E402
                nxt = next_earnings(p["ticker"], today, earnings_cache)
                s2e = sessions_to_earnings(p["ticker"], today, earnings_cache)
                earn = {"next": nxt.isoformat() if nxt else None,
                        "sessions_to": s2e,
                        "inside_hold": (s2e is not None
                                        and s2e <= int(p["time"]))}
            except Exception:
                pass
        out.append({
            "intent_id": intent_id(today, p["ticker"], p["family"], p["side"]),
            "kind": "ENTRY", "created": dt.datetime.now().isoformat(
                timespec="seconds"),
            "window_pt": list(cfg["entry_window_pt"]),
            "ticker": p["ticker"], "family": p["family"], "side": p["side"],
            "grade": p["grade"], "signal_date": p.get("asof"),
            "fill_date": p["fill_date"],
            "stock": {"level": p["level"], "risk": p["risk"], "atr": p["atr"],
                      "time_bars": p["time"], "exit": p["exit"],
                      "exit_mult": p["exit_mult"], "sq_days": p.get("sq_days"),
                      "rvol": p.get("rvol"), "bull": p.get("bull")},
            "vol": {"hv20": None if hv20 != hv20 else round(hv20, 4),
                    "hv60": None if hv60 != hv60 else round(hv60, 4)},
            "earnings": earn,
            "expiry_hint": expiry_hint(today, int(p["time"]), cfg),
            "paper_row": {"strategy": f"FNL-{p['family']}",
                          "date": p["fill_date"]},
        })
    out.sort(key=lambda i: (0 if (i["stock"].get("sq_days") or 99) <= 8 else 1,
                            0 if (i["stock"].get("rvol") or 0) >= 1.2 else 1,
                            i["ticker"]))
    return out


def exit_intents(ledger: dict, prints: dict, today, now) -> list:
    """EXIT intents off the loop's print for every open ledger position:
    stop / target touch, the time stop (session where trading_gap ==
    time_bars - 1, after 12:30 PT), DTE below the close threshold.
    prints[t] = (last, open, volume)."""
    from market_calendar import trading_gap  # noqa: E402
    cfg = load_config()
    out = []
    late = now.hour * 60 + now.minute >= 12 * 60 + 30
    for pid, pos in (ledger.get("positions") or {}).items():
        if pos.get("status") != "open":
            continue
        q = prints.get(pos["ticker"])
        if q is None:
            continue
        last = float(q[0])
        reasons = []
        if last <= float(pos["stock_stop"]):
            reasons.append("stop")
        elif last >= float(pos["stock_target"]):
            reasons.append("target")
        try:
            gap = trading_gap(dt.date.fromisoformat(pos["fill_date"]), today)
            tb = int(pos.get("time_bars", 20))
            if gap >= tb or (gap == tb - 1 and late):
                reasons.append("time")
        except ValueError:
            pass
        try:
            dte = (dt.date.fromisoformat(pos["expiry"]) - today).days
            if dte < cfg["expiry"]["close_dte"]:
                reasons.append("dte")
        except (ValueError, TypeError):
            pass
        for r in reasons:
            out.append({"intent_id": exit_intent_id(pid, r), "kind": "EXIT",
                        "position_id": pid, "reason": r, "stock_print": last,
                        "print_time": now.isoformat(timespec="seconds")})
    return out


def write_intents(entries: list = None, exits: list = None, today=None) -> dict:
    """Merge by id into data/exec/intents.json (loop + premarket are the
    only writers; the session never writes it)."""
    cur = load_json(INTENTS, {})
    if cur.get("session_date") != (today.isoformat() if today else None):
        cur = {"session_date": today.isoformat() if today else None,
               "intents": []}
    have = {i["intent_id"]: i for i in cur.get("intents", [])}
    for i in (entries or []) + (exits or []):
        have.setdefault(i["intent_id"], i)
    cur["intents"] = list(have.values())
    cur["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    atomic_write_json(INTENTS, cur)
    return cur


def heartbeat_stale(hb: dict, now, cfg: dict = None) -> bool:
    cfg = cfg or DEFAULT_CONFIG
    try:
        last = dt.datetime.fromisoformat(hb["last_run"])
        if last.tzinfo is not None and now.tzinfo is None:
            last = last.replace(tzinfo=None)
        if last.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        return (now - last).total_seconds() > cfg["stale"]["heartbeat_s"]
    except Exception:
        return True


def alarms(ledger: dict, hb: dict, pending: int, now, cfg: dict = None) -> list:
    """Owner-only texts the loop sends when the session is not doing its
    job: stale heartbeat with open positions or pending intents."""
    cfg = cfg or DEFAULT_CONFIG
    n_open = sum(1 for p in (ledger.get("positions") or {}).values()
                 if p.get("status") == "open")
    out = []
    if (n_open or pending) and heartbeat_stale(hb, now, cfg):
        out.append(f"EXEC session down: {n_open} open, {pending} intents "
                   f"pending, last heartbeat {hb.get('last_run', 'never')}")
    return out


# ---------- fidelity ----------
def trade_row(pos: dict, exit_reason: str, exit_premium: float, exit_date,
              stock_R, slip_entry=None, slip_exit=None, mode="live") -> dict:
    pl = float(pos["planned_loss"]) * int(pos["qty"])
    pnl = (float(exit_premium) - float(pos["fill_price"])) * 100 * int(pos["qty"])
    opt_R = pnl / pl if pl > 0 else float("nan")
    return {"intent_id": pos.get("position_id"), "mode": mode,
            "ticker": pos["ticker"], "signal_date": pos.get("signal_date"),
            "fill_date": pos["fill_date"], "expiry": pos["expiry"],
            "strike": pos["strike"], "qty": pos["qty"],
            "fill_premium": pos["fill_price"], "delta_fill": pos.get("delta_fill"),
            "iv_fill": pos.get("iv_fill"), "iv_ratio": pos.get("iv_ratio"),
            "planned_loss": pos["planned_loss"], "stock_open": pos.get("stock_open"),
            "stock_stop": pos["stock_stop"], "stock_target": pos["stock_target"],
            "exit_date": exit_date.isoformat() if hasattr(exit_date, "isoformat") else exit_date,
            "exit_reason": exit_reason, "exit_premium": exit_premium,
            "pnl_usd": round(pnl, 2), "option_R": round(opt_R, 3),
            "stock_R": stock_R,
            "fidelity_err": (round(opt_R - float(stock_R), 3)
                             if stock_R is not None and stock_R == stock_R
                             else None),
            "slip_entry": slip_entry, "slip_exit": slip_exit,
            "day_trade": pos["fill_date"] == (exit_date.isoformat() if hasattr(
                exit_date, "isoformat") else exit_date)}


TRADE_COLS = ["intent_id", "mode", "ticker", "signal_date", "fill_date", "expiry",
              "strike", "qty", "fill_premium", "delta_fill", "iv_fill", "iv_ratio",
              "planned_loss", "stock_open", "stock_stop", "stock_target",
              "exit_date", "exit_reason", "exit_premium", "pnl_usd", "option_R",
              "stock_R", "fidelity_err", "slip_entry", "slip_exit", "day_trade"]


def append_trade(row: dict, path: str = None) -> None:
    path = path or TRADES
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k) for k in TRADE_COLS})


def trades_summary(path: str = None) -> dict:
    """For the Saturday review: n, win %, mean option_R, mean fidelity_err,
    mean slip per side, by mode."""
    path = path or TRADES
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path)
    out = {}
    for mode, g in df.groupby("mode"):
        last = g.tail(20)
        out[mode] = {"n": int(len(g)), "win_pct": round(100 * (g.option_R > 0).mean(), 1),
                     "option_R": round(float(g.option_R.mean()), 3),
                     "stock_R": round(float(pd.to_numeric(g.stock_R, errors="coerce").mean()), 3),
                     "fidelity_err": round(float(pd.to_numeric(g.fidelity_err, errors="coerce").mean()), 3),
                     "slip_entry": round(float(pd.to_numeric(g.slip_entry, errors="coerce").mean()), 3),
                     "slip_exit": round(float(pd.to_numeric(g.slip_exit, errors="coerce").mean()), 3),
                     "last10_option_R": round(float(last.tail(10).option_R.mean()), 3),
                     "last10_stock_R": round(float(pd.to_numeric(last.tail(10).stock_R, errors="coerce").mean()), 3)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--init-config", action="store_true")
    a = ap.parse_args()
    if a.init_config:
        if os.path.exists(CONFIG):
            print(f"{CONFIG} exists; not overwritten")
        else:
            atomic_write_json(CONFIG, DEFAULT_CONFIG)
            print(f"wrote {CONFIG} (armed=false, account_number=null)")
        return 0
    cfg = load_config()
    intents = load_json(INTENTS, {})
    ledger = load_json(LEDGER, {})
    hb = load_json(HEARTBEAT, {})
    print(f"armed={cfg['armed']} account={cfg['account_number']} "
          f"families={cfg['families']} min_grade={cfg['min_grade']} "
          f"halt={'YES' if os.path.exists(HALT) else 'no'}")
    print(f"intents ({intents.get('session_date')}): "
          f"{len(intents.get('intents', []))}")
    for i in intents.get("intents", []):
        print(f"  {i['kind']:5s} {i['intent_id']} "
              f"{i.get('reason') or i.get('grade', '')}")
    pos = ledger.get("positions") or {}
    print(f"ledger positions: {len(pos)} "
          f"({sum(1 for p in pos.values() if p.get('status') == 'open')} open); "
          f"heartbeat {hb.get('last_run', 'never')} mode={hb.get('mode', '-')}")
    print("trades:", json.dumps(trades_summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
