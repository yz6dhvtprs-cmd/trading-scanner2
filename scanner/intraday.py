"""Intraday watcher (15-min cadence): RPS signals + entry/stop texts + logging.

Usage:
    python scanner/intraday.py --channels dry [--force]

Each run, on 15m bars:
  1. RPS v2 generation on 90d daily + 200x 1h/15m per tracked ticker
     (same rps_live as backtester --algo RPS): new patterns -> TRIGGERED
     rows in signal_log + one text (deduped vs open rows).
  2. Open paper signals (signal_log rows with empty outcome_R): price crossing
     entry -> ENTERED; stop touch -> STOPPED (-1R logged); +2R touch ->
     informational "up +2R, trailing".
One text per ticker+event per day (scanner/intraday_state.json).
Off-hours/holidays: exits silently (launchd fires 24/7 on a 900s interval).
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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from notify import alert, load_config  # noqa: E402
from backtest_analyzer import rps_live  # noqa: E402  (same code as --algo RPS)
from combos import add_features  # noqa: E402
from indicators2 import add_extra  # noqa: E402
from scan import fmt_row  # noqa: E402  (shared alert text)

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

    # 1. (retired): approach/stand-down texts removed — %-from-high notices
    #    carry no direction. Proximity now only feeds the analyzer's states.
    # 1b. RPS v2 generation on 90d daily + 200x 1h/15m per tracked ticker
    # (90d ~= 64 trading bars: 60 calendar days ~= 42 bars, under R10's
    # 60-bar floor, so the window is sized in trading bars, not days).
    import yfinance as yf
    rps_new = 0
    try:
        dl = yf.download(syms, period="90d", interval="1d", auto_adjust=True,
                         progress=False, threads=True, group_by="ticker")
        h1r = yf.download(syms, period="1mo", interval="1h",
                          auto_adjust=True, progress=False, threads=True,
                          group_by="ticker")
        m15r = yf.download(syms, period="1mo", interval="15m",
                           auto_adjust=True, progress=False, threads=True,
                           group_by="ticker")
    except Exception:
        dl = None
    if dl is not None:
        open_keys = set()
        if len(log):
            oo = log[log["outcome_R"].isna()]
            for _, r in oo.iterrows():
                open_keys.add((r["ticker"], r["strategy"], str(r["notes"])))
        for t in syms:
            try:
                one = len(syms) == 1
                d = flat(dl if one else dl[t]).dropna(subset=["Close"])
                h1 = flat(h1r if one else h1r[t]).dropna(
                    subset=["Close"]).tail(200)
                m15 = flat(m15r if one else m15r[t]).dropna(
                    subset=["Close"]).tail(200)
                if len(d) < 60 or len(h1) < 40 or len(m15) < 40:
                    continue
                sigs = rps_live(add_extra(add_features(d)), h1, m15)
            except Exception:
                continue
            for s in sigs:
                st = s["state"]
                side = "long" if st in (
                    "Long", "Possible upcoming Reversal") else "short"
                risk = round(abs(float(s["price"]) - float(s["stop"])), 2)
                if risk <= 0:
                    continue
                key = f"rps-2step key={s.get('sig_key')}"
                if (t, "RPS", key) in open_keys:
                    continue
                open_keys.add((t, "RPS", key))
                entry, stop = float(s["price"]), float(s["stop"])
                new_idx = (log.index.max() + 1) if len(log) else 0
                log = pd.concat([log, pd.DataFrame([{
                    "date": dt.date.today().isoformat(), "ticker": t,
                    "strategy": "RPS", "side": side, "entry": entry,
                    "stop": stop, "target": "-", "grade": "B",
                    "timeframe": "intraday15", "status": "TRIGGERED",
                    "outcome_R": "", "exit_reason": "", "regime": "",
                    "news_flag": "", "notes": key}], index=[new_idx])])
                rps_new += 1
                once(f"rps:{t}:{s.get('sig_key')}",
                     fmt_row({"grade": "B", "ticker": t, "side": side,
                              "state": st, "strategy": "RPS",
                              "variant": "rps-2step", "entry": entry,
                              "stop": stop, "target": "-"}), t)
    print(f"intraday RPS: {rps_new} new setups", flush=True)
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
    if fired or rps_new:
        log.to_csv(LOG, index=False)
        save_state(state)
    print(f"intraday: {len(fired)} texts", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
