"""Backtest the ARMED analyzer over historical 15-min bars (read-only).

Walks every 15m bar of the window and runs the live scanner/analyze.py
gates at each bar (directional Long, possible reversal, possible rejection),
with strictly causal data: completed daily bars + a forming daily bar rebuilt
from that day's 15m bars so far (exactly what the live scanner sees
intraday), completed 1h bars only, and indicators recomputed per bar.
Prints one line per NEWLY appearing state (live change-only semantics).

Usage:
    python scanner/backtest_analyzer.py [--ticker TICKER] [--days N]
Missing args are prompted. No alerts, no state writes, no lookahead.

Continuations surface as Long (breakout/pullback triggers); there is no
SHORT leg while shorts stay paused per algo.json (short bias reads print
as Possible Rejection).
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from analyze import analyze, flat  # noqa: E402
from combos import add_features  # noqa: E402
from indicators2 import add_extra  # noqa: E402

PT = "America/Los_Angeles"
MIN_DAILY = 121   # swing_levels reads a 120-bar lookback
MIN_TF = 35       # ema21 + MACD(12,26,9) warmup on 1h/15m
H1_WARMUP_DAYS = 15  # extra 1h history behind the window, indicators only


def fetch(ticker: str, days: int):
    """(daily_full, h1_full, m15_full), oldest-first, tz-aware."""
    import yfinance as yf
    d = flat(yf.download(ticker, period="1y", interval="1d",
                         auto_adjust=True, progress=False, threads=False))
    h1 = flat(yf.download(ticker, period=f"{days + H1_WARMUP_DAYS}d",
                          interval="1h", auto_adjust=True, progress=False,
                          threads=False))
    m15 = flat(yf.download(ticker, period=f"{days}d", interval="15m",
                           auto_adjust=True, progress=False, threads=False))
    out = []
    for f in (d, h1, m15):
        f = f.dropna(subset=["Close"]).sort_index()
        if f.index.tz is None:
            f = f.tz_localize("America/New_York")
        out.append(f)
    return out[0], out[1], out[2]


def prep_tf(f: pd.DataFrame) -> pd.DataFrame:
    """ema21 + MACD on a truncated intraday frame (mirrors live main)."""
    f = f.copy()
    f["ema21"] = f["Close"].ewm(span=21, adjust=False).mean()
    m = f["Close"].ewm(span=12, adjust=False).mean() - \
        f["Close"].ewm(span=26, adjust=False).mean()
    f["macd"] = m
    f["macd_sig"] = m.ewm(span=9, adjust=False).mean()
    return f


def slices_for(d_full: pd.DataFrame, h1_full: pd.DataFrame,
               m15_full: pd.DataFrame, ts: pd.Timestamp):
    """Causal frames as of 15m bar OPEN ts: daily = completed days + a
    forming bar rebuilt from today's 15m bars so far; 1h = bars completed
    by this 15m bar's close; 15m = bars through ts."""
    day = ts.date()
    dhist = d_full[d_full.index.date < day].tail(199)
    daybars = m15_full[(m15_full.index.date == day) &
                       (m15_full.index <= ts)]
    if len(daybars) == 0:
        return None
    forming = pd.DataFrame([{
        "Open": float(daybars["Open"].iloc[0]),
        "High": float(daybars["High"].max()),
        "Low": float(daybars["Low"].min()),
        "Close": float(daybars["Close"].iloc[-1]),
        "Volume": float(daybars["Volume"].sum()),
    }], index=[daybars.index[-1]])
    d = pd.concat([dhist, forming]).tail(200)
    d = add_extra(add_features(d))
    h1 = prep_tf(h1_full[h1_full.index <= ts - pd.Timedelta(minutes=45)])
    m15 = prep_tf(m15_full[m15_full.index <= ts])
    return d, h1, m15


def walk(ticker: str, d_full: pd.DataFrame, h1_full: pd.DataFrame,
         m15_full: pd.DataFrame) -> tuple:
    """Run the armed gates bar by bar. Returns (hits, stats); each hit is a
    newly appeared state (change-only, like live alerts)."""
    hits, active = [], set()
    scanned = evaluated = skipped = errors = 0
    for ts in m15_full.index:
        scanned += 1
        sl = slices_for(d_full, h1_full, m15_full, ts)
        if sl is None:
            skipped += 1
            continue
        d, h1, m15 = sl
        if len(d) < MIN_DAILY or len(h1) < MIN_TF or len(m15) < MIN_TF:
            skipped += 1
            continue
        evaluated += 1
        try:
            results = analyze(ticker, d, h1, m15)
        except Exception:
            errors += 1
            continue
        for r in results:
            if r["state"] not in active:
                hits.append({"time": ts + pd.Timedelta(minutes=15), **r})
        active = {r["state"] for r in results}
    stats = {"scanned": scanned, "evaluated": evaluated, "skipped": skipped,
             "errors": errors}
    return hits, stats


_SIDE = {"Long": "Buy", "Possible upcoming Rejection": "Short",
         "Possible upcoming Reversal": "Buy"}


def fmt(ticker: str, hit: dict) -> str:
    ts = hit["time"].tz_convert(PT).strftime("%Y-%m-%d %H:%MPT")
    side = _SIDE.get(hit["state"], "?")
    return (f'{ts} "{ticker} - {hit["state"]} - {side} @ {hit["price"]} - '
            f'SL {hit["stop"]} - target {hit["target"]}. ({hit["note"]})"')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="")
    ap.add_argument("--days", default="")
    a = ap.parse_args()
    ticker = (a.ticker or input("Ticker: ")).strip().upper()
    days_raw = (str(a.days) or input("Days of 15m/1h history (1-60): ")) \
        .strip()
    if not ticker:
        print("no ticker given", flush=True)
        return 1
    try:
        days = int(days_raw or 20)
    except ValueError:
        print(f"bad days: {days_raw!r}", flush=True)
        return 1
    if not 1 <= days <= 60:
        print(f"days must be 1-60 (15m fetch limit), got {days}", flush=True)
        return 1
    try:
        d_full, h1_full, m15_full = fetch(ticker, days)
    except Exception as e:
        print(f"fetch failed for {ticker}: {e}", flush=True)
        return 1
    if len(m15_full) == 0 or len(d_full) == 0:
        print(f"no data for {ticker} (bad ticker or empty window)",
              flush=True)
        return 1
    span0 = m15_full.index[0].tz_convert(PT).strftime("%Y-%m-%d")
    span1 = m15_full.index[-1].tz_convert(PT).strftime("%Y-%m-%d")
    print(f"BACKTEST {ticker} | 15m window {span0}..{span1} "
          f"({len(m15_full)} bars) | daily tail-200 | "
          f"1h {days}+{H1_WARMUP_DAYS}d warmup | change-only hits",
          flush=True)
    hits, stats = walk(ticker, d_full, h1_full, m15_full)
    for h in hits:
        print(fmt(ticker, h), flush=True)
    print(f"done: {len(hits)} trade setups | {stats['evaluated']} bars "
          f"evaluated, {stats['skipped']} warmup-skipped, "
          f"{stats['errors']} errors", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
