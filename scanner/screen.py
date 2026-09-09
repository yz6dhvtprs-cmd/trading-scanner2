"""Agent1 screener (hourly): confidence-ranked watchlist, 25 max + kickout.

Usage: python scanner/screen.py --channels dry
Reads universe_live.csv (top-50; full-pool rescan stays nightly).
Writes scanner/agent_watch.json: {ticker: {conf, dir, adx, updated}}.
Top 25 by confidence survive; lowest is kicked. Change-only: logs only.
Confidence = 0.4*min(adx,50)/50 + 0.3*align + 0.2*|rsi-50|/50 + 0.1*confirms/4.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from combos import add_features  # noqa: E402
from indicators2 import add_extra, alignment  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(ROOT, "scanner", "agent_watch.json")
MAXN = 25


def confidence(f) -> tuple:
    i = len(f) - 1
    adx = float(f["adx"].iloc[i])
    rsi = float(f["rsi"].iloc[i])
    al = alignment(f).iloc[-1]
    side = 1 if al["bull_frac"] >= al["bear_frac"] else -1
    ag = float(max(al["bull_frac"], al["bear_frac"]))
    d = 1 if side == 1 else -1
    suf = "long" if d == 1 else "short"
    conf_n = sum(bool(f[f"{k}_{suf}"].iloc[i]) for k in
                 ("mtf_ok", "rsi_range_ok", "macd_ok"))
    conf = (0.4 * min(adx, 50) / 50 + 0.3 * ag +
            0.2 * abs(rsi - 50) / 50 + 0.1 * conf_n / 3)
    return round(conf, 3), ("UP" if side == 1 else "DN"), round(adx, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="dry")
    ap.parse_args()
    import yfinance as yf
    tickers = pd.read_csv(os.path.join(
        ROOT, "universe", "universe_live.csv"))["ticker"].tolist()
    px = yf.download(tickers, period="1y", interval="1d", auto_adjust=True,
                     progress=False, threads=True, group_by="ticker")
    scored = {}
    for t in tickers:
        try:
            h = px[t].dropna(subset=["Close"])
            h.columns = [c.capitalize() for c in h.columns]
            if len(h) < 225:
                continue
            f = add_extra(add_features(h))
            c, d, a = confidence(f)
            scored[t] = {"conf": c, "dir": d, "adx": a,
                         "updated": dt.date.today().isoformat()}
        except Exception:
            continue
    top = dict(sorted(scored.items(), key=lambda kv: -kv[1]["conf"])[:MAXN])
    prev = {}
    if os.path.exists(WATCH):
        prev = json.load(open(WATCH))
    new = sorted(set(top) - set(prev))
    kicked = sorted(set(prev) - set(top))
    json.dump(top, open(WATCH, "w"), indent=1)
    print(f"screened {len(scored)} -> watch {len(top)} | "
          f"NEW {new} | KICKED {kicked}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
