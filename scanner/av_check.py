"""Alpha Vantage nightly calibration (<=5 calls/day of the 25 free).

Compares OUR computed RSI(14)/MACD(12,26,9) vs Alpha Vantage server-side
values for 2 tickers. Drift beyond tolerance means our math or data
diverged -> investigate before trusting live scores.
Usage: python scanner/av_check.py  (nightly; NOT per-scan, quota is 25/day)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from combos import add_features  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS = ["NVDA", "AAPL"]
TOL_RSI, TOL_MACD = 1.5, 0.05


def av(fn: str, symbol: str, key: str, **kw) -> dict:
    q = {"function": fn, "symbol": symbol, "apikey": key, **kw}
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def main() -> int:
    key = json.load(open(os.path.join(ROOT, "scanner", "config.json"))).get(
        "av_key", "")
    if not key:
        print("av_check: no key", flush=True)
        return 0
    import yfinance as yf
    for t in TICKERS:
        h = yf.download(t, period="3mo", interval="1d", auto_adjust=True,
                        progress=False)
        if isinstance(h.columns, pd.MultiIndex):
            h.columns = h.columns.get_level_values(0)
        h.columns = [c.capitalize() for c in h.columns]
        f = add_features(h.dropna(subset=["Close"]))
        ours_rsi = float(f["rsi"].iloc[-1])
        c = f["Close"]
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(
            span=26, adjust=False).mean()
        ours_macd = float((macd - macd.ewm(span=9, adjust=False).mean()).iloc[-1])
        try:
            r = av("RSI", t, key, interval="daily", time_period=14,
                   series_type="close")["Technical Analysis: RSI"]
            av_rsi = float(r[sorted(r)[-1]]["RSI"])
            m = av("MACD", t, key, interval="daily", series_type="close")
            mh = m["Technical Analysis: MACD"]
            av_macd = float(mh[sorted(mh)[-1]]["MACD_Hist"])
            dr, dm = abs(ours_rsi - av_rsi), abs(ours_macd - av_macd)
            flag = "" if dr <= TOL_RSI and dm <= TOL_MACD else "  <-- DRIFT"
            print(f"{t}: rsi ours={ours_rsi:.2f} av={av_rsi:.2f} | "
                  f"macd_hist ours={ours_macd:.3f} av={av_macd:.3f}{flag}",
                  flush=True)
        except Exception as e:
            print(f"{t}: AV call failed ({e})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
