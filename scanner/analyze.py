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


def candle(bar, prev=None) -> str:
    o, h, l, c = bar.Open, bar.High, bar.Low, bar.Close
    rng = h - l
    if rng <= 0:
        return "-"
    body = abs(c - o)
    if prev is not None:
        po, pc = prev.Open, prev.Close
        if c > o and pc < po and c >= po and o <= pc and body > abs(pc - po):
            return "bull_engulf"
        if c < o and pc > po and c <= po and o >= pc and body > abs(pc - po):
            return "bear_engulf"
    if body < 0.1 * rng:
        return "doji"
    if (min(o, c) - l) > 2 * body and (h - max(o, c)) < body:
        return "hammer" if c >= o else "hammer_dn"
    if (h - max(o, c)) > 2 * body and (min(o, c) - l) < body:
        return "shooting_star" if c <= o else "shooting_star_up"
    return "-"


BULL_PAT = ("hammer", "hammer_dn", "doji", "bull_engulf")
BEAR_PAT = ("shooting_star", "shooting_star_up", "doji", "bear_engulf")
ZONE_TOL = 0.01  # S/R zones, not lines


def fib_levels(hi: float, lo: float) -> dict:
    d = hi - lo
    return {r: hi - d * r for r in (0.382, 0.5, 0.618)}


def fib_targets(hi: float, lo: float, direction: int) -> dict:
    """Bigger-swing objectives: extensions beyond the swing."""
    d = hi - lo
    if direction == 1:
        return {"ext127": hi + d * 0.272, "ext162": hi + d * 0.618}
    return {"ext127": lo - d * 0.272, "ext162": lo - d * 0.618}


def near(price: float, level: float, tol: float = 0.003) -> bool:
    return abs(price - level) / level <= tol


def swing_levels(df: pd.DataFrame, lookback: int = 120, k: int = 5) -> tuple:
    """Support/resistance from fractal swings + Fib + EMA/MA levels.
    Returns (supports, resistances) sorted lists."""
    w = df.iloc[-lookback:]
    hi, lo = w["High"], w["Low"]
    win = 2 * k + 1
    ph = hi[(hi == hi.rolling(win, center=True).max())].dropna().tolist()
    pl = lo[(lo == lo.rolling(win, center=True).min())].dropna().tolist()
    shi, slo = float(hi.max()), float(lo.min())
    fib = list(fib_levels(shi, slo).values())
    dyn = [float(df[f"ema{n}"].iloc[-1]) for n in (21, 50, 200)
           if f"ema{n}" in df.columns]
    dyn += [float(df[f"sma{n}"].iloc[-1]) for n in (50, 200)
            if f"sma{n}" in df.columns]
    c = float(df["Close"].iloc[-1])
    lvls = sorted(set(round(x, 2) for x in ph + pl + fib + dyn
                      if np.isfinite(x)))
    # merge duplicates within 0.5%
    merged = []
    for x in lvls:
        if merged and abs(x - merged[-1]) / merged[-1] < 0.005:
            merged[-1] = round((x + merged[-1]) / 2, 2)
        else:
            merged.append(x)
    sup = [x for x in merged if x < c]
    res = [x for x in merged if x > c]
    return sup, res


def tf_up(f: pd.DataFrame) -> str:
    """Trend vote on any timeframe frame with Close/ema21/macd."""
    try:
        up = float(f["Close"].iloc[-1]) > float(f["ema21"].iloc[-1])
        if "macd" in f.columns and np.isfinite(float(f["macd"].iloc[-1])):
            up = up and float(f["macd"].iloc[-1]) > float(f["macd_sig"].iloc[-1])
        return "UP" if up else "DN"
    except Exception:
        return "?"


