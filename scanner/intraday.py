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
from combos import add_features, pick  # noqa: E402
from indicators2 import add_extra  # noqa: E402
from scan import fmt_row  # noqa: E402  (shared alert text)
from simple_scan import add_ind as ema_ind  # noqa: E402
from simple_scan import load_pool, setup_row  # noqa: E402
from market_calendar import add_trading_days, trading_gap  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "scanner", "signal_log.csv")
WATCH = os.path.join(ROOT, "scanner", "watchlist.csv")
STATE = os.path.join(ROOT, "scanner", "intraday_state.json")
# no cap: fixed ETF pool, every open/watchlist name is evaluated
EMA_STATE = os.path.join(ROOT, "scanner", "ema_state.json")
EMA_RSI_ON = True         # live gate mirrors --rsi default
EMA_RSI_TARGET, EMA_RSI_TOL = 40.0, 2.0  # band = target..target+tol
EMA_WRSI_MIN = 55.0       # mirrors --wrsi-min (weekly regime gate)
EMA_ATR_MULT = 1.0        # proximity band mirrors --atr-mult
EMA_IGNORE_DAYS = 5       # ignores last this many TRADING days at most
EMA_ALERT_GAP = 3         # re-alert only after this many TRADING days


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
                  set(wl["ticker"].tolist() if len(wl) else []))
    if not syms:
        return 0
    px = yf.download(syms, period="1d", interval="15m", auto_adjust=True,
                     progress=False, threads=True, group_by="ticker")
    bars = {}
    for t in syms:
        try:
            h = flat(pick(px, t)).dropna(subset=["Close"])
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
    # 1c. EMA50+RSI40 scan over the spy50+qqq50 union, every run.
    # Alert only genuinely new triggers: suppressed if alerted within the
    # last EMA_ALERT_GAP trading days. Deep breaks (close < ema50 - 1 ATR)
    # park the ticker in ignored for EMA_IGNORE_DAYS or until a close back
    # over ema50, whichever is sooner.
    import yfinance as yf
    ema_new, ema_ign = 0, 0
    dst = {"alerted": {}, "ignored": {}}
    if os.path.exists(EMA_STATE):
        try:
            dst.update(json.load(open(EMA_STATE)))
        except Exception:
            pass
    dst.setdefault("alerted", {})
    dst.setdefault("ignored", {})
    today = dt.date.today()
    pool = sorted(set(load_pool("spy50")) | set(load_pool("qqq50")))
    try:
        dd = yf.download(pool, period="1y", interval="1d",
                         auto_adjust=True, progress=False, threads=True,
                         group_by="ticker")
    except Exception:
        dd = None
    if dd is not None:
        for t in pool:
            try:
                f = flat(pick(dd, t)).dropna(subset=["Close"])
                f.columns = [str(c).capitalize() for c in f.columns]
                # completed bars only: today's forming bar would let a
                # mid-day print alert a setup the daily chart never shows
                # (this matches the backtest, which scores closed bars).
                try:
                    forming = f.index[-1].date() >= today
                except Exception:
                    forming = False
                if forming:
                    f = f.iloc[:-1]
                if len(f) < 70:
                    continue
                prev_close = float(f["Close"].iloc[-2])
                today_open = float(f["Open"].iloc[-1])
                last = ema_ind(f).iloc[-1]
            except Exception:
                continue
            d50, atr, px = (float(last["ema50"]), float(last["atr"]),
                            float(last["Close"]))
            if px < d50 - atr:  # broken too deep: park it
                dst["ignored"][t] = {
                    "until": add_trading_days(
                        today, EMA_IGNORE_DAYS).isoformat()}
                ema_ign += 1
                continue
            if t in dst["ignored"]:
                try:
                    over = today > dt.date.fromisoformat(
                        dst["ignored"][t]["until"])
                except Exception:
                    over = True
                if px > d50 or over:  # recovered or expired: back in list
                    del dst["ignored"][t]
                else:
                    continue
            if not setup_row(last, EMA_RSI_ON, EMA_RSI_TARGET,
                             EMA_RSI_TOL, EMA_ATR_MULT, EMA_WRSI_MIN,
                             prev_close, today_open):
                continue
            prev = dst["alerted"].get(t)
            if prev is not None:
                try:
                    gap = trading_gap(dt.date.fromisoformat(prev), today)
                except Exception:
                    gap = EMA_ALERT_GAP + 1  # corrupt stamp: page, don't trap
                if gap <= EMA_ALERT_GAP:
                    continue
            dst["alerted"][t] = today.isoformat()
            ema_new += 1
            once(f"ema:{t}:{today.isoformat()}",
                 f'{t} LONG - EMA50+RSI40 pullback - '
                 f'Entry {px:.2f} - E50 {d50:.2f} - '
                 f'RSI {float(last["rsi"]):.0f} - '
                 f'WRSI {float(last["wrsi"]):.0f}', t)
        try:
            json.dump(dst, open(EMA_STATE, "w"))
        except Exception:
            pass
    print(f"intraday EMA: {ema_new} alerts, {ema_ign} ignored",
          flush=True)
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
