"""Intraday watcher (10-min cadence): approach/entry/stop texts + outcome logging.

Usage:
    python scanner/intraday.py --channels dry [--force]

Watches, on 15m bars:
  1. Watchlist names within 0.5% below their 20d high -> APPROACH text.
  2. Open paper signals (signal_log rows with empty outcome_R): price crossing
     entry -> ENTERED; stop touch -> STOPPED (-1R logged); +2R touch ->
     informational "up +2R, trailing".
One text per ticker+event per day (scanner/intraday_state.json).
Off-hours/holidays: exits silently (launchd fires 24/7 on a 600s interval).
Free-data caveat: Yahoo 15m prints lag ~15 min; fine for paper, not for HFT.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify import alert, load_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "scanner", "signal_log.csv")
WATCH = os.path.join(ROOT, "scanner", "watchlist.csv")
STATE = os.path.join(ROOT, "scanner", "intraday_state.json")
CAP = 40


def pt_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-7)))


def market_open(now=None) -> bool:
    now = now or pt_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 6 * 60 + 30 <= mins <= 13 * 60 + 5  # 6:30a-1:05p PT


def load_state() -> dict:
    try:
        with open(STATE) as f:
            s = json.load(f)
        return s if s.get("date") == dt.date.today().isoformat() else {}
    except Exception:
        return {}


def save_state(s: dict):
    s["date"] = dt.date.today().isoformat()
    with open(STATE, "w") as f:
        json.dump(s, f)


def flat(h):
    if isinstance(h.columns, pd.MultiIndex):
        h.columns = h.columns.get_level_values(0)
    h.columns = [c.capitalize() for c in h.columns]
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="dry")
    ap.add_argument("--force", action="store_true",
                    help="run even outside market hours (tests logic)")
    a = ap.parse_args()
    if not a.force and not market_open():
        return 0
    if not a.force:
        import subprocess
        r = subprocess.run(["python", "scanner/market_calendar.py"],
                           cwd=ROOT, capture_output=True)
        if r.returncode != 0:
            return 0  # holiday: silent

    import yfinance as yf
    log = pd.read_csv(LOG) if os.path.exists(LOG) else pd.DataFrame()
    open_tr = log[log["outcome_R"].isna()] if len(log) else log
    wl = pd.read_csv(WATCH) if os.path.exists(WATCH) else pd.DataFrame()
    if len(wl):
        wl = wl[wl["date"] == wl["date"].max()]
    syms = sorted(set(open_tr["ticker"].tolist() if len(open_tr) else []) |
                  set(wl["ticker"].tolist() if len(wl) else []))[:CAP]
    if not syms:
        return 0
    px = yf.download(syms, period="1d", interval="15m", auto_adjust=True,
                     progress=False, threads=True, group_by="ticker")
    bars = {}
    for t in syms:
        try:
            h = flat(px[t] if len(syms) > 1 else px).dropna(subset=["Close"])
            if len(h):
                bars[t] = h
        except Exception:
            continue

    cfg = load_config()
    chans = [c.strip() for c in a.channels.split(",")]
    state = load_state()
    fired = []

    def once(key, text, title):
        if key in state:
            return
        print(text, flush=True)
        for r in alert(text, chans, cfg, title=title):
            print("  ", r, flush=True)
        state[key] = True
        fired.append(key)

    # 1. approach / stand-down: watchlist names near 20d high (micro-level =
    #    highest high of available 15m bars). Each transition texts once.
    for _, w in wl.iterrows():
        t = w["ticker"]
        if t not in bars or w["dir"] != "UP":
            continue
        h = bars[t]
        lvl = float(h["High"].max())
        last = float(h["Close"].iloc[-1])
        gap = (lvl - last) / lvl
        if 0 < gap <= 0.005:
            once(f"approach:{t}",
                 f"APPROACH {t}: {last:.2f} within 0.5% of intraday high "
                 f"{lvl:.2f} — breakout trigger arming", t)
        elif gap > 0.01 and f"approach:{t}" in state:
            del state[f"approach:{t}"]
            once(f"standdown:{t}:{lvl:.2f}",
                 f"STAND DOWN {t}: faded to {last:.2f} "
                 f"({gap * 100:.1f}% under {lvl:.2f}) — trigger off", t)

    # 2. open paper signals
    for idx, r in open_tr.iterrows():
        t = r["ticker"]
        if t not in bars:
            continue
        last = float(bars[t]["Close"].iloc[-1])
        d = 1 if r["side"] == "long" else -1
        entry, stop = float(r["entry"]), float(r["stop"])
        if r["status"] in ("TRIGGERED", "CONFIRMED"):
            # pre-entry invalidation first: stop touched before entry fills
            dead = (last <= stop) if d == 1 else (last >= stop)
            if dead:
                log.at[idx, "status"] = "INVALIDATED"
                log.at[idx, "exit_reason"] = "pre-entry stop touch"
                once(f"invalid:{t}:{r['date']}",
                     f"INVALIDATED {t}: stop {stop} touched before entry "
                     f"(last {last:.2f}) — setup dead, no trade", t)
            elif (last >= entry) if d == 1 else (last <= entry):
                log.at[idx, "status"] = "ENTERED"
                log.at[idx, "entry"] = round(last, 2)
                once(f"entered:{t}:{r['date']}",
                     f"ENTERED {t} {r['side']} @ {last:.2f} "
                     f"(stop {stop}) — paper", t)
        elif r["status"] == "ENTERED":
            entry = float(r["entry"])
            risk = abs(entry - stop)
            rpnl = (last - entry) / risk * d
            if (last <= stop) if d == 1 else (last >= stop):
                log.at[idx, "outcome_R"] = -1.0
                log.at[idx, "exit_reason"] = "stop"
                once(f"stopped:{t}:{r['date']}",
                     f"STOPPED {t}: -1R @ {last:.2f} — logged", t)
            elif rpnl >= 2.0:
                once(f"up2r:{t}:{r['date']}",
                     f"{t} up +2R @ {last:.2f}, trailing per algo — paper", t)
    if fired:
        log.to_csv(LOG, index=False)
        save_state(state)
    print(f"intraday: {len(fired)} texts", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
