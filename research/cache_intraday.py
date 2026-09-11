"""Cache 15m (and 1h) intraday bars. yfinance caps 15m history at 60 days, so
this is the whole sample that exists from the free endpoint. Run it often; the
window rolls forward and old bars are unrecoverable once they age out.

    python research/cache_intraday.py --interval 15m --tickers AAPL,MSFT,...
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "research", "_cache_intraday")

DEFAULT = ("AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,AMD,NFLX,CRM,ORCL,ADBE,"
           "INTC,CSCO,QCOM,TXN,MU,PANW,SNOW,UBER,COIN,PLTR,SHOP,ABNB,"
           "JPM,BAC,GS,MS,WFC,V,MA,AXP,SCHW,"
           "XOM,CVX,COP,SLB,UNH,JNJ,LLY,PFE,MRK,ABBV,TMO,"
           "WMT,COST,HD,LOW,TGT,NKE,SBUX,MCD,DIS,BA,CAT,DE,GE,LMT,"
           "SPY,QQQ,IWM,DIA,XLK,XLF,XLE,SMH,ARKK")


def fetch(t: str, interval: str, period: str):
    import yfinance as yf
    h = yf.download(t, period=period, interval=interval, auto_adjust=False,
                    prepost=False, progress=False)
    if h is None or len(h) == 0:
        return None
    if isinstance(h.columns, pd.MultiIndex):
        h.columns = h.columns.get_level_values(0)
    h.columns = [str(c).capitalize() for c in h.columns]
    h = h.loc[:, ~h.columns.duplicated()]
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume")
            if c in h.columns]
    h = h[keep].dropna(subset=["Close"])
    idx = pd.DatetimeIndex(h.index)
    # normalise to US/Eastern wall clock so time-of-day features are correct
    idx = idx.tz_convert("America/New_York") if idx.tz is not None \
        else idx.tz_localize("UTC").tz_convert("America/New_York")
    h.index = idx
    h.index.name = "ts"
    # regular session only: 09:30-16:00 ET
    h = h.between_time("09:30", "15:59")
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=DEFAULT)
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--period", default="60d")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    ok, fail = 0, []
    for t in [x.strip().upper() for x in a.tickers.split(",") if x.strip()]:
        p = os.path.join(CACHE, f"{t}_{a.interval}.csv")
        if os.path.exists(p) and not a.force:
            ok += 1
            continue
        try:
            h = fetch(t, a.interval, a.period)
        except Exception as e:  # noqa: BLE001
            fail.append(f"{t}:{str(e)[:50]}")
            continue
        if h is None or len(h) < 200:
            fail.append(f"{t}:short({0 if h is None else len(h)})")
            continue
        h.round(4).to_csv(p)
        ok += 1
        print(f"{t}: {len(h)} bars {h.index[0]} -> {h.index[-1]}", flush=True)
        time.sleep(0.3)
    print(f"cached={ok} failed={len(fail)}", flush=True)
    if fail:
        print("failures: " + ", ".join(fail[:20]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
