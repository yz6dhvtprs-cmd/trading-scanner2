"""Robinhood-MCP hybrid seam: batching + single-name server technicals.

Bulk pool bars stay on yfinance + ``daily_cache`` (cheap, cached, 500+
names). Once an algo narrows the pool to single names (intraday
cand/live_pool, weekly_box qualifiers, screener adds), this module
describes the exact Robinhood MCP calls that fetch server-computed
technicals (EMA/RSI/MACD/ATR/SMA) for those names - no local recompute.

Why single-symbol call specs: the agent MCP bridge currently rejects
every array-typed param (``... has type "string", want array`` - observed
2026-10-02 on get_equity_quotes, get_equity_historicals,
get_equity_fundamentals, get_index_quotes). So call_plan() emits one
scalar-only get_equity_technical_indicators spec per (ticker,
indicator): these pass today. chunk() packs names into <=
RH_MAX_SYMBOLS batches for the day the bridge accepts arrays again
(historicals/quotes/fundamentals cap at 10 symbols per call).

Interval map: RH has no 15-minute bars - 15m means aggregate 3x 5minute
client-side (needs_aggregation). yfinance period shorthands become
explicit (start_time, end_time) via period_to_range.

local_technicals() computes the same values off cached OHLC frames with
the repo's own formulas (simple_scan) - offline fallback and the parity
oracle for compare(). ops/rh_check.py drives plan/compare.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RH_MAX_SYMBOLS = 10  # per-call cap on historicals/quotes/fundamentals

# yfinance interval -> (RH interval, bars to aggregate). None means no
# direct equivalent: aggregate N fine bars client-side.
YF_TO_RH = {
    "1m": ("minute", 1),
    "2m": ("minute", 2),
    "5m": ("5minute", 1),
    "15m": ("5minute", 3),
    "30m": ("30minute", 1),
    "1h": ("hour", 1),
    "4h": ("4hour", 1),
    "1d": ("day", 1),
    "1wk": ("week", 1),
    "1mo": ("month", 1),
}

PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90,
    "6mo": 180, "1y": 365, "2y": 730,
}

# Per-algo server-technical needs. Periods mirror the repo's gates:
# intraday EMA8/13/21/50 + RSI14 + ATR14 (simple_scan), weekly EMA10/20
# + MACD 12/26/9 (weekly_box), reversals RSI14/ATR14, consolidation ATR14.
PRESETS = {
    "intraday": {
        "interval": "day",
        "specs": [{"type": "ema", "period": n} for n in (8, 13, 21, 50)]
        + [{"type": "rsi", "period": 14}, {"type": "atr", "period": 14},
           # weekly-regime RSI rides along on the week interval so the
           # remote set also covers live_touch_decisions' wrsi gate.
           {"type": "rsi", "period": 14, "interval": "week"}],
    },
    "weekly": {
        "interval": "week",
        "specs": [{"type": "ema", "period": 10},
                  {"type": "ema", "period": 20},
                  {"type": "macd", "fast_period": 12,
                   "slow_period": 26, "signal_period": 9}],
    },
    "reversals": {
        "interval": "day",
        "specs": [{"type": "rsi", "period": 14},
                  {"type": "atr", "period": 14}],
    },
}
PRESETS["full"] = {
    "interval": "day",
    "specs": PRESETS["intraday"]["specs"] + [
        {"type": "sma", "period": 20},
        {"type": "macd", "fast_period": 12,
         "slow_period": 26, "signal_period": 9}],
}

DRIFT_PCT = 2.0  # matches daily_cache RESYNC_PCT (adjustment-basis drift)


def chunk(tickers: list, size: int = RH_MAX_SYMBOLS) -> list:
    """Order-preserving de-duped batches of at most `size` names.

    This is the array shape RH accepts: never send the whole pool in one
    call (500+ names); send these batches instead.
    """
    out, seen, cur = [], set(), []
    for t in tickers:
        s = str(t).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        cur.append(s)
        if len(cur) >= size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def rh_interval(yf_interval: str) -> tuple:
    """(RH interval, aggregate_n) for a yfinance interval.

    aggregate_n > 1 means combine that many fine bars client-side
    (15m = 3x 5minute - RH has no 15-minute bar).
    """
    try:
        return YF_TO_RH[yf_interval]
    except KeyError:
        raise ValueError(f"no RH interval for {yf_interval!r}")


def needs_aggregation(yf_interval: str) -> bool:
    """True when the RH side needs client-side bar aggregation."""
    return rh_interval(yf_interval)[1] > 1


def period_to_range(period: str, today=None) -> tuple:
    """yfinance period shorthand -> (start_time, end_time) RFC3339 UTC.

    RH historicals take explicit ranges, not "5d"/"1mo" shorthands.
    """
    try:
        days = PERIOD_DAYS[period]
    except KeyError:
        raise ValueError(f"unknown period {period!r}")
    if today is None:
        end = dt.datetime.now(dt.timezone.utc)
    elif isinstance(today, dt.datetime):
        end = today if today.tzinfo else today.replace(
            tzinfo=dt.timezone.utc)
    elif isinstance(today, dt.date):
        end = dt.datetime(today.year, today.month, today.day,
                          tzinfo=dt.timezone.utc)
    else:
        raise ValueError(f"bad today {today!r}")
    start = end - dt.timedelta(days=days)
    fmt = lambda d: d.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return fmt(start), fmt(end)


def call_plan(tickers: list, preset: str = "intraday",
              start_time: str = None) -> list:
    """MCP tool-call specs for narrowed single names (scalar-only params).

    Each entry is {"tool", "params"} ready for the agent to execute; no
    entry contains a list/dict param, so all of them pass the current
    bridge. start_time defaults to a 90d lookback (covers EMA50 warm-up).
    """
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}")
    if start_time is None:
        start_time, _ = period_to_range("3mo")
    cfg = PRESETS[preset]
    specs = []
    for t in dict.fromkeys(str(s).strip().upper() for s in tickers
                           if str(s).strip()):
        for need in cfg["specs"]:
            params = {"symbol": t, "type": need["type"],
                      "interval": need.get("interval", cfg["interval"]),
                      "start_time": start_time, "output": "latest"}
            for k in ("period", "fast_period", "slow_period",
                      "signal_period", "num_std", "multiplier"):
                if k in need:
                    params[k] = need[k]
            specs.append({"tool": "get_equity_technical_indicators",
                          "params": params})
    return specs


def array_specs(tickers: list, tool: str, extra: dict = None,
                size: int = RH_MAX_SYMBOLS) -> list:
    """Chunked array-param specs for the post-fix bridge.

    Same batches chunk() builds, shaped for get_equity_historicals /
    get_equity_quotes / get_equity_fundamentals. NOT executable through
    the current bridge (it rejects array params) - kept so the bulk path
    is one search-and-enable when the bridge is fixed.
    """
    out = []
    for batch in chunk(tickers, size):
        params = {"symbols": batch}
        params.update(extra or {})
        out.append({"tool": tool, "params": params})
    return out


def to_15m(fine) -> "pd.DataFrame":
    """5-minute OHLC(V) -> 15-minute bars the algo rules consume.

    Pull 5minute bars from RH (get_equity_historicals / technicals when
    the bridge accepts arrays; single-symbol until then), assemble each
    :00/:15/:30/:45 bucket locally (O first, H max, L min, C last, V
    sum), and feed the result straight into the existing 15m consumers
    (rps_live wants 200x 15m, len >= 40; EMA live-touch reads the last
    close). Works on RH begins_at (UTC) or yfinance (ET-naive) stamps -
    flooring is tz-agnostic. The still-forming bucket is KEPT: drop it
    for backtest parity, keep it for live-touch.
    """
    return aggregate_bars(fine, rule="15min")


def aggregate_bars(fine, rule: str = "15min") -> "pd.DataFrame":
    """Fine OHLC(V) bars -> coarse session-aligned buckets."""
    import pandas as pd

    f = fine.copy()
    f.columns = [str(c).capitalize() for c in f.columns]
    for col in ("Open", "High", "Low", "Close"):
        if col not in f.columns:
            raise ValueError(f"missing {col} column")
    if not len(f):
        return f
    f.index = pd.to_datetime(f.index)
    g = f.groupby(f.index.floor(rule))
    agg = pd.DataFrame({"Open": g["Open"].first(),
                        "High": g["High"].max(),
                        "Low": g["Low"].min(),
                        "Close": g["Close"].last()})
    if "Volume" in f.columns:
        agg["Volume"] = g["Volume"].sum()
    return agg.sort_index()


def _envelope_data(result: dict) -> dict:
    data = result.get("data", result) if isinstance(result, dict) else {}
    return data if isinstance(data, dict) else {}


def merge_technicals(results: list) -> dict:
    """MCP technical results -> {ticker: {ema50, rsi, atr, macd, ...}}.

    Accepts full tool envelopes ({"data": {...}}) or bare data dicts.
    Single-output types alias to the repo's facts keys (ema50, rsi14,
    atr); macd keeps its line/signal/histogram fields.
    """
    merged: dict = {}
    for res in results:
        data = _envelope_data(res)
        t = str(data.get("symbol", "")).strip().upper()
        if not t:
            continue
        row = merged.setdefault(t, {})
        for ind in data.get("indicators", []) or []:
            itype = ind.get("type", "")
            params = ind.get("params", {}) or {}
            series = ind.get("series", []) or []
            if not series:
                continue
            pt = series[-1]
            if itype == "macd":
                for k in ("macd", "signal", "histogram"):
                    if isinstance(pt.get(k), (int, float)):
                        row[k] = float(pt[k])
            else:
                v = pt.get("value")
                if not isinstance(v, (int, float)):
                    continue
                if itype == "ema":
                    row[f"ema{params.get('period', '')}"] = float(v)
                elif itype == "sma":
                    row[f"sma{params.get('period', '')}"] = float(v)
                elif itype == "rsi":
                    if data.get("interval") == "week":
                        row["wrsi"] = float(v)
                    else:
                        row["rsi"] = float(v)
                        row[f"rsi{params.get('period', '')}"] = float(v)
                elif itype == "atr":
                    row["atr"] = float(v)
                else:
                    row[f"{itype}_{pt.get('value')!s}"] = float(v)
    return merged


def local_technicals(frame) -> dict:
    """Same keys as merge_technicals(), computed off a cached OHLC frame.

    Offline fallback + parity oracle: uses the repo's own formulas, so
    any drift vs merge_technicals() is data-basis drift (split vs
    split+dividend adjustment), not formula drift.
    """
    from simple_scan import ema, rsi14  # noqa: E402 (repo formulas)

    c = frame["Close"].astype(float).dropna()
    h, l, pc = (frame["High"].astype(float), frame["Low"].astype(float),
                c.shift(1))
    tr = pd_concat_max(h - l, (h - pc).abs(), (l - pc).abs())
    out = {f"ema{n}": float(ema(c, n).iloc[-1]) for n in (8, 13, 21, 50)}
    out["rsi"] = float(rsi14(c).iloc[-1])
    out["rsi14"] = out["rsi"]
    out["atr"] = float(tr.ewm(alpha=1.0 / 14, adjust=False).mean().iloc[-1])
    out["close"] = float(c.iloc[-1])
    return out


def pd_concat_max(*series):
    """Column-wise max without importing pandas at module top."""
    import pandas as pd

    return pd.concat(list(series), axis=1).max(axis=1)


ABS_KEYS = ("rsi", "rsi14", "wrsi")  # 0-100 oscillators: drift is
# measured in absolute points, not percent (41 -> 44 RSI is 2.5 points
# of signal, not a 5.7% move - relative % would false-alarm inside the
# 40-45 band). Prices (ema/atr/sma/macd) stay relative percent.
ABS_TOL = 3.0


def compare(local: dict, remote: dict, tol_pct: float = DRIFT_PCT,
            tol_abs: float = ABS_TOL) -> dict:
    """Drift between local (cached-bar) and RH server values.

    Returns {"ok", "drift", "breaches"}; ok is False when any shared
    numeric key drifts more than tol_pct (prices, default 2% - the same
    bar the cache uses to detect rewritten history) or tol_abs points
    (RSI family, default 3.0). Residual drift is data-basis drift
    (split-only vs split+dividend adjustment, warm-up length), not
    formula drift - so for a narrowed name the remote price set
    (ema/rsi/atr/wrsi) applies as one unit via apply_remote(), never
    one cherry-picked key. Regime/context keys the server does not
    provide (shorts_below, prev_close) stay local by design.
    """
    drift, breaches = {}, []
    for k, lv in local.items():
        rv = remote.get(k)
        if not isinstance(lv, (int, float)) or not isinstance(
                rv, (int, float)):
            continue
        if k in ABS_KEYS:
            gap = abs(float(lv) - float(rv))
            drift[k] = round(gap, 3)
            if gap > tol_abs:
                breaches.append(k)
        else:
            denom = abs(float(lv)) or 1.0
            pct = abs(float(lv) - float(rv)) / denom * 100.0
            drift[k] = round(pct, 3)
            if pct > tol_pct:
                breaches.append(k)
    return {"ok": not breaches, "drift": drift, "breaches": breaches}


OVERRIDES_NAME = "rh_overrides.json"  # under ROOT/data; written by
# ops/rh_check.py --write after the agent executes a --plan. Read by the
# intraday live-touch gate. Never committed (live-mutated, like state).

REQUIRED_REMOTE = ("ema50", "atr", "rsi")  # wholesale-swap key set


def load_overrides(path: str, today=None) -> dict:
    """{ticker: remote row} stamped for `today`; {} when missing/stale.

    Stale (yesterday's tape on today's gate) and corrupt files read as
    empty, so the gate silently stays local - never half-remote.
    """
    if today is None:
        today = dt.date.today()
    stamp = (today.isoformat()
             if isinstance(today, (dt.date, dt.datetime)) else str(today))
    try:
        with open(path) as f:
            doc = json.load(f)
    except Exception:
        return {}
    if not isinstance(doc, dict) or doc.get("date") != stamp:
        return {}
    tickers = doc.get("tickers")
    return tickers if isinstance(tickers, dict) else {}


def apply_remote(facts: dict, overrides: dict, today=None) -> dict:
    """Swap the full remote price set into intraday facts rows.

    A row flips only when its override carries ALL of REQUIRED_REMOTE;
    wrsi/ema8/13/21 ride along when present. Anything else (missing
    keys, unknown tickers) stays local - the gate never sees a
    half-remote row. Flipped rows are tagged _src="rh", the rest
    _src="local" (for the run log line). Returns facts, mutated.
    """
    for t, row in facts.items():
        rem = overrides.get(t) if isinstance(overrides, dict) else None
        if isinstance(rem, dict) and all(
                isinstance(rem.get(k), (int, float))
                for k in REQUIRED_REMOTE):
            for k in set(REQUIRED_REMOTE) | {"wrsi", "ema8", "ema13",
                                             "ema21"}:
                if isinstance(rem.get(k), (int, float)):
                    row[k] = float(rem[k])
            row["_src"] = "rh"
        else:
            row.setdefault("_src", "local")
    return facts