def analyze(t: str, d: pd.DataFrame, h1: pd.DataFrame,
            m15: pd.DataFrame) -> dict:
    """Every returned state carries direction + entry + S/R target + stop.
    Base-signal stops stay risk-based (validated); everything else reads off
    support/resistance + Fib + EMA/MA levels on daily and 1h."""
    i = len(d) - 1
    c = float(d["Close"].iloc[i])
    hi20 = float(d["hi20"].iloc[i])
    lo20 = float(d["lo20"].iloc[i])
    fib = fib_levels(hi20, lo20)
    # patterns may print on any of the last 3 bars (level zones, not lines)
    pats = {candle(d.iloc[j], d.iloc[j - 1]) for j in range(max(1, i - 2), i + 1)}
    rsi = float(d["rsi"].iloc[i])
    sup, res = swing_levels(d)
    votes = {"D": tf_up(d), "1h": tf_up(h1), "15m": tf_up(m15)}

    def objective(direction: int):
        """Nearest S/R objective + Fib extension in trade direction."""
        if direction == 1:
            tgt = min(res) if res else None
            ext = fib_targets(hi20, lo20, 1)
        else:
            tgt = max(sup) if sup else None
            ext = fib_targets(hi20, lo20, -1)
        return tgt, ext

    # 1. base signals first (never replaced; stops stay validated risk-based)
    for sname, dd, label in (("breakout", 1, "Long"), ("pullback", 1, "Long")):
        if signal_mask(d, sname, dd, 20.0)[i]:
            risk = risk_of(d, sname, dd, i)
            stop = c - risk
            tgt, ext = objective(1)
            tgt_txt = f"{tgt:.2f}" if tgt else f"+3R {(c + 3 * risk):.2f}"
            return {"state": label, "price": round(c, 2),
                    "target": f"{tgt_txt} (trail; fib-ext {ext['ext162']:.2f})",
                    "stop": round(stop, 2),
                    "note": f"{sname} trigger " + _votes(votes)}
    # MTF gate for new states: daily + 1h must agree with the call direction.
    up_ok = votes["D"] == "UP" and votes["1h"] == "UP"
    dn_ok = votes["D"] == "DN" and votes["1h"] == "DN"
    # 2. rejection (short bias): at 20d-high / Fib / resistance + weak candle
    levels = [("20d-high", hi20)] + [(f"fib{r}", v) for r, v in fib.items()]
    bear_pat = bool(pats & set(BEAR_PAT))
    for name, lvl in levels:
        if near(c, lvl, ZONE_TOL) and bear_pat and rsi > 55 and dn_ok:
            tgt, ext = objective(-1)
            tgt_txt = f"{tgt:.2f}" if tgt else f"{ext['ext162']:.2f}"
            rr = (c - float(tgt_txt)) / (lvl * 1.005 - c) \
                if lvl * 1.005 > c else 0
            if rr < 1.0:
                continue
            return {"state": "Possible upcoming Rejection",
                    "price": round(c, 2), "target": tgt_txt,
                    "stop": round(lvl * 1.005, 2),
                    "note": f"{sorted(pats & set(BEAR_PAT))[0]} at {name} "
                            f"{lvl:.2f} " + _votes(votes)}
    # 3. reversal (long bias): at 20d-low / Fib / support + hammer + washed out
    bull_pat = bool(pats & set(BULL_PAT))
    for name, lvl in [("20d-low", lo20)] + [(f"fib{r}", v)
                                            for r, v in fib.items()]:
        if near(c, lvl, ZONE_TOL) and bull_pat and rsi < 45 and up_ok:
            tgt, ext = objective(1)
            tgt_txt = f"{tgt:.2f}" if tgt else f"{ext['ext162']:.2f}"
            rr = (float(tgt_txt) - c) / (c - lvl * 0.995) \
                if c > lvl * 0.995 else 0
            if rr < 1.0:
                continue
            return {"state": "Possible upcoming Reversal",
                    "price": round(c, 2), "target": tgt_txt,
                    "stop": round(lvl * 0.995, 2),
                    "note": f"{sorted(pats & set(BULL_PAT))[0]} at {name} "
                            f"{lvl:.2f} " + _votes(votes)}
    return {"state": "-", "price": round(c, 2), "target": "-",
            "stop": "-", "note": "no qualified state " + _votes(votes)}


def _votes(votes: dict) -> str:
    return f"D/{votes.get('D', '?')} 1h/{votes.get('1h', '?')} " \
        f"15m/{votes.get('15m', '?')}"


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
            for _f in (h1, m15):
                if len(_f):
                    _f["ema21"] = _f["Close"].ewm(span=21,
                                                  adjust=False).mean()
                    _m = _f["Close"].ewm(span=12, adjust=False).mean() - \
                        _f["Close"].ewm(span=26, adjust=False).mean()
                    _f["macd"] = _m
                    _f["macd_sig"] = _m.ewm(span=9, adjust=False).mean()
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
