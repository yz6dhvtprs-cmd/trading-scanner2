"""Intraday watcher (15-min cadence): RPS signals + entry/stop texts + logging.

Usage:
    python ops/intraday.py --channels dry [--force]

Each run, on 15m bars:
  1. RPS v2 generation on 90d daily + 200x 1h/15m per tracked ticker
     (same rps_live as backtester --algo RPS): new patterns -> TRIGGERED
     rows in signal_log + one text (deduped vs open rows).
  1d. Weekly-box breakout watch (data/wbox_watch.json, seeded by the
     Friday scan): first print above a box top on session-paced RVOL >= 1.5
     and not more than 3% above it -> one batched text + a WBOX-BO paper
     row (stop = box mid); a print under the box low drops the watch.
  1e. Swing-funnel fills (data/funnel_watch.json, seeded by the premarket
     scan): today's setups book at the open within 1 ATR of the level and
     never through the setup stop -> FNL-* paper rows + one batched text.
  2. Open paper signals (signal_log rows with empty outcome_R): price crossing
     entry -> ENTERED; stop touch -> STOPPED (-1R logged); numeric target
     touch -> TARGET (+R logged); WBOX rows close at market after 8 weeks,
     FNL rows after their own time stop, FNL trail rows ratchet a
     chandelier stop; +2R touch -> informational "up +2R, trailing". WBOX
     and FNL events each batch into one text per run.
One text per ticker+event per day (data/intraday_state.json).
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
    os.path.abspath(__file__))), "algos"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from notify import alert, load_config  # noqa: E402
from backtest_analyzer import rps_live  # noqa: E402  (same code as --algo RPS)
from combos import add_features, pick  # noqa: E402
from indicators2 import add_extra  # noqa: E402
from scan import fmt_row  # noqa: E402  (shared alert text)
from simple_scan import add_ind as ema_ind  # noqa: E402
from simple_scan import batched_download  # noqa: E402
from screener import scan_pool  # noqa: E402 (spy+qqq UNION adds)
from simple_scan import drop_forming, load_pool  # noqa: E402
from simple_scan import rsi_window_pass, setup_row  # noqa: E402
from market_calendar import add_trading_days, market_open, pt_now  # noqa: E402
from market_calendar import trading_gap  # noqa: E402
from weekly_box import breakout_decisions, breakout_text  # noqa: E402
from weekly_box import load_watch, prune_watch, save_watch  # noqa: E402
from weekly_box import wbox_row  # noqa: E402
from funnel import TEXT_GRADES, fill_decisions, fills_text  # noqa: E402
from funnel import fnl_plan, fnl_row, trail_stop  # noqa: E402
from funnel import load_watch as load_funnel  # noqa: E402
from funnel import log_rows as log_funnel_rows  # noqa: E402
from funnel import save_watch as save_funnel  # noqa: E402
from execution import HEARTBEAT as EXEC_HB, INTENTS as EXEC_INTENTS  # noqa: E402
from execution import LEDGER as EXEC_LEDGER, alarms as exec_alarms  # noqa: E402
from execution import exit_intents, load_config as exec_config  # noqa: E402
from execution import load_json as exec_load, write_intents  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "data", "signal_log.csv")
WATCH = os.path.join(ROOT, "data", "watchlist.csv")
STATE = os.path.join(ROOT, "data", "intraday_state.json")
# no cap: fixed ETF pool, every open/watchlist name is evaluated
EMA_STATE = os.path.join(ROOT, "data", "ema_state.json")
RH_OVERRIDES = os.path.join(ROOT, "data", "rh_overrides.json")
EMA_RSI_ON = True         # live gate mirrors --rsi default
EMA_RSI_TARGET, EMA_RSI_MAX = 40.0, 45.0  # mirrors --rsi-target/max
EMA_WRSI_MIN = 55.0       # mirrors --wrsi-min (weekly regime gate)
EMA_ATR_MULT = 1.0        # proximity band mirrors --atr-mult
EMA_IGNORE_DAYS = 5       # ignores last this many TRADING days at most
EMA_ALERT_GAP = 3         # re-alert only after this many TRADING days
WBOX_TIME_DAYS = 56       # WBOX paper rows close at market after 8 weeks
TEXT_COLS = ("ticker", "strategy", "side", "target", "grade", "timeframe",
             "status", "exit_reason", "regime", "news_flag", "notes")


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


def read_log(path: str) -> pd.DataFrame:
    """signal_log with its free-text columns forced to object dtype. An
    all-empty column (exit_reason before the first stop) otherwise reads
    back as float64 and the first string write raises TypeError — that
    killed every 15-min run from 10/07 to 10/09 at the stop branch."""
    if not os.path.exists(path):
        return pd.DataFrame()
    log = pd.read_csv(path)
    for c in TEXT_COLS:
        if c in log.columns:
            log[c] = log[c].astype(object)
    if "outcome_R" in log.columns:
        log["outcome_R"] = pd.to_numeric(log["outcome_R"], errors="coerce")
    return log


def fnl_time_due(gap: int, time_bars: int, now) -> bool:
    """funnel.simulate exits a time-stopped trade at the CLOSE of bar
    signal+time_bars, i.e. trading_gap(fill, today) == time_bars - 1 late
    in that session. Earlier the tracker fired at gap >= time_bars (the
    next session's open — one bar late vs the backtest). Due when that
    session's last-hour clock has arrived, or any later session."""
    if gap >= time_bars:
        return True
    late = now.hour * 60 + now.minute >= 12 * 60 + 30
    return gap == time_bars - 1 and late


def session_elapsed(now=None) -> float:
    """Fraction of the 06:30-13:00 PT session elapsed, floored at 0.1 so
    an opening print is judged on paced volume, 1.0 after the close."""
    now = now or pt_now()
    mins = now.hour * 60 + now.minute - (6 * 60 + 30)
    if mins >= 390:
        return 1.0
    return max(mins / 390.0, 0.1)


def ema_batch_text(tickers: list) -> str:
    """One batched page for a 15-min run: header line plus one ticker per
    line. A run that pages 3 names sends 1 text, not 3."""
    return "D_RSI 40-45, WRSI>55, EMA50\n" + "\n".join(tickers)


def live_touch_decisions(facts: dict, quotes: dict, dst: dict,
                         today) -> tuple:
    """Live-touch pages for stage-1 names, one shared dedupe clock.

    facts[t]: ema50/atr/rsi/wrsi/shorts_below/prev_close from the last
    COMPLETED daily bar. quotes[t]: (live, day_open) off today's tape
    (Yahoo prints lag ~15 min, fine for a 1-ATR touch band). Returns
    (hits, parks, muted): fresh pages, new deep-break parks, and
    would-page-but-deduped names. A name with no live print changes
    nothing — no state writes here, the caller applies them.
    """
    hits, parks, muted = [], [], []
    for t, d in facts.items():
        q = quotes.get(t)
        if q is None:
            continue
        live, day_open = q
        e50, atr = d["ema50"], d["atr"]
        if live < e50 - atr:  # broke down on the live print: park it
            parks.append(t)
            continue
        if t in dst.get("ignored", {}):
            try:
                over = today > dt.date.fromisoformat(
                    dst["ignored"][t]["until"])
            except Exception:
                over = True
            if not (live > e50 or over):
                continue  # still parked
        if not (d["shorts_below"]
                and EMA_RSI_TARGET <= d["rsi"] <= EMA_RSI_MAX
                and d["wrsi"] > EMA_WRSI_MIN
                and d["prev_close"] > e50 and day_open > e50
                and abs(live - e50) <= atr):
            continue
        prev = dst.get("alerted", {}).get(t)
        if prev is not None:
            try:
                gap = trading_gap(dt.date.fromisoformat(prev), today)
            except Exception:
                gap = EMA_ALERT_GAP + 1  # corrupt stamp: page, don't trap
            if gap <= EMA_ALERT_GAP:
                muted.append(t)
                continue
        hits.append(t)
    return hits, parks, muted


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
        r = subprocess.run(["python", "algos/market_calendar.py"],
                           cwd=ROOT, capture_output=True)
        if r.returncode != 0:
            return 0  # holiday: silent

    import yfinance as yf
    today = dt.date.today()
    log = read_log(LOG)
    open_tr = log[log["outcome_R"].isna()] if len(log) else log
    wl = pd.read_csv(WATCH) if os.path.exists(WATCH) else pd.DataFrame()
    if len(wl):
        wl = wl[wl["date"] == wl["date"].max()]
    strat = (open_tr["strategy"].astype(str) if len(open_tr)
             else pd.Series(dtype=str))
    managed = strat.str.match(r"^(WBOX|FNL)")  # box + funnel paper rows
    rps_open = open_tr[~managed] if len(open_tr) else open_tr
    wb_open = open_tr[managed] if len(open_tr) else open_tr
    syms = sorted(set(rps_open["ticker"].tolist() if len(rps_open) else [])
                  | set(wl["ticker"].tolist() if len(wl) else []))
    # box and funnel names ride one light daily fetch (stages 1d/1e
    # below), never the 15m/RPS downloads here — RPS keeps its own pool
    watch = prune_watch(load_watch(), today)
    fst = load_funnel()
    fn_due = [p for p in fst.get("pending", [])
              if p.get("fill_date") == today.isoformat()
              and not p.get("done") and not p.get("paused")]
    # the executor's open option positions ride the same daily fetch so
    # their EXIT intents can be written even after the paper row closed
    ex_ledger = exec_load(EXEC_LEDGER, {})
    ex_open = [p["ticker"] for p in (ex_ledger.get("positions") or {}).values()
               if p.get("status") == "open"]
    wb_syms = sorted(set(watch) | set(p["ticker"] for p in fn_due) | set(ex_open)
                     | set(wb_open["ticker"].tolist() if len(wb_open) else []))
    if not syms and not wb_syms:
        return 0
    bars = {}
    if syms:
        px = yf.download(syms, period="1d", interval="15m",
                         auto_adjust=True, progress=False, threads=True,
                         group_by="ticker")
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

    def once(key, text, title, audience="owner"):
        # subscribers get new LONG setups only; paper tracking, fills,
        # stops and watch updates go to the owner
        if key in state:
            return
        print(text, flush=True)
        for r in alert(text, chans, cfg, title=title, audience=audience):
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
    dl = None
    if syms:  # RPS keeps its own pool: box names never enter here
        try:
            dl = yf.download(syms, period="90d", interval="1d",
                             auto_adjust=True, progress=False, threads=True,
                             group_by="ticker")
            h1r = yf.download(syms, period="1mo", interval="1h",
                              auto_adjust=True, progress=False,
                              threads=True, group_by="ticker")
            m15r = yf.download(syms, period="1mo", interval="15m",
                               auto_adjust=True, progress=False,
                               threads=True, group_by="ticker")
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
                              "stop": stop, "target": "-"}), t,
                     "all" if side == "long" else "owner")
    print(f"intraday RPS: {rps_new} new setups", flush=True)
    # 1c. EMA50+RSI40 scan over the full spy+qqq union, every run.
    # Alert only genuinely new triggers: suppressed if alerted within the
    # last EMA_ALERT_GAP trading days. Deep breaks (close < ema50 - 1 ATR)
    # park the ticker in ignored for EMA_IGNORE_DAYS or until a close back
    # over ema50, whichever is sooner.
    from daily_cache import load as load_bars, sync_pool  # noqa: E402
    ema_new, ema_ign = 0, 0
    ema_hits: list = []  # paged this run: one batched text, not one per name
    dst = {"alerted": {}, "ignored": {}}
    if os.path.exists(EMA_STATE):
        try:
            dst.update(json.load(open(EMA_STATE)))
        except Exception:
            pass
    dst.setdefault("alerted", {})
    dst.setdefault("ignored", {})
    today = dt.date.today()
    pool = sorted(scan_pool())
    # daily bars are local now: sync_pool refreshes the rotating cache once
    # a day (first run) and every later 15-min run reads off disk — zero
    # daily downloads intraday. Completed bars never change mid-session.
    try:
        sync = sync_pool(pool, today=today)
    except Exception:
        sync = {"refreshed": False, "missing": list(pool)}
    # stage 1: cached closes for the full pool; only names whose last
    # COMPLETED bar sits in the RSI band go on to full indicators
    # (AND-filter: a name outside the band cannot page, so filtering
    # loses nothing). The same cached frame feeds stage 2.
    cand, frames, facts = [], {}, {}
    for t in pool:
        try:
            f = drop_forming(load_bars(t).dropna(subset=["Close"]), today)
            if len(f) < 20:
                continue
            if rsi_window_pass(f["Close"], 1, EMA_RSI_TARGET,
                               EMA_RSI_MAX):
                cand.append(t)
                frames[t] = f
        except Exception:
            continue
    if cand:
        for t in cand:
            try:
                # completed bars only: today's forming bar would let a
                # mid-day print alert a setup the daily chart never shows
                # (this matches the backtest, which scores closed bars).
                f = frames[t]
                if len(f) < 70:
                    continue
                prev_close = float(f["Close"].iloc[-2])
                today_open = float(f["Open"].iloc[-1])
                last = ema_ind(f).iloc[-1]
                facts[t] = {
                    "ema50": float(last["ema50"]),
                    "atr": float(last["atr"]),
                    "rsi": float(last["rsi"]),
                    "wrsi": float(last["wrsi"]),
                    "shorts_below": bool(
                        float(last["Close"]) < float(last["ema8"])
                        and float(last["Close"]) < float(last["ema13"])
                        and float(last["Close"]) < float(last["ema21"])),
                    "prev_close": prev_close}
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
                             EMA_RSI_MAX, EMA_ATR_MULT, EMA_WRSI_MIN,
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
            ema_hits.append(t)
    # remote-price swap: narrowed names with fresh RH server values use
    # the full remote set (ema/rsi/atr/wrsi) in the live-touch gate below;
    # everyone else stays local. A stale/missing overrides file leaves
    # facts untouched, so this degrades to the old behavior by default.
    n_rh = 0
    try:
        from rh_mcp import apply_remote, load_overrides  # noqa: E402
        facts = apply_remote(
            facts, load_overrides(RH_OVERRIDES, today), today)
        n_rh = sum(1 for d in facts.values() if d.get("_src") == "rh")
    except Exception:
        pass
    # live-touch pass: same gates, but price-vs-line on the live print
    # instead of the last close — a name drifting into its EMA50 at 10am
    # pages at 10:15, not tomorrow. Daily-paged names mute here via the
    # shared stamp (gap 0), so one clock governs both passes.
    ema_live, ema_muted = 0, 0
    live_pool = [t for t in facts if dst["alerted"].get(t) !=
                 today.isoformat()]
    if live_pool:
        try:
            tape = batched_download(live_pool, period="1d", interval="1m",
                                    chunk=50)
        except Exception:
            tape = None
        quotes = {}
        if tape is not None:
            for t in live_pool:
                try:
                    m = tape[t] if isinstance(
                        tape.columns, pd.MultiIndex) else tape
                    m = m.dropna(subset=["Close"])
                    if len(m):
                        quotes[t] = (float(m["Close"].iloc[-1]),
                                     float(m["Open"].iloc[0]))
                except Exception:
                    continue
        hits, parks, muted = live_touch_decisions(facts, quotes, dst,
                                                  today)
        for t in parks:
            dst["ignored"][t] = {
                "until": add_trading_days(
                    today, EMA_IGNORE_DAYS).isoformat()}
            ema_ign += 1
        for t in hits:
            dst["alerted"][t] = today.isoformat()
            if t in dst["ignored"]:
                del dst["ignored"][t]  # recovered live: park lifted
            ema_new += 1
            ema_live += 1
            ema_hits.append(t)
        ema_muted = len(muted)
    try:
        json.dump(dst, open(EMA_STATE, "w"))
    except Exception:
        pass
    if ema_hits:
        slot = pt_now().strftime("%H:%M")
        once(f"ema-batch:{today.isoformat()}:{slot}",
             ema_batch_text(sorted(ema_hits)), "EMA", "all")
    src = "refreshed" if sync.get("refreshed") else "local"
    print(f"intraday EMA: {ema_new} alerts ({ema_live} live-touch), "
          f"{ema_ign} ignored, {ema_muted} muted "
          f"(stage1 {len(cand)}/{len(pool)}, {n_rh} rh-remote, "
          f"daily {src})", flush=True)
    # 1d. weekly-box breakout watch: every Friday box (all tiers) sits in
    # data/wbox_watch.json for four weeks; the first print above the box
    # top on session-paced RVOL >= 1.5 pages (never more than 3% above
    # it) and books a WBOX-BO paper row with the stop at the box mid. A
    # print under the box low drops the watch. One light daily fetch
    # covers the watch plus the marks for the open WBOX rows.
    wb_prints, wb_avg = {}, {}
    if wb_syms:
        try:
            px1 = batched_download(wb_syms, period="1d", interval="1d")
        except Exception:
            px1 = None
        for t in wb_syms:
            if px1 is not None:
                try:
                    f1 = px1[t] if isinstance(
                        px1.columns, pd.MultiIndex) else px1
                    f1 = f1.dropna(subset=["Close"])
                    if len(f1):
                        wb_prints[t] = (float(f1["Close"].iloc[-1]),
                                        float(f1["Open"].iloc[-1]),
                                        float(f1["Volume"].iloc[-1]))
                except Exception:
                    pass
            try:
                h = drop_forming(load_bars(t).dropna(subset=["Close"]),
                                 today)
                if len(h) >= 20:
                    wb_avg[t] = float(h["Volume"].tail(20).mean())
            except Exception:
                pass
    pages, broke, chased = breakout_decisions(
        watch, wb_prints, wb_avg, today, session_elapsed())
    dirty = False
    for p in pages:
        t = p["ticker"]
        watch[t]["paged"] = today.isoformat()
        new_idx = (log.index.max() + 1) if len(log) else 0
        log = pd.concat([log, pd.DataFrame([wbox_row(
            t, p["lower"], p["upper"], p["last"], today, p["grade"],
            strategy="WBOX-BO",
            note=f"tier={p['tier']} rvol={p['rvol']:.1f}")],
            index=[new_idx])])
        dirty = True
    for t in broke:
        watch[t]["done"] = f"broke down {today.isoformat()}"
    for t in chased:
        watch[t]["done"] = f"chased {today.isoformat()}"
    if pages or broke or chased:
        save_watch(watch)
    if pages:
        once(f"wbox-bo:{today.isoformat()}:{pt_now().strftime('%H:%M')}",
             breakout_text(pages), "WBOX", "all")
    print(f"intraday WBOX: {len(pages)} breakouts, {len(broke)} broke "
          f"down, {len(chased)} chased, {len(watch)} on watch, "
          f"{len(wb_open)} open paper rows", flush=True)
    # 1e. funnel fills: the premarket scan's setups fill at today's open
    # within 1 ATR of the level and never through the setup stop. B+ and
    # up text, B books silently, C (paused) never reaches here.
    fills, invalid = fill_decisions(fn_due, wb_prints, today)
    fn_rows = [fnl_row(f, today) for f in fills]
    n_booked = log_funnel_rows(fn_rows) if fn_rows else 0
    keys = {(f["ticker"], f["family"], f["side"]): "filled" for f in fills}
    keys.update({(p["ticker"], p["family"], p["side"]): why
                 for p, why in invalid})
    if keys:
        for p in fst.get("pending", []):
            k = (p["ticker"], p["family"], p["side"])
            if k in keys and p.get("fill_date") == today.isoformat():
                p["done"] = keys[k]
        save_funnel(fst)
    tf = [f for f in fills if f["grade"] in TEXT_GRADES]
    ti = [(p, w) for p, w in invalid if p["grade"] in TEXT_GRADES]
    if tf or ti:
        once(f"fnl-fill:{today.isoformat()}:{pt_now().strftime('%H:%M')}",
             fills_text(tf, ti), "FNL")
    print(f"intraday FNL: {len(fn_due)} due, {len(fills)} filled "
          f"({n_booked} booked), {len(invalid)} invalid", flush=True)
    # 1f. execution layer (docs/strategies/EXECUTION.md): EXIT intents for the
    # executor session's open option positions off this run's print (stop /
    # target / time / dte, same ids the session derives itself), and an
    # owner-only alarm when its heartbeat is stale with positions or intents
    # pending. The loop never calls the broker.
    try:
        now_pt = pt_now().replace(tzinfo=None)
        ecfg = exec_config()
        xs = exit_intents(ex_ledger, wb_prints, today, now_pt)
        if xs:
            write_intents([], xs, today)
        pend = sum(1 for i in exec_load(EXEC_INTENTS, {}).get("intents", [])
                   if i.get("kind") == "ENTRY" and not i.get("done"))
        for msg in exec_alarms(ex_ledger, exec_load(EXEC_HB, {}), pend, now_pt,
                               ecfg):
            key = f"exec-alarm:{today.isoformat()}:{now_pt.strftime('%H')}"
            if key not in state:
                state[key] = True
                dirty = True
                print(msg, flush=True)
                if ecfg.get("notify_to"):
                    for r in alert(msg, chans, cfg, title="EXEC",
                                   audience="owner"):
                        print("  ", r, flush=True)
        print(f"intraday EXEC: {len(xs)} exit intents, {len(ex_open)} open "
              f"option positions, {pend} entry intents pending", flush=True)
    except Exception as e:  # the paper tracker never waits on the exec layer
        print(f"intraday EXEC: skipped ({type(e).__name__}: {e})", flush=True)
    # 2. open paper signals: RPS rows mark off the 15m tape, WBOX/FNL rows
    # off the daily print; a numeric target closes a row as a win (RPS
    # rows carry "-" and trail per algo); WBOX rows close at market after
    # WBOX_TIME_DAYS, FNL rows after their own time stop (trading days)
    # and FNL trail rows ratchet a chandelier stop off the daily cache.
    # WBOX and FNL events each batch into one text per run.
    wb_events: list = []
    fn_events: list = []
    for idx, r in open_tr.iterrows():
        t = r["ticker"]
        if t in bars:
            last = float(bars[t]["Close"].iloc[-1])
        elif t in wb_prints:
            last = wb_prints[t][0]
        else:
            continue
        d = 1 if r["side"] == "long" else -1
        entry, stop = float(r["entry"]), float(r["stop"])
        is_wb = str(r["strategy"]).startswith("WBOX")
        is_fn = str(r["strategy"]).startswith("FNL")
        plan = fnl_plan(r["notes"]) if is_fn else {"trail": None,
                                                   "time": None}
        if is_fn and plan["trail"] and r["status"] == "ENTERED":
            # chandelier off completed bars since entry plus today's print
            try:
                h = drop_forming(load_bars(t).dropna(subset=["Close"]),
                                 today)
                since = h[h.index.date > dt.date.fromisoformat(
                    str(r["date"]))]
                ext = (max(float(since["High"].max()), last) if d == 1
                       else min(float(since["Low"].min()), last)) \
                    if len(since) else last
                atr_now = float(ema_ind(h)["atr"].iloc[-1])
                new_stop = round(trail_stop(r["side"], stop, ext, atr_now,
                                            plan["trail"]), 2)
            except Exception:
                new_stop = stop
            if (new_stop > stop) if d == 1 else (new_stop < stop):
                log.at[idx, "stop"] = new_stop
                stop = new_stop
                dirty = True
        if r["status"] in ("TRIGGERED", "CONFIRMED"):
            # pre-entry invalidation first: stop touched before entry fills
            dead = (last <= stop) if d == 1 else (last >= stop)
            if dead:
                log.at[idx, "status"] = "INVALIDATED"
                log.at[idx, "exit_reason"] = "pre-entry stop touch"
                dirty = True
                once(f"invalid:{t}:{r['date']}",
                     f"INVALIDATED {t}: stop {stop} touched before entry "
                     f"(last {last:.2f}) — setup dead, no trade", t)
            elif (last >= entry) if d == 1 else (last <= entry):
                log.at[idx, "status"] = "ENTERED"
                log.at[idx, "entry"] = round(last, 2)
                dirty = True
                once(f"entered:{t}:{r['date']}",
                     f"ENTERED {t} {r['side']} @ {last:.2f} "
                     f"(stop {stop}) — paper", t)
        elif r["status"] == "ENTERED":
            entry = float(r["entry"])
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            rpnl = (last - entry) / risk * d
            try:
                tgt = float(r["target"])
            except (TypeError, ValueError):
                tgt = None
            try:
                age = (today - dt.date.fromisoformat(str(r["date"]))).days
            except ValueError:
                age = 0
            events = fn_events if is_fn else wb_events
            if (last <= stop) if d == 1 else (last >= stop):
                # a ratcheted trail stop books the locked-in R, not -1
                got = round((last - entry) / risk * d, 2) if (
                    is_fn and plan["trail"]) else -1.0
                log.at[idx, "outcome_R"] = got
                log.at[idx, "exit_reason"] = "trail" if got != -1.0 else "stop"
                dirty = True
                if is_wb or is_fn:
                    events.append(f"{t} {'trail' if got != -1.0 else 'stopped'}"
                                  f" {got:+.1f}R @ {last:.2f}")
                else:
                    once(f"stopped:{t}:{r['date']}",
                         f"STOPPED {t}: -1R @ {last:.2f} — logged", t)
            elif tgt is not None and ((last >= tgt) if d == 1
                                      else (last <= tgt)):
                won = round((tgt - entry) / risk * d, 2)
                log.at[idx, "outcome_R"] = won
                log.at[idx, "exit_reason"] = "target"
                dirty = True
                if is_wb or is_fn:
                    events.append(f"{t} target +{won:.1f}R @ {last:.2f}")
                else:
                    once(f"target:{t}:{r['date']}",
                         f"TARGET {t}: +{won:.1f}R @ {last:.2f} — logged",
                         t)
            elif (is_wb and age >= WBOX_TIME_DAYS) or (
                    is_fn and plan["time"] and fnl_time_due(
                        trading_gap(dt.date.fromisoformat(str(r["date"])),
                                    today), plan["time"], pt_now())):
                log.at[idx, "outcome_R"] = round(rpnl, 2)
                log.at[idx, "exit_reason"] = "time"
                dirty = True
                events.append(f"{t} time stop {rpnl:+.1f}R @ {last:.2f}")
            elif rpnl >= 2.0:
                key = f"up2r:{t}:{r['date']}"
                if is_wb or is_fn:
                    if key not in state:
                        state[key] = True
                        dirty = True
                        events.append(f"{t} up +2R @ {last:.2f}")
                else:
                    once(key, f"{t} up +2R @ {last:.2f}, trailing per "
                         f"algo — paper", t)
    if wb_events:
        once(f"wbox-track:{today.isoformat()}:"
             f"{pt_now().strftime('%H:%M')}",
             "WBOX paper:\n" + "\n".join(wb_events), "WBOX")
    if fn_events:
        once(f"fnl-track:{today.isoformat()}:"
             f"{pt_now().strftime('%H:%M')}",
             "FNL paper:\n" + "\n".join(fn_events), "FNL")
    if fired or rps_new or dirty:
        log.to_csv(LOG, index=False)
        save_state(state)
    print(f"intraday: {len(fired)} texts", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
