"""Simple DEMA pullback scanner + 20% backtest.

Setup (daily): Close below DEMA 8, 10 AND 21, while within 1xATR of DEMA50
(either side) — price raining under the short trends but sitting on value —
plus RSI14 >= --rsi-min (momentum not dead; --no-rsi disables).

Backtest per fresh signal: enter at the signal-day close (proxy for the
closing-15m fill — same print), then report days to +target% (default 20%,
first High at/above it), max profit % (best High since), and current
profit % vs the last daily close. Signals cluster day-runs: only the
FIRST bar of each consecutive run counts (no overlapping entries).

Usage:
    python scanner/simple_scan.py --ticker NVDA --days 60
    python scanner/simple_scan.py --pool spy50 --days 90 --target 0.20
Pools: sp100 (top-100 S&P500 by weight, proxy), sp500, spy (=sp500),
qqq (=nasdaq100), spy50, qqq50 (top 50 by ETF weight, SlickCharts 09-2026).
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_pool(name: str) -> list:
    """Weight-ranked tickers for a pool (yfinance form: BRK-B not BRK.B)."""
    def read(f):
        df = pd.read_csv(os.path.join(ROOT, "universe", f))
        return [t.replace(".", "-") for t in df["symbol"].tolist()]

    if name in ("sp500", "spy"):
        return read("sp500_w.csv")
    if name == "qqq":
        return read("nasdaq100_w.csv")
    if name == "spy50":
        return read("sp500_w.csv")[:50]
    if name == "qqq50":
        return read("nasdaq100_w.csv")[:50]
    if name == "sp100":
        return read("sp500_w.csv")[:100]  # proxy: 100 largest S&P weights
    raise ValueError(f"unknown pool {name}")


def dema(s: pd.Series, n: int) -> pd.Series:
    """Double EMA: 2*EMA(n) - EMA(EMA(n), n). Faster than EMA, less noise."""
    e1 = s.ewm(span=n, adjust=False).mean()
    return 2 * e1 - e1.ewm(span=n, adjust=False).mean()


def rsi14(close: pd.Series) -> pd.Series:
    """Wilder RSI14 (flat reads 50, pure-up 100)."""
    d = close.astype(float).diff()
    up, dn = d.clip(lower=0.0), -d.clip(upper=0.0)
    ru = up.ewm(alpha=1.0 / 14, adjust=False).mean()
    rd = dn.ewm(alpha=1.0 / 14, adjust=False).mean()
    out = 100.0 - 100.0 / (1.0 + ru / rd.replace(0.0, float("nan")))
    out = out.fillna(50.0)
    out.loc[(rd == 0) & (ru > 0)] = 100.0
    return out


def add_ind(d: pd.DataFrame) -> pd.DataFrame:
    """Daily + DEMA8/10/21/50, RSI14 and Wilder ATR14."""
    d = d.copy()
    c = d["Close"].astype(float)
    for n in (8, 10, 21, 50):
        d[f"dema{n}"] = dema(c, n)
    d["rsi"] = rsi14(c)
    h, l, pc = d["High"].astype(float), d["Low"].astype(float), c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    return d


def setup_row(r, rsi_on: bool = True, rsi_min: float = 40.0,
              atr_mult: float = 1.0) -> bool:
    """One-bar DEMA50+RSI rule (shared by batch scan and live stage)."""
    if not (r["Close"] < r["dema8"] and r["Close"] < r["dema10"] and
            r["Close"] < r["dema21"]):
        return False
    if abs(r["Close"] - r["dema50"]) > atr_mult * r["atr"]:
        return False
    return (not rsi_on) or (r["rsi"] >= rsi_min)


def fresh_signals(d: pd.DataFrame, days: int, rsi_on: bool = True,
                  rsi_min: float = 40.0,
                  atr_mult: float = 1.0) -> pd.DataFrame:
    """First-bar-only signals inside the last `days` bars."""
    d = d.copy()
    sig = pd.Series([setup_row(r, rsi_on, rsi_min, atr_mult)
                     for _, r in d.iterrows()], index=d.index)
    d["sig"] = sig & ~sig.shift(1, fill_value=False)
    return d.tail(days)


def backtest(d: pd.DataFrame, day, entry: float,
             target: float) -> dict:
    """Forward stats from the bar after `day`. Closes trigger nothing;
    Highs print the money (target touch, max excursion)."""
    fwd = d[d.index > day]
    if len(fwd) == 0:
        return {"days": None, "max": 0.0, "now": 0.0}
    hit = fwd[fwd["High"].astype(float) >= entry * (1.0 + target)]
    return {"days": int((hit.index[0] - day).days) if len(hit) else None,
            "max": float(fwd["High"].astype(float).max() / entry - 1.0),
            "now": float(fwd["Close"].astype(float).iloc[-1] / entry - 1.0)}


def scan(tickers: list, days: int, target: float, rsi_on: bool = True,
         rsi_min: float = 40.0, atr_mult: float = 1.0) -> list:
    """Batched daily fetch, per-ticker signals + backtests. One row/setup."""
    import yfinance as yf
    need = days + 120
    period = f"{need}d" if need <= 720 else "5y"
    px = yf.download(tickers, period=period, interval="1d",
                     auto_adjust=True, progress=False, threads=True,
                     group_by="ticker")
    rows = []
    for t in tickers:
        try:
            # multi-ticker fetch lays out (Ticker, Price): select the
            # ticker level; a flat frame is already one ticker.
            f = px[t] if isinstance(px.columns, pd.MultiIndex) else px
            f = f.dropna(subset=["Close"])
            f.columns = [str(c).capitalize() for c in f.columns]
            if len(f) < 80:
                continue
            d = fresh_signals(add_ind(f), days, rsi_on, rsi_min,
                              atr_mult)
            for day, r in d[d["sig"]].iterrows():
                entry = float(r["Close"])
                b = backtest(d, day, entry, target)
                rows.append({
                    "date": day.date().isoformat(), "ticker": t,
                    "entry": round(entry, 2),
                    "dema50": round(float(r["dema50"]), 2),
                    "atr": round(float(r["atr"]), 2),
                    "rsi": round(float(r["rsi"]), 1),
                    "days_to_%d%%" % int(target * 100):
                        b["days"] if b["days"] is not None else "never",
                    "max%": round(b["max"] * 100, 1),
                    "now%": round(b["now"] * 100, 1)})
        except Exception:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", action="append", default=[],
                   help="repeatable, e.g. --ticker NVDA --ticker AAPL")
    g.add_argument("--pool", choices=["sp100", "sp500", "spy", "qqq",
                                      "spy50", "qqq50"])
    ap.add_argument("--days", type=int, default=60,
                    help="signal window (trailing daily bars)")
    ap.add_argument("--target", type=float, default=0.20,
                    help="profit target fraction (0.20 = +20%)")
    ap.add_argument("--rsi", action=argparse.BooleanOptionalAction,
                    default=True, help="RSI14 >= --rsi-min gate")
    ap.add_argument("--rsi-min", type=float, default=40.0,
                    help="RSI floor for the setup")
    ap.add_argument("--atr-mult", type=float, default=1.0,
                    help="DEMA50 proximity band in ATRs")
    a = ap.parse_args()
    tickers = [t.upper() for t in a.ticker] if a.ticker \
        else load_pool(a.pool)
    gate = f"rsi>={a.rsi_min:.0f}" if a.rsi else "rsi=off"
    print(f"simple-scan {len(tickers)} names, last {a.days}d, "
          f"target +{a.target * 100:.0f}%, {gate}", flush=True)
    rows = scan(tickers, a.days, a.target, a.rsi, a.rsi_min, a.atr_mult)
    dk = "days_to_%d%%" % int(a.target * 100)
    print(f"{'date':10} {'ticker':6} {'entry':>8} {'dema50':>8} {'atr':>6} "
          f"{'rsi':>5} {dk:>8} {'max%':>7} {'now%':>7}", flush=True)
    for r in sorted(rows, key=lambda r: (r["date"], r["ticker"])):
        print(f'{r["date"]:10} {r["ticker"]:6} {r["entry"]:8.2f} '
              f'{r["dema50"]:8.2f} {r["atr"]:6.2f} {r["rsi"]:5.1f} '
              f'{str(r[dk]):>8} {r["max%"]:7.1f} {r["now%"]:7.1f}',
              flush=True)
    hit = [r for r in rows if r[dk] != "never"]
    med = sorted(r[dk] for r in hit)
    med = med[len(med) // 2] if med else "-"
    print(f"setups={len(rows)} hit-target={len(hit)} "
          f"median-days={med}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
