"""Fetch top-50 US universe with liquidity filter and META exclusion.

Usage:
    python universe/fetch_universe.py --out universe/universe_live.csv

Reads a candidate list, pulls market caps + 90d dollar volume via yfinance,
applies METHOD.md, writes an auditable snapshot. Requires: pandas, yfinance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

SP500_URL = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
             "/main/data/constituents.csv")

# Fallback pool if the S&P 500 list cannot be fetched (ranking still on live data).
CANDIDATES = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA",
    "BRK-B", "JPM", "XOM", "LLY", "V", "MA", "NFLX", "ORCL", "COST", "WMT",
    "HD", "PG", "JNJ", "BAC", "ABBV", "KO", "CRM", "CSCO", "AMD", "DIS",
    "LIN", "MRK", "TMO", "PEP", "ACN", "ABT", "DHR", "VZ", "TXN", "QCOM",
    "NEE", "UNH", "PM", "HON", "IBM", "GE", "NOW", "AMGN", "GS", "MS",
    "CAT", "AXP", "SPGI", "BLK", "DE", "LOW", "GILD", "MDT", "CVX", "PLTR",
    "ADBE", "INTU", "AMAT",
]


def get_candidates(pd) -> list:
    """S&P 500 constituents so the quartile liquidity filter can leave 50+ names."""
    try:
        df = pd.read_csv(SP500_URL)
        syms = df["Symbol"].astype(str).str.replace(".", "-", regex=False)
        syms = sorted(set(syms) - {"META"})
        print(f"candidate pool: S&P 500 ({len(syms)} tickers)", file=sys.stderr)
        return syms
    except Exception as e:
        print(f"S&P list fetch failed ({e}); using fallback pool", file=sys.stderr)
        return [t for t in CANDIDATES if t != "META"]


def main(out: str, top: int = 50) -> int:
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError:
        print("need pandas + yfinance: pip install pandas yfinance", file=sys.stderr)
        return 2

    tickers = get_candidates(pd)

    # One batched download for all histories (fast, single HTTP call).
    try:
        px = yf.download(tickers, period="6mo", auto_adjust=True,
                         progress=False, threads=True, group_by="ticker")
    except Exception as e:
        print(f"batch download failed: {e}", file=sys.stderr)
        return 1

    def hist_of(t):
        try:
            h = px[t] if len(tickers) > 1 else px
            return h.dropna(subset=["Close"])
        except Exception:
            return None

    rows = []
    for t in tickers:
        try:
            hist = hist_of(t)
            if hist is None or hist.empty:
                print(f"skip {t}: no history", file=sys.stderr)
                continue
            dv90 = float((hist["Close"] * hist["Volume"]).tail(90).median())
            tk = yf.Ticker(t)
            try:
                cap = tk.fast_info.get("marketCap")
            except Exception:
                cap = None
            sector = ""
            if not cap:
                info = tk.info or {}
                cap = info.get("marketCap")
                sector = info.get("sector", "")
            if not cap:
                print(f"skip {t}: no market cap", file=sys.stderr)
                continue
            rows.append({"ticker": t, "market_cap": float(cap),
                         "dollar_vol_90d": dv90, "sector": sector})
        except Exception as e:  # per-ticker failure must not kill the run
            print(f"skip {t}: {e}", file=sys.stderr)

    if not rows:
        print("no data fetched", file=sys.stderr)
        return 1

    df = pd.DataFrame(rows)
    df = df[df["ticker"] != "META"]  # hard exclusion first
    if len(df) > top:
        # narrow pool (e.g. top-50): quartile liquidity filter, then rank cut
        dv_cut = df["dollar_vol_90d"].quantile(0.75)
        df = df[df["dollar_vol_90d"] >= dv_cut]
    else:
        # full pool: absolute liquidity floor ($25M/day) instead of quartile
        df = df[df["dollar_vol_90d"] >= 25e6]
    df = df.sort_values("market_cap", ascending=False).head(top)
    df.insert(0, "snapshot_date", dt.date.today().isoformat())
    df.insert(1, "source", "yfinance")
    df.to_csv(out, index=False)
    print(f"wrote {len(df)} tickers -> {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="universe/universe_live.csv")
    ap.add_argument("--top", type=int, default=50,
                    help="pool size by market cap (500 = full S&P 500 pool)")
    args = ap.parse_args()
    sys.exit(main(args.out, args.top))
