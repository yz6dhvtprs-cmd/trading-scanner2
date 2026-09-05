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
from notify import alert, load_config  # noqa: E402

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
                frames[t] = add_features(h)
        except Exception:
            continue
    return frames


def scan_nightly(frames: dict, algo: dict) -> list:
    """Score last completed bar per ticker. Returns alert dicts."""
    out = []
    for t, f in frames.items():
        i = len(f) - 1
        for v in algo["variants"]:
            d = 1 if v["side"] == "long" else -1
            if not signal_mask(f, v["strategy"], d,
                               v["filters"]["adx_min"])[i]:
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

    rows = scan_nightly(frames, algo)
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
    for r in rows:
        msg = (f"[{r['grade']}] {r['ticker']} {r['side'].upper()} "
               f"{r['strategy']} ({r['variant']}) | entry {r['entry']} "
               f"stop {r['stop']} tgt {r['target']} | {r['status']} | "
               f"opt: {'call' if r['side']=='long' else 'put'} debit spread")
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
                       "notes": r["variant"]}]).to_csv(
            LOG, mode="a", header=False, index=False)
    if not rows:
        print("(quiet tape: no setups)")
    print(f"logged {len(rows)} rows -> signal_log.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
