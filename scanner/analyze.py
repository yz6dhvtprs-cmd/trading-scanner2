"""Agent2 analyzer (15-min): directional states per watchlist ticker, one text.

Usage: python scanner/analyze.py --channels dry
Reads scanner/agent_watch.json (<=25). Pulls daily + 1h + 15m per ticker.
States: LONG / SHORT / REVERSAL? (possible upcoming reversal) /
        REJECT? (possible upcoming rejection at level).
Combined message covers only tickers whose state CHANGED since last run
(scanner/agent_state.json). Format matches the ops template exactly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from combos import add_features, level_of, risk_of, signal_mask  # noqa: E402
from indicators2 import add_extra  # noqa: E402
from notify import alert, load_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(ROOT, "scanner", "agent_watch.json")
STATE = os.path.join(ROOT, "scanner", "agent_state.json")


def flat(h):
    if isinstance(h.columns, pd.MultiIndex):
        h.columns = h.columns.get_level_values(0)
    h.columns = [c.capitalize() for c in h.columns]
    return h


def candle(bar) -> str:
    o, h, l, c = bar.Open, bar.High, bar.Low, bar.Close
    rng = h - l
    if rng <= 0:
        return "-"
    body = abs(c - o)
    if body < 0.1 * rng:
        return "doji"
    if (min(o, c) - l) > 2 * body and (h - max(o, c)) < body:
        return "hammer" if c >= o else "hammer_dn"
    if (h - max(o, c)) > 2 * body and (min(o, c) - l) < body:
        return "shooting_star" if c <= o else "shooting_star_up"
    return "-"


def fib_levels(hi: float, lo: float) -> dict:
    d = hi - lo
    return {r: hi - d * r for r in (0.382, 0.5, 0.618)}


def near(price: float, level: float, tol: float = 0.003) -> bool:
    return abs(price - level) / level <= tol


def analyze(t: str, d: pd.DataFrame, h1: pd.DataFrame,
            m15: pd.DataFrame) -> dict:
    """Returns {state, price, target, stop, note}."""
    i = len(d) - 1
    c = float(d["Close"].iloc[i])
    hi20 = float(d["hi20"].iloc[i])
    lo20 = float(d["lo20"].iloc[i])
    fib = fib_levels(hi20, lo20)
    pat_d = candle(d.iloc[i])
    pat_15 = candle(m15.iloc[-1]) if len(m15) else "-"
    rsi = float(d["rsi"].iloc[i])

    # 1. base signals first (never replaced)
    for sname, dd, label in (("breakout", 1, "Long"), ("pullback", 1, "Long")):
        if signal_mask(d, sname, dd, 20.0)[i]:
            risk = risk_of(d, sname, dd, i)
            stop = c - risk
            return {"state": label, "price": round(c, 2),
                    "target": f"trail (ref +3R {(c + 3 * risk):.2f})",
                    "stop": round(stop, 2), "note": f"{sname} trigger"}
    # 2. rejection: at 20d-high or Fib + reversal candle
    levels = [("20d-high", hi20)] + [(f"fib{r}", v) for r, v in fib.items()]
    for name, lvl in levels:
        if near(c, lvl) and pat_d in ("shooting_star", "doji") \
                and rsi > 60:
            return {"state": "Possible upcoming Rejection",
                    "price": round(c, 2), "target": f"{lvl * 0.97:.2f}",
                    "stop": round(lvl * 1.005, 2),
                    "note": f"{pat_d} at {name} {lvl:.2f}"}
    # 3. reversal: hammer/doji at 20d-low or Fib + RSI washed out
    for name, lvl in [("20d-low", lo20)] + [(f"fib{r}", v)
                                            for r, v in fib.items()]:
        if near(c, lvl) and pat_d in ("hammer", "hammer_dn", "doji") \
                and rsi < 40:
            return {"state": "Possible upcoming Reversal",
                    "price": round(c, 2), "target": f"{lvl * 1.03:.2f}",
                    "stop": round(lvl * 0.995, 2),
                    "note": f"{pat_d} at {name} {lvl:.2f}"}
    # 4. continuation read from 15m alignment (informational, no levels)
    if len(m15) and pat_15 in ("hammer", "hammer_dn") and rsi > 50:
        return {"state": "Possible upcoming Reversal", "price": round(c, 2),
                "target": "watch", "stop": "watch",
                "note": f"15m {pat_15} in uptrend"}
    return {"state": "-", "price": round(c, 2), "target": "-",
            "stop": "-", "note": "no change"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="dry")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-7)))
    mins = now.hour * 60 + now.minute
    if not a.force and (now.weekday() >= 5 or not (6 * 60 + 30 <= mins <= 13 * 60)):
        return 0  # outside 6:30a-1:00p PT window
    if not a.force:
        import subprocess
        if subprocess.run(["python", "scanner/market_calendar.py"], cwd=ROOT,
                           capture_output=True).returncode != 0:
            return 0
    if not os.path.exists(WATCH):
        print("no watchlist; screener first", flush=True)
        return 0
    watch = json.load(open(WATCH))
    syms = list(watch)[:25]
    import yfinance as yf
    pxd = yf.download(syms, period="1y", interval="1d", auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    lines, states = [], {}
    prev = json.load(open(STATE)) if os.path.exists(STATE) else {}
    for t in syms:
        try:
            h = pxd[t] if len(syms) > 1 else pxd
            h = flat(h).dropna(subset=["Close"])
            if len(h) < 225:
                continue
            d = add_extra(add_features(h))
            h1 = flat(yf.download(t, period="1mo", interval="1h",
                                  auto_adjust=True, progress=False))
            m15 = flat(yf.download(t, period="5d", interval="15m",
                                   auto_adjust=True, progress=False))
            r = analyze(t, d, h1, m15)
            key = (r["state"], r["price"], r["note"])
            states[t] = {"state": r["state"], "note": r["note"]}
            if r["state"] != "-" and prev.get(t) != states[t]:
                lines.append(
                    f'"{t} - {r["state"]} - at {r["price"]} price - '
                    f'target price {r["target"]} - '
                    f'stop loss {r["stop"]}. ({r["note"]})')
        except Exception as e:
            print(f"skip {t}: {e}", flush=True)
    json.dump(states, open(STATE, "w"))
    cfg = load_config()
    chans = [c.strip() for c in a.channels.split(",")]
    if lines:
        msg = f"ANALYZER {dt.datetime.now().strftime('%H:%M')}PT " \
              f"({len(lines)} updates):\n" + "\n".join(lines)
        print(msg, flush=True)
        for res in alert(msg, chans, cfg, title="Trade updates"):
            print("  ", res, flush=True)
    else:
        print("analyzer: no state changes", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
