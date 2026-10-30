"""Live gate for one candidate option trade.

The split of labour, deliberately:

* **this script** owns the deterministic part — the bar maths (EMA/RSI/ATR,
  distance from EMA20, position in the 20-day range, SPY regime, VIX bucket),
  the bucket labels, the lookup of what each bucket actually did in the owner's
  own trade history (`<TRADE_AUDIT_DIR>/trade_priors.json`, written by
  the trade-audit skill), and the policy vetoes that are not
  statistics (no averaging down, daily cap, revenge timer, stop required).
* **the model** owns everything a script cannot see — fetching the live quote,
  delta/IV/spread from the Robinhood MCP, how many positions are already open,
  what the chart looks like, what the news is, what the owner is trying to do —
  and the final call. The `/trade-check` skill is that loop.

Three kinds of check, labelled in the output:

* policy vetoes — risk management, hardcoded (no averaging down, daily cap,
  two-loss stop, revenge timer, size cap, a stop on the underlying);
* history vetoes — rules the audit derived (short-dated OTM, bearish in a bull
  tape, the earnings reaction day); each is shown with the live record for its
  bucket, and if that record ever turns good the output says so as a conflict
  instead of hiding it;
* five scored gates (expiry, contract, trend, trigger, regime) — pass/fail by
  rule, each annotated with the bucket's own n / win rate / profit factor from
  the priors file; the expiry and regime gates read the priors
  directly, so re-running the audit moves them without a code change.

The model that drives this (`/trade-check`) sees every conflict and the whole
context and makes the final call — but may only ever be more conservative than
the gate (`trade_queue.clamp`).

CLI:

    python3 gate.py --json '{"ticker":"AAPL","otype":"call",
        "dte":45,"strike":250,"qty":2,"premium":1800,"delta":0.62,"stop_px":238,
        "live":{"last":255.1,"day_open":252.0,"vix":17.4,"new_today":1,
                "min_since_loss":90,"adding_to_loser":false}}'

    python3 gate.py --ticker AAPL --otype call --dte 45 \
        --strike 250 --premium 1800 --delta 0.62 --stop-px 238 --last 255.1

Everything under `live` is optional; a missing field fails its gate rather than
passing it silently. Exit status is 0 for TAKE, 1 for WAIT, 2 for NO.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GATE_VERSION = 1
PRIORS_PATH = os.environ.get("TRADE_PRIORS", os.path.join(
    os.environ.get("TRADE_AUDIT_DIR", "trade-audit-data"), "trade_priors.json"))

# A bucket needs this many historical trades before its own numbers are used.
MIN_N = 20
# Profit factor below this (with enough trades) is veto-grade; above the second
# is a positive prior. Between them the bucket is neutral.
VETO_PF = float(os.environ.get("TRADE_VETO_PF", 0.80))
GOOD_PF = 1.20

# ---------------------------------------------------------------- bucket labels
# One definition, used both to summarize the history (priors.py) and to label a
# live candidate. Edges must not be changed on one side only.

DTE_EDGES = [-1, 0, 1, 2, 4, 7, 14, 30, 60, 100000]
DTE_LABELS = ["0", "1", "2", "3-4", "5-7", "8-14", "15-30", "31-60", "60+"]
MONEY_EDGES = [-1e9, -3, -1, 0, 1, 3, 1e9]
MONEY_LABELS = ["ITM>3%", "ITM1-3", "ITM<1", "OTM<1", "OTM1-3", "OTM>3%"]
EXT_EDGES = [-1e9, -2, -1, 0, 1, 2, 1e9]
EXT_LABELS = ["<-2", "-2..-1", "-1..0", "0..1", "1..2", ">2"]
RSI_EDGES = [0, 30, 40, 50, 60, 70, 100]
RSI_LABELS = ["<30", "30-40", "40-50", "50-60", "60-70", ">70"]
VIX_EDGES = [0, 14, 18, 22, 30, 1000]
VIX_LABELS = ["<14", "14-18", "18-22", "22-30", ">30"]
HOUR_EDGES = [0, 10, 11, 13, 15, 15.5, 24]
HOUR_LABELS = ["9:30-10", "10-11", "11-13", "13-15", "15-15:30", "15:30-16"]
PREM_EDGES = [0, 250, 500, 1000, 2500, 5000, 1e9]
PREM_LABELS = ["<250", "250-500", "500-1k", "1k-2.5k", "2.5k-5k", ">5k"]
SEQ_EDGES = [0, 1, 3, 6, 1e9]
SEQ_LABELS = ["1st", "2-3", "4-6", "7+"]
REENTRY_EDGES = [-1, 2, 10, 60, 1e9]
REENTRY_LABELS = ["<2m", "2-10m", "10-60m", ">1h"]
RANGEPOS_EDGES = [-0.01, 0.2, 0.4, 0.6, 0.8, 1.01]
RANGEPOS_LABELS = ["bottom", "low", "mid", "high", "top"]

ETFS = {"SPY", "QQQ", "IWM", "DIA", "UVXY", "TQQQ", "SQQQ", "SLV", "GLD", "TLT", "SPXW", "VIX"}


def _cut(v, edges, labels):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    for i in range(len(edges) - 1):
        if edges[i] < v <= edges[i + 1]:
            return labels[i]
    return labels[0] if v <= edges[0] else labels[-1]


def label_dte(d): return _cut(d, DTE_EDGES, DTE_LABELS)
def label_money(pct): return _cut(pct, MONEY_EDGES, MONEY_LABELS)
def label_ext(a): return _cut(a, EXT_EDGES, EXT_LABELS)
def label_rsi(r): return _cut(r, RSI_EDGES, RSI_LABELS)
def label_vix(v): return _cut(v, VIX_EDGES, VIX_LABELS)
def label_hour(h): return _cut(h, HOUR_EDGES, HOUR_LABELS)
def label_prem(p): return _cut(p, PREM_EDGES, PREM_LABELS)
def label_seq(n): return _cut(n, SEQ_EDGES, SEQ_LABELS)
def label_reentry(m): return _cut(m, REENTRY_EDGES, REENTRY_LABELS)
def label_rangepos(p): return _cut(p, RANGEPOS_EDGES, RANGEPOS_LABELS)


def label_trend(above20, above50):
    if above20 is None or above50 is None:
        return None
    if above20 and above50:
        return "up"
    if not above20 and not above50:
        return "down"
    return "mixed"


# ---------------------------------------------------------------------- priors

def load_priors(path: str = None) -> dict:
    p = path or PRIORS_PATH
    try:
        return json.load(open(p))
    except Exception:
        return {"buckets": {}, "overall": {}, "generated": None}


def prior(priors: dict, family: str, key) -> dict | None:
    """History for one bucket: {n, win, pf} or None when thin/unknown."""
    if key is None:
        return None
    b = (priors.get("buckets") or {}).get(family) or {}
    hit = b.get(str(key))
    if not hit or hit.get("n", 0) < MIN_N:
        return None
    return hit


def verdict_of(pf: float | None) -> str:
    if pf is None:
        return "unknown"
    if pf < VETO_PF:
        return "bad"
    if pf >= GOOD_PF:
        return "good"
    return "neutral"


# -------------------------------------------------------------------- features

def indicators(d: pd.DataFrame) -> pd.DataFrame:
    """EMA/RSI/ATR/range columns on a capitalized OHLCV daily frame."""
    f = d.copy()
    c = f["Close"]
    for n in (10, 20, 50, 200):
        f[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    delta = c.diff()
    up, dn = delta.clip(lower=0), -delta.clip(upper=0)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean()
    f["rsi14"] = 100 - 100 / (1 + rs)
    tr = pd.concat([f["High"] - f["Low"], (f["High"] - c.shift()).abs(),
                    (f["Low"] - c.shift()).abs()], axis=1).max(axis=1)
    f["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    f["hi20"] = f["High"].rolling(20).max()
    f["lo20"] = f["Low"].rolling(20).min()
    f["ret5"] = c.pct_change(5)
    return f


def yf_daily(ticker: str, tf: str = "1d") -> pd.DataFrame:
    """Two years of daily bars from yfinance (capitalized OHLCV columns)."""
    import yfinance as yf
    t = {"VIX": "^VIX", "SPXW": "^SPX", "BRKB": "BRK-B"}.get(ticker, ticker)
    d = yf.download(t, period="2y", interval="1d", auto_adjust=False, progress=False)
    if d is None or not len(d):
        return pd.DataFrame()
    d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    return d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])


def yf_earnings(ticker: str) -> list:
    import yfinance as yf
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=12)
        return sorted({i.date() for i in ed.index}) if ed is not None else []
    except Exception:
        return []


def _bars(ticker: str, loader, ctx: dict, min_bars: int = 60):
    """Cached daily bars, bootstrapping the cache when the name is thin.
    Index ETFs and anything never scanned before are usually missing."""
    d = loader(ticker)
    if d is not None and len(d) >= min_bars:
        return d
    try:
        d = yf_daily(ticker)
        ctx.setdefault("fetched", []).append(ticker)
    except Exception as ex:
        ctx["missing"].append(f"could not fetch {ticker} bars ({type(ex).__name__})")
    return d


def context(ticker: str, asof=None, live: dict = None, loader=None,
            earnings_cache: dict = None) -> dict:
    """Deterministic entry context for one name: the previous completed bar's
    trend/RSI/ATR state, where the live price sits in the day and the 20-day
    range, the SPY regime, VIX and earnings proximity.

    `live` supplies what only the market can answer: last, day_open, day_high,
    day_low, vix. Anything missing comes back as None and fails its gate.
    """
    live = dict(live or {})
    asof = asof or dt.date.today()
    if isinstance(asof, str):
        asof = dt.date.fromisoformat(asof[:10])
    if loader is None:
        loader = yf_daily
    ctx: dict = {"ticker": ticker, "asof": str(asof), "missing": []}

    d = _bars(ticker, loader, ctx)
    if d is None or len(d) < 60:
        ctx["missing"].append(f"daily bars for {ticker}")
        return ctx
    f = indicators(d)
    prev = f.iloc[-1]
    if "last" in live and live["last"]:
        S = float(live["last"])
    else:
        S = float(prev["Close"])
        ctx["missing"].append("live quote (used last cached close)")
    def num(x):
        """float, or None for NaN/inf so a flat or thin series reads as unknown"""
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
        return x if np.isfinite(x) else None
    atr = num(prev["atr14"])
    ctx.update(
        last=S, prev_close=float(prev["Close"]), bar_date=str(f.index[-1].date()),
        ema10=num(prev["ema10"]), ema20=num(prev["ema20"]), ema50=num(prev["ema50"]),
        ema200=num(prev["ema200"]), rsi=num(prev["rsi14"]), atr=atr,
        above_e20=bool(S > prev["ema20"]), above_e50=bool(S > prev["ema50"]),
        above_e200=bool(S > prev["ema200"]),
        dist20_atr=num((S - prev["ema20"]) / atr) if atr else None,
        ret5=num(prev["ret5"]),
        near_hi20=num(S / prev["hi20"] - 1) if num(prev["hi20"]) else None,
        near_lo20=num(S / prev["lo20"] - 1) if num(prev["lo20"]) else None,
    )
    if ctx["rsi"] is None or ctx["dist20_atr"] is None:
        ctx["missing"].append("RSI/ATR undefined on this series (flat or too short)")
    ctx["trend"] = label_trend(ctx["above_e20"], ctx["above_e50"])
    hi = live.get("day_high") or S
    lo = live.get("day_low") or S
    if live.get("day_open"):
        ctx["from_open"] = float(S / float(live["day_open"]) - 1)
        hi, lo = max(hi, float(live["day_open"])), min(lo, float(live["day_open"]))
    else:
        ctx["from_open"] = None
        ctx["missing"].append("day open")
    ctx["range_pos"] = float((S - lo) / (hi - lo)) if hi > lo else None

    try:
        spy = indicators(_bars("SPY", loader, ctx))
        p = spy.iloc[-1]
        sl = float(live.get("spy_last") or p["Close"])
        ctx["spy_above_e20"] = bool(sl > p["ema20"])
        ctx["spy_above_e50"] = bool(sl > p["ema50"])
        ctx["spy_regime"] = ("SPY up" if ctx["spy_above_e20"] and ctx["spy_above_e50"]
                             else "SPY down" if not ctx["spy_above_e20"] and not ctx["spy_above_e50"]
                             else "SPY mixed")
    except Exception:
        ctx["spy_regime"] = None
        ctx["missing"].append("SPY regime")
    ctx["vix"] = float(live["vix"]) if live.get("vix") else None
    if ctx["vix"] is None:
        ctx["missing"].append("VIX")

    if ticker in ETFS:
        ctx["earn_next_d"] = ctx["earn_prev_d"] = None
        return ctx
    try:
        ds = (earnings_cache or {}).get(ticker) if earnings_cache is not None else yf_earnings(ticker)
        ds = sorted(ds or [])
        nxt = next((x for x in ds if x >= asof), None)
        past = [x for x in ds if x <= asof]
        ctx["earn_next_d"] = (nxt - asof).days if nxt else None
        ctx["earn_prev_d"] = (asof - past[-1]).days if past else None
    except Exception:
        ctx["earn_next_d"] = ctx["earn_prev_d"] = None
        ctx["missing"].append("earnings dates")
    return ctx


# ------------------------------------------------------------------------ gate

def gate(cand: dict, ctx: dict = None, priors: dict = None, cfg: dict = None) -> dict:
    """Score one candidate. Returns vetoes, gate rows (each with the history
    behind it), the count of passing gates and a verdict."""
    cfg = {"max_new_per_day": 3, "max_premium": 2500, "revenge_min": 60,
           "min_gates": 3, **(cfg or {})}
    priors = priors if priors is not None else load_priors()
    live = dict(cand.get("live") or {})
    ctx = ctx if ctx is not None else context(cand["ticker"], cand.get("asof"), live)
    otype = cand["otype"].lower()
    side = cand.get("side", "long").lower()
    bullish = (otype == "call") == (side == "long")
    dirn = "bullish" if bullish else "bearish"
    dte = int(cand["dte"])
    S = ctx.get("last")
    strike = cand.get("strike")
    otm = None
    if strike and S:
        otm = (strike / S - 1) * 100 if otype == "call" else (1 - strike / S) * 100

    vetoes, gates, notes = [], [], []

    def add_gate(name, ok, why, family=None, key=None):
        h = prior(priors, family, key) if family else None
        hv = verdict_of(h["pf"]) if h else "unknown"
        gates.append({"gate": name, "ok": bool(ok), "why": why,
                      "bucket": f"{family}:{key}" if family else None,
                      "history": h, "history_verdict": hv,
                      # the rule and this account's own record for the bucket disagree:
                      # not resolved here, handed to the model to adjudicate
                      "conflict": (not ok and hv == "good") or (bool(ok) and hv == "bad")})

    def add_veto(rule, why, family=None, key=None, policy=False):
        h = prior(priors, family, key) if family else None
        if not policy and (verdict_of(h["pf"]) if h else "unknown") != "bad":
            # a history rule only blocks when YOUR record for that bucket is bad
            notes.append(f"advisory, not blocking: {rule} ({why}); " + (
                f"your history for {family}:{key} is PF {h['pf']} on n={h['n']}" if h
                else "no history for this bucket yet"))
            return
        vetoes.append({"rule": rule, "why": why, "basis": "policy" if policy else "history",
                       "bucket": f"{family}:{key}" if family else None, "history": h,
                       "conflict": False})

    # --- policy vetoes (risk management, not edge measurement)
    if live.get("adding_to_loser"):
        add_veto("no averaging down", "this would add to a position that is down",
                 "adds", "avg down", policy=True)
    if live.get("new_today") is not None and live["new_today"] >= cfg["max_new_per_day"]:
        add_veto("daily cap", f"{live['new_today']} new positions already today "
                 f"(cap {cfg['max_new_per_day']})", "seq", label_seq(live["new_today"] + 1), policy=True)
    if live.get("losses_today") is not None and live["losses_today"] >= 2:
        add_veto("two-loss stop", f"{live['losses_today']} losses already today", policy=True)
    if live.get("min_since_loss") is not None and live["min_since_loss"] < cfg["revenge_min"]:
        add_veto("revenge timer", f"{live['min_since_loss']:.0f} min since the last loss "
                 f"(wait {cfg['revenge_min']})", "reentry_after_loss",
                 label_reentry(live["min_since_loss"]), policy=True)
    prem = cand.get("premium")
    if prem and prem > cfg["max_premium"]:
        add_veto("size cap", f"premium {prem:.0f} over the {cfg['max_premium']:.0f} tier",
                 "premium", label_prem(prem), policy=True)
    if not cand.get("stop_px"):
        add_veto("stop required", "no underlying price defined that invalidates the trade",
                 policy=True)
    if cand.get("stop_px") and S:
        bad = (cand["stop_px"] >= S) if bullish else (cand["stop_px"] <= S)
        if bad:
            add_veto("stop required", f"stop {cand['stop_px']} is on the wrong side of {S:.2f}",
                     policy=True)

    # --- history vetoes: this account's own record for the bucket
    fam_money = "moneyness_0_1dte" if dte <= 1 else "moneyness"
    if dte <= 1 and otm is not None and otm > 1:
        add_veto("short-dated OTM", f"{dte} DTE and {otm:.1f}% out of the money",
                 fam_money, label_money(otm))
    if not bullish and ctx.get("spy_regime") == "SPY up":
        extreme = (ctx.get("dist20_atr") or 0) > 2 and (ctx.get("rsi") or 0) > 70
        if not extreme:
            add_veto("bearish in a bull tape",
                     "SPY above EMA20 and EMA50 and the name is not stretched "
                     f"(dist20 {ctx.get('dist20_atr')}, RSI {ctx.get('rsi')})",
                     "dir_x_spy", f"{dirn}|SPY up")
    if ctx.get("earn_prev_d") is not None and ctx["earn_prev_d"] <= 1:
        add_veto("earnings reaction", f"reported {ctx['earn_prev_d']} day(s) ago",
                 "earnings", "post-earnings day")

    # --- the five scored gates
    h_dte = prior(priors, "dte", label_dte(dte))
    add_gate("expiry", (h_dte is not None and h_dte["pf"] >= GOOD_PF) or dte >= 31,
             f"{dte} DTE", "dte", label_dte(dte))
    dlt = cand.get("delta")
    if dlt is None:
        add_gate("contract", False, "delta not supplied (fetch it from the chain, never derive it)",
                 fam_money, label_money(otm))
    else:
        add_gate("contract", 0.55 <= abs(dlt) <= 0.75,
                 f"delta {abs(dlt):.2f}" + (f", {otm:+.1f}% OTM" if otm is not None else ""),
                 fam_money, label_money(otm))
    ext, rsi = ctx.get("dist20_atr"), ctx.get("rsi")
    if bullish:
        aligned = ctx.get("trend") == "up" and (ext is None or ext <= 2)
        pullback = (ext is not None and ext <= 0.5) and (rsi is not None and rsi < 40)
        add_gate("trend", aligned or pullback,
                 f"{ctx.get('trend')} trend, {ext if ext is None else round(ext, 2)} ATR from EMA20, "
                 f"RSI {rsi if rsi is None else round(rsi)}", "dir_x_ext", f"{dirn}|{label_ext(ext)}")
    else:
        add_gate("trend", ctx.get("spy_regime") == "SPY down"
                 or (ext is not None and ext > 2 and rsi is not None and rsi > 70),
                 f"{ctx.get('spy_regime')}, {ext if ext is None else round(ext, 2)} ATR from EMA20, "
                 f"RSI {rsi if rsi is None else round(rsi)}", "dir_x_ext", f"{dirn}|{label_ext(ext)}")
    fo, rp = ctx.get("from_open"), ctx.get("range_pos")
    with_move = None if fo is None else (fo > 0) == bullish
    good_pos = None if rp is None else (rp >= 0.8 if bullish else rp <= 0.2)
    add_gate("trigger", bool(with_move) and bool(good_pos),
             f"move since open {'n/a' if fo is None else f'{fo * 100:+.2f}%'}, "
             f"position in day range {'n/a' if rp is None else round(rp, 2)}"
             + ("" if with_move is not False else " — entering against the move"),
             "range_pos", None if rp is None else f"{dirn}|{label_rangepos(rp)}")
    hour = live.get("entry_hour")
    h_vix = prior(priors, "vix", label_vix(ctx.get("vix")))
    h_hour = prior(priors, "hour", label_hour(hour))
    regime_ok = (h_vix is None or h_vix["pf"] >= VETO_PF) and (h_hour is None or h_hour["pf"] >= VETO_PF)
    add_gate("regime", ctx.get("vix") is not None and regime_ok,
             f"VIX {ctx.get('vix')} ({label_vix(ctx.get('vix'))})"
             + (f", entry {label_hour(hour)} ET" if hour is not None else ""),
             "vix", label_vix(ctx.get("vix")))

    passed = sum(1 for g in gates if g["ok"])
    bad_hist = [g for g in gates if g["history_verdict"] == "bad"]
    conflicts = [g for g in gates if g["conflict"]]
    for g in conflicts:
        h = g["history"]
        notes.append(f"conflict on {g['gate']}: rule says {'PASS' if g['ok'] else 'FAIL'} but "
                     f"{g['bucket']} ran PF {h['pf']} on n={h['n']} — adjudicate, do not ignore")
    if vetoes:
        verdict = "NO"
    elif passed >= cfg["min_gates"] and not bad_hist:
        verdict = "TAKE"
    elif passed >= cfg["min_gates"]:
        verdict = "TAKE SMALLER"
    else:
        verdict = "WAIT"
    core_missing = [m for m in ctx.get("missing", []) if "bars" in m or "RSI/ATR" in m]
    if core_missing and verdict.startswith("TAKE"):
        verdict = "WAIT"
        notes.append("capped at WAIT: no usable bars/indicators for the name")
    if ctx.get("missing"):
        notes.append("unavailable at decision time: " + "; ".join(ctx["missing"]))
    if not priors.get("generated"):
        notes.append("no priors file — history checks are inactive; "
                     "run the trade-audit skill first")
    else:
        notes.append(f"priors from {priors['generated']} ({priors.get('n')} positions, "
                     f"overall PF {priors.get('overall', {}).get('pf')})")
    return {"candidate": {**{k: v for k, v in cand.items() if k != "live"}, "direction": dirn,
                          "otm_pct": None if otm is None else round(otm, 2)},
            "context": ctx, "vetoes": vetoes, "gates": gates, "conflicts": len(conflicts),
            "passed": passed, "of": len(gates), "verdict": verdict, "notes": notes,
            "gate_version": GATE_VERSION}


def render(res: dict) -> str:
    c = res["candidate"]
    otm = c.get("otm_pct")
    moneyness = "" if otm is None else f", {otm:+.1f}% OTM"
    out = [f"TRADE: {c['ticker']} {c.get('expiry') or str(c['dte']) + 'DTE'} "
           f"{c.get('strike', '?')}{c['otype'][0].upper()} x{c.get('qty', '?')} "
           f"({c['direction']}, delta {c.get('delta', '?')}, "
           f"premium {c.get('premium', '?')}{moneyness})"]
    for v in res["vetoes"]:
        h = v.get("history")
        hs = f"  [{v['bucket']}: n={h['n']} win={h['win']:.0%} PF={h['pf']}]" if h else ""
        out.append(f"VETO:  {v['rule']} — {v['why']} ({v['basis']}){hs}")
    for g in res["gates"]:
        h = g.get("history")
        hs = f"  [{g['bucket']}: n={h['n']} win={h['win']:.0%} PF={h['pf']} → {g['history_verdict']}]" if h else ""
        flag = "  <- CONFLICT" if g.get("conflict") else ""
        out.append(f"GATE:  {g['gate']:<9} {'PASS' if g['ok'] else 'FAIL'}  {g['why']}{hs}{flag}")
    out.append(f"GATES: {res['passed']}/{res['of']}")
    if c.get("stop_px"):
        out.append(f"STOP:  underlying {c['stop_px']}")
    out.append(f"VERDICT: {res['verdict']}")
    for n in res["notes"]:
        out.append(f"note:  {n}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", help="the whole candidate as JSON (preferred)")
    ap.add_argument("--ticker"); ap.add_argument("--otype", choices=["call", "put"])
    ap.add_argument("--side", default="long", choices=["long", "short"])
    ap.add_argument("--dte", type=int); ap.add_argument("--expiry")
    ap.add_argument("--strike", type=float); ap.add_argument("--qty", type=int)
    ap.add_argument("--premium", type=float); ap.add_argument("--delta", type=float)
    ap.add_argument("--stop-px", type=float, dest="stop_px")
    ap.add_argument("--last", type=float); ap.add_argument("--day-open", type=float, dest="day_open")
    ap.add_argument("--day-high", type=float, dest="day_high")
    ap.add_argument("--day-low", type=float, dest="day_low")
    ap.add_argument("--vix", type=float)
    ap.add_argument("--new-today", type=int, dest="new_today")
    ap.add_argument("--min-since-loss", type=float, dest="min_since_loss")
    ap.add_argument("--adding-to-loser", action="store_true", dest="adding_to_loser")
    ap.add_argument("--priors", default=None)
    ap.add_argument("--cfg", default=None, help='policy overrides as JSON, e.g. '
                    '\'{"max_new_per_day":2,"max_premium":1500,"revenge_min":90,"min_gates":3}\'')
    ap.add_argument("--as-json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--out", help="also write the JSON result to this file (atomic)")
    a = ap.parse_args(argv)
    if a.json:
        cand = json.loads(a.json)
    else:
        if not (a.ticker and a.otype and (a.dte is not None or a.expiry)):
            ap.error("--json, or at least --ticker --otype --dte")
        live = {k: getattr(a, k) for k in ("last", "day_open", "day_high", "day_low", "vix",
                                           "new_today", "min_since_loss", "adding_to_loser")
                if getattr(a, k) is not None}
        cand = {k: getattr(a, k) for k in ("ticker", "otype", "side", "dte", "expiry", "strike",
                                            "qty", "premium", "delta", "stop_px")
                if getattr(a, k) is not None}
        cand["live"] = live
    if cand.get("dte") is None and cand.get("expiry"):
        cand["dte"] = (dt.date.fromisoformat(cand["expiry"]) - dt.date.today()).days
    res = gate(cand, priors=load_priors(a.priors), cfg=json.loads(a.cfg) if a.cfg else None)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        tmp = a.out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(res, fh, indent=1, default=str)
        os.replace(tmp, a.out)
    print(json.dumps(res, indent=2, default=str) if a.as_json else render(res))
    return {"TAKE": 0, "TAKE SMALLER": 0, "WAIT": 1, "NO": 2}.get(res["verdict"], 1)


if __name__ == "__main__":
    sys.exit(main())
