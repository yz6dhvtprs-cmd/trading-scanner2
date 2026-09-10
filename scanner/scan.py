"""Live scan: fresh bars -> algo.json signals -> free alerts + signal log.

Usage:
    source .venv/bin/activate
    python scanner/scan.py                       # nightly-style scan, top-50
    python scanner/scan.py --pool sp500          # full pool (slower)
    python scanner/scan.py --mode premarket      # re-check TRIGGERED rows only:
                                                 # pulls intraday, applies the
                                                 # gap/invalidation gate, alerts
                                                 # CONFIRMED only
    python scanner/scan.py --channels dry        # print only, send nothing

Every alert appends a row to scanner/signal_log.csv (feeds Saturday review).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from combos import add_features, level_of, risk_of, signal_mask  # noqa: E402
from indicators2 import add_extra, alignment  # noqa: E402
from notify import alert, load_config  # noqa: E402
from backtest_analyzer import rps_live  # noqa: E402  (same RPS code as --algo RPS)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "scanner", "signal_log.csv")


def download(tickers: list, period: str, interval: str):
    import yfinance as yf
    px = yf.download(tickers, period=period, interval=interval,
                     auto_adjust=True, progress=False, threads=True,
                     group_by="ticker")
    frames = {}
    for t in tickers:
        try:
            h = px[t] if len(tickers) > 1 else px
            h = h.dropna(subset=["Close"])
            h.columns = [c.capitalize() for c in h.columns]
            if len(h) > 250:
                frames[t] = add_extra(add_features(h))
        except Exception:
            continue
    return frames


def stack_dir(f: pd.DataFrame) -> str:
    """Light-gate vote on one timeframe: price vs EMA8/21/50 stack."""
    try:
        c, e8, e21, e50 = (float(f["Close"].iloc[-1]), float(f["ema8"].iloc[-1]),
                           float(f["ema21"].iloc[-1]), float(f["ema50"].iloc[-1]))
    except Exception:
        return "?"
    if c > e8 > e21 > e50:
        return "UP"
    if c < e8 < e21 < e50:
        return "DN"
    return "-"


def intraday_votes(tickers: list, cfg_channels: bool = True) -> dict:
    """D/1h/15m alignment for a capped ticker list (live-only; 15m history
    is too short to backtest, so these votes gate alerts, not grades)."""
    import yfinance as yf
    votes = {}
    for t in tickers[:40]:
        try:
            v = {"D": stack_dir(daily_frames[t])} if t in daily_frames else {}

            def flat(h):
                if isinstance(h.columns, pd.MultiIndex):
                    h.columns = h.columns.get_level_values(0)
                h.columns = [c.capitalize() for c in h.columns]
                return h

            h1 = flat(yf.download(t, period="1mo", interval="1h",
                                  auto_adjust=True, progress=False))
            for n in (8, 21, 50):
                h1[f"ema{n}"] = h1["Close"].ewm(span=n, adjust=False).mean()
            v["1h"] = stack_dir(h1)
            m15 = flat(yf.download(t, period="1mo", interval="15m",
                                   auto_adjust=True, progress=False))
            for n in (8, 21, 50):
                m15[f"ema{n}"] = m15["Close"].ewm(span=n, adjust=False).mean()
            v["15m"] = stack_dir(m15)
            votes[t] = v
        except Exception:
            continue
    return votes


daily_frames: dict = {}


def scan_nightly(frames: dict, algo: dict) -> list:
    """Score last completed bar per ticker. Returns alert dicts."""
    out = []
    for t, f in frames.items():
        i = len(f) - 1
        for v in algo["variants"]:
            d = 1 if v["side"] == "long" else -1
            if not signal_mask(f, v["strategy"], d,
                               v["filters"]["adx_min"],
                               v["filters"].get("rvol_min", 2.0))[i]:
                continue
            entry = float(f["Close"].iloc[i])
            risk = risk_of(f, v["strategy"], d, i)
            trail = v["exit"].get("trail_atr")
            tgt_txt = "trail, no fixed target" if trail else \
                f'{entry + float(v["exit"].get("target_r", 2)) * risk * d:.2f}'
            out.append({"ticker": t, "variant": v["name"],
                        "grade": v["grade"], "strategy": v["strategy"],
                        "side": v["side"], "entry": round(entry, 2),
                        "stop": round(entry - risk * d, 2),
                        "target": tgt_txt, "risk": round(risk, 2),
                        "adx": round(float(f["adx"].iloc[i]), 1)})
    return out


def download_tf(tickers: list, interval: str) -> dict:
    """Batched 1mo intraday frames (RPS step-1/2 RSI needs 15m + 1h)."""
    import yfinance as yf
    try:
        px = yf.download(tickers, period="1mo", interval=interval,
                         auto_adjust=True, progress=False, threads=True,
                         group_by="ticker")
    except Exception:
        return {}
    frames = {}
    for t in tickers:
        try:
            h = px[t] if len(tickers) > 1 else px
            h = h.dropna(subset=["Close"])
            h.columns = [c.capitalize() for c in h.columns]
            if len(h) > 40:
                frames[t] = h
        except Exception:
            continue
    return frames


def scan_rps(frames: dict, h1_frames: dict, m15_frames: dict,
             open_keys: set) -> list:
    """RPS two-step on the last bar per ticker. Alert dicts in the rows
    schema (strategy RPS, grade B). Skips patterns with an identical open
    row already in signal_log (intraday.py is tracking those)."""
    out = []
    for t, f in frames.items():
        if t not in h1_frames or t not in m15_frames:
            continue
        try:
            sigs = rps_live(f, h1_frames[t], m15_frames[t])
        except Exception:
            continue
        for s in sigs:
            side = "long" if s["state"] in (
                "Long", "Possible upcoming Reversal") else "short"
            risk = round(abs(float(s["price"]) - float(s["stop"])), 2)
            if risk <= 0:
                continue
            key = f"rps-2step key={s.get('sig_key')}"
            if (t, "RPS", key) in open_keys:
                continue
            out.append({"ticker": t, "variant": "rps-2step", "grade": "B",
                        "strategy": "RPS", "side": side,
                        "state": s["state"],
                        "entry": float(s["price"]), "stop": float(s["stop"]),
                        "target": "-", "risk": risk, "notes": key,
                        "confirms": "RPS"})
    return out


def fmt_row(r: dict) -> str:
    """One alert/print line per setup. RPS rows carry the reversal state;
    OLD rows carry strategy (variant). No TRIGGERED/CONFIRMED/confirms
    segments — those stay internal to the row/log lifecycle."""
    desc = r.get("state") or \
        f'{r["strategy"]} ({r["variant"]})'
    return (f'[{r["grade"]}] - {r["ticker"]} {r["side"].upper()} - {desc} '
            f'- Entry {r["entry"]} - SL {r["stop"]} - Tgt {r["target"]} - '
            f'opt: {"call" if r["side"] == "long" else "put"} debit spread')


def premarket_gate(row: dict, frames_detail) -> str:
    """Re-check one TRIGGERED row against the latest intraday print.
    Returns CONFIRMED / INVALIDATED(reason)."""
    t = row["ticker"]
    try:
        last = float(frames_detail["Close"].iloc[-1])
    except Exception:
        return "CONFIRMED (no intraday data)"
    entry, stop = row["entry"], row["stop"]
    buf = 0.25 * row["risk"]
    d = 1 if row["side"] == "long" else -1
    if d == 1:
        if last <= stop:
            return f"INVALIDATED (gapped through stop {stop})"
        if last > entry + buf and row["variant"] == "primary":
            return "INVALIDATED (chased beyond entry buffer)"
    else:
        if last >= stop:
            return f"INVALIDATED (gapped through stop {stop})"
    row["entry"] = round(last, 2)
    return "CONFIRMED"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="top50", choices=["top50", "sp500"])
    ap.add_argument("--mode", default="nightly", choices=["nightly", "premarket"])
    ap.add_argument("--channels", default="dry",
                    help="comma-separated: telegram,mac,imessage,shortcut:NAME,dry")
    ap.add_argument("--intraday", action="store_true",
                    help="add live 1h/15m stack votes for watchlist+triggered")
    ap.add_argument("--align-min", type=float, default=0.78,
                    help="watchlist light-gate: min 9-MA agreement fraction")
    ap.add_argument("--rps", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="RPS two-step reversal stage (same code as "
                         "backtester --algo RPS)")
    a = ap.parse_args()
    with open(os.path.join(ROOT, "algo.json")) as f:
        algo = json.load(f)

    if a.pool == "top50":
        tickers = pd.read_csv(os.path.join(
            ROOT, "universe", "universe_live.csv"))["ticker"].tolist()
    else:
        tickers = pd.read_csv(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
            "/main/data/constituents.csv")["Symbol"].str.replace(
                ".", "-", regex=False).tolist()
        tickers = [t for t in tickers if t != "META"]
    print(f"{len(tickers)} tickers, downloading 1y daily...", flush=True)
    frames = download(tickers, "1y", "1d")
    print(f"{len(frames)} frames", flush=True)
    daily_frames.update(frames)

    # Stage 0 light gate: 9-MA alignment -> WATCHLIST (potential trades)
    watch = []
    for t, f in frames.items():
        al = alignment(f).iloc[-1]
        if al["bull_frac"] >= a.align_min:
            watch.append({"ticker": t, "dir": "UP",
                          "agree": round(float(al["bull_frac"]), 2),
                          "adx": round(float(f["adx"].iloc[-1]), 1)})
        elif al["bear_frac"] >= a.align_min:
            watch.append({"ticker": t, "dir": "DN",
                          "agree": round(float(al["bear_frac"]), 2),
                          "adx": round(float(f["adx"].iloc[-1]), 1)})
    print(f"watchlist: {len(watch)} names", flush=True)

    rows = scan_nightly(frames, algo)

    if a.rps and frames:
        syms = sorted(frames)
        print(f"RPS two-step on {len(syms)} names (1h+15m) ...", flush=True)
        h1f = download_tf(syms, "1h")
        m15f = download_tf(syms, "15m")
        open_keys = set()
        if os.path.exists(LOG):
            old = pd.read_csv(LOG)
            o = old[old["outcome_R"].isna()] if len(old) else old
            for _, r in o.iterrows():
                open_keys.add((r["ticker"], r["strategy"], str(r["notes"])))
        rps_rows = scan_rps(frames, h1f, m15f, open_keys)
        print(f"RPS: {len(rps_rows)} setups", flush=True)
        rows += rps_rows
    # attach confirm flags to triggered rows (informational; base untouched)
    for r in rows:
        f = frames[r["ticker"]]
        i = len(f) - 1
        d = 1 if r["side"] == "long" else -1
        r["confirms"] = "".join([
            "M" if bool(f[f"mtf_ok_{'long' if d == 1 else 'short'}"].iloc[i]) else "",
            "R" if bool(f[f"rsi_range_ok_{'long' if d == 1 else 'short'}"].iloc[i]) else "",
            "D" if bool(f[f"hidiv_{'long' if d == 1 else 'short'}"].iloc[i]) else "",
            "X" if bool(f[f"macd_ok_{'long' if d == 1 else 'short'}"].iloc[i]) else "",
        ]) or "-"

    votes = {}
    if a.intraday and (watch or rows):
        syms = sorted({w["ticker"] for w in watch} |
                      {r["ticker"] for r in rows})
        print(f"intraday votes for {len(syms)} names...", flush=True)
        votes = intraday_votes(syms)
    if a.mode == "premarket" and rows:
        syms = sorted({r["ticker"] for r in rows})
        print(f"pulling intraday for {len(syms)} triggered...", flush=True)
        intra = download(syms, "1d", "1m")
        for r in rows:
            r["status"] = premarket_gate(
                r, intra.get(r["ticker"], pd.DataFrame()))
        rows = [r for r in rows if r["status"].startswith("CONFIRMED")]
    else:
        for r in rows:
            r["status"] = "TRIGGERED"

    import datetime as dt
    cfg = load_config()
    chans = [c.strip() for c in a.channels.split(",")]

    def tag(sym):
        v = votes.get(sym)
        return f" D/{v.get('D','?')} 1h/{v.get('1h','?')} 15m/{v.get('15m','?')}" \
            if v else ""

    # Stage 0 digest: change-only. Full list always logged; the text carries
    # only NEW/DROPPED names vs the previous session (first run sends full).
    WLPATH = os.path.join(ROOT, "scanner", "watchlist.csv")
    prev = set()
    if os.path.exists(WLPATH):
        old = pd.read_csv(WLPATH)
        if len(old):
            prev = set(old[old["date"] == old["date"].max()]["ticker"])
    if watch:
        pd.DataFrame([{"date": dt.date.today().isoformat(), **w}
                      for w in watch]).to_csv(
            WLPATH, mode="a", header=not os.path.exists(WLPATH), index=False)
        cur = {w["ticker"] for w in watch}
        new, dropped = sorted(cur - prev), sorted(prev - cur)
        if prev and not new and not dropped:
            print(f"WATCH unchanged ({len(cur)} names, logged, no text)",
                  flush=True)
        else:
            wl = sorted(watch, key=lambda w: -w["adx"])
            if not prev:
                body = "; ".join(f"{w['ticker']}{w['dir']}{w['agree']}"
                                 f"{tag(w['ticker'])}" for w in wl[:20])
                wmsg = f"WATCH baseline ({len(wl)} names): {body}"
            else:
                parts = []
                if new:
                    parts.append("NEW " + ", ".join(
                        f"{w['ticker']}{w['dir']}" for w in wl
                        if w["ticker"] in set(new)))
                if dropped:
                    parts.append("DROPPED " + ", ".join(dropped))
                wmsg = f"WATCH delta: {'; '.join(parts)} ({len(cur)} tracked)"
            print(wmsg, flush=True)
            for res in alert(wmsg, chans, cfg, title="Watchlist"):
                print("  ", res)

    for r in rows:
        msg = fmt_row(r) + tag(r["ticker"])
        print(msg)
        for res in alert(msg, chans, cfg, title=f"{r['ticker']} signal"):
            print("  ", res)
        pd.DataFrame([{"date": dt.date.today().isoformat(),
                       "ticker": r["ticker"], "strategy": r["strategy"],
                       "side": r["side"], "entry": r["entry"],
                       "stop": r["stop"], "target": str(r["target"]),
                       "grade": r["grade"], "timeframe": "daily",
                       "status": r["status"], "outcome_R": "",
                       "exit_reason": "", "regime": "", "news_flag": "",
                       "notes": r.get("notes", r["variant"])}]).to_csv(
            LOG, mode="a", header=False, index=False)
    if not rows:
        print("(quiet tape: no setups)")
    print(f"logged {len(rows)} rows -> signal_log.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
