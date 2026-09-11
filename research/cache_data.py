"""One-time local cache of daily OHLCV so research runs are reproducible/offline.

Usage:
    python research/cache_data.py                # default basket, 15y daily
    python research/cache_data.py --tickers AAPL,MSFT --years 10

Writes research/_cache/<TICKER>_1d.csv (Date,Open,High,Low,Close,Volume).
Every downstream script should read the cache, never the network, so that
walk-forward results are byte-reproducible across runs.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "research", "_cache")

# Liquid, long-history names across sectors + the index/vol context tickers.
# Breadth matters: a predictor tuned on one symbol's 2y is curve-fit by
# construction. Cross-sectional replication is the only honest check.
DEFAULT = ("AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,JPM,V,UNH,XOM,JNJ,WMT,"
           "PG,HD,MA,COST,ORCL,CVX,ABBV,KO,PEP,MRK,AMD,CRM,NFLX,ADBE,INTC,CSCO,"
           "SPY,QQQ,IWM,DIA,XLF,XLE,XLK,XLV,^VIX")


def fetch(t: str, years: int) -> pd.DataFrame | None:
    import yfinance as yf
    h = yf.download(t, period=f"{years}y", interval="1d",
                    auto_adjust=True, progress=False)
    if h is None or len(h) == 0:
        return None
    if isinstance(h.columns, pd.MultiIndex):
        h.columns = h.columns.get_level_values(0)
    h.columns = [str(c).capitalize() for c in h.columns]
    h = h.loc[:, ~h.columns.duplicated()]
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume")
            if c in h.columns]
    h = h[keep].dropna(subset=["Close"])
    h.index = pd.DatetimeIndex(h.index).tz_localize(None)
    h.index.name = "Date"
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=DEFAULT)
    ap.add_argument("--years", type=int, default=15)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    ok, skip, fail = 0, 0, []
    for t in [x.strip().upper() for x in a.tickers.split(",") if x.strip()]:
        path = os.path.join(CACHE, f"{t.replace('^', '')}_1d.csv")
        if os.path.exists(path) and not a.force:
            skip += 1
            continue
        try:
            h = fetch(t, a.years)
        except Exception as e:  # noqa: BLE001 - report and continue
            h, e_msg = None, str(e)[:80]
            fail.append(f"{t}:{e_msg}")
        if h is None or len(h) < 250:
            if t not in [f.split(":")[0] for f in fail]:
                fail.append(f"{t}:short({0 if h is None else len(h)})")
            continue
        h.round(4).to_csv(path)
        ok += 1
        print(f"{t}: {len(h)} rows {h.index[0].date()} -> {h.index[-1].date()}",
              flush=True)
        time.sleep(0.4)  # be polite to the free endpoint
    print(f"cached={ok} skipped={skip} failed={len(fail)}", flush=True)
    if fail:
        print("failures: " + ", ".join(fail), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
