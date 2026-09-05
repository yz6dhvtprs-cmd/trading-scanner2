"""2-year walk-forward backtest: 3 directional strategies x universe tickers, daily bars.

Usage:
    source .venv/bin/activate
    python backtest/run.py --universe universe/universe_live.csv --out backtest/results.csv

Method:
- 2y daily bars per ticker (one batched yfinance download).
- Indicators: EMA9/21/50, RSI14, ATR14, RVOL(50d), prior-20d high/low, SMA200 regime.
- Signals form on completed bars; entries fill at the NEXT open (no lookahead).
- Fixed --target-r R target, 1R stop, time-stop exit; costs modeled as bps/side.
- Walk-forward split: first ~18 months TRAIN, last ~6 months TEST.
- Grades (on TEST, net of costs) follow grading.md; <8 test trades -> B (thin).

This is the validation gate from the plan: only positive-expectancy TEST
variants may go live. Intraday (5m/15m/1h) extension is a follow-up step.
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

COST_BPS_PER_SIDE = 2.5
MIN_TEST_TRADES = 8


# ---------- indicators ----------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([h - l, (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]
    df["ema9"], df["ema21"], df["ema50"] = ema(c, 9), ema(c, 21), ema(c, 50)
    df["sma200"] = c.rolling(200).mean()
    df["rsi"] = rsi(c)
    df["atr"] = atr(h, l, c)
    df["rvol"] = v / v.rolling(50).mean()
    df["hi20"] = h.rolling(20).max().shift(1)   # prior 20d level, no lookahead
    df["lo20"] = l.rolling(20).min().shift(1)
    df["bull_regime"] = c > df["sma200"]
    return df


# ---------- signals (defined on completed bar i, filled at open[i+1]) ----------
def sig_pullback(df: pd.DataFrame, i: int, direction: int) -> dict | None:
    r = df.iloc[i]
    if direction == 1:
        if not (r.ema9 > r.ema21 > r.ema50 and r.bull_regime):
            return None
        if not (r.Low <= r.ema9 and r.Low >= r.ema21 * 0.995):
            return None
        if not (38 <= r.rsi <= 55):
            return None
        if not (r.Close > r.ema9 and r.Close > r.Open):  # trigger: reclaim + bullish
            return None
        risk = max(r.Close - r.Low, 0.2 * r.atr)
        risk = min(risk, 3 * r.atr)
        return {"stop_dist": risk, "level": r.Close}
    else:
        if not (r.ema9 < r.ema21 < r.ema50 and not r.bull_regime):
            return None
        if not (r.High >= r.ema9 and r.High <= r.ema21 * 1.005):
            return None
        if not (45 <= r.rsi <= 62):
            return None
        if not (r.Close < r.ema9 and r.Close < r.Open):
            return None
        risk = max(r.High - r.Close, 0.2 * r.atr)
        risk = min(risk, 3 * r.atr)
        return {"stop_dist": risk, "level": r.Close}


def sig_breakout(df: pd.DataFrame, i: int, direction: int) -> dict | None:
    r = df.iloc[i]
    if direction == 1:
        if not (r.Close > r.hi20 and r.rvol >= 2.0 and 50 <= r.rsi <= 67):
            return None
        if not r.bull_regime:
            return None
        risk = max(r.Close - (r.hi20 - 0.25 * r.atr), 0.2 * r.atr)
        if risk > 3 * r.atr:
            return None
        return {"stop_dist": risk, "level": r.hi20}
    else:
        if not (r.Close < r.lo20 and r.rvol >= 2.0 and 33 <= r.rsi <= 50):
            return None
        if r.bull_regime:
            return None
        risk = max((r.lo20 + 0.25 * r.atr) - r.Close, 0.2 * r.atr)
        if risk > 3 * r.atr:
            return None
        return {"stop_dist": risk, "level": r.lo20}


def sig_retest(df: pd.DataFrame, i: int, direction: int) -> dict | None:
    r = df.iloc[i]
    if direction == 1:
        if not (r.bull_regime and r.Close > r.ema21):
            return None
        if not (r.Low <= r.ema21 * 1.002 and r.Close > r.ema21):
            return None
        crossed = ((df.Close.shift(1) < df.ema21.shift(1)) & (df.Close > df.ema21))
        if not crossed.iloc[max(0, i - 5):i + 1].any():
            return None
        # chop filter: max 2 crosses in last 10 bars
        if crossed.iloc[max(0, i - 10):i + 1].sum() > 2:
            return None
        risk = min(max(r.Close - r.Low, 0.2 * r.atr), 3 * r.atr)
        return {"stop_dist": risk, "level": r.Close}
    else:
        if not ((not r.bull_regime) and r.Close < r.ema21):
            return None
        if not (r.High >= r.ema21 * 0.998 and r.Close < r.ema21):
            return None
        crossed = ((df.Close.shift(1) > df.ema21.shift(1)) & (df.Close < df.ema21))
        if not crossed.iloc[max(0, i - 5):i + 1].any():
            return None
        if crossed.iloc[max(0, i - 10):i + 1].sum() > 2:
            return None
        risk = min(max(r.High - r.Close, 0.2 * r.atr), 3 * r.atr)
        return {"stop_dist": risk, "level": r.Close}


STRATEGIES = {"pullback": sig_pullback, "breakout": sig_breakout,
              "retest": sig_retest}


# ---------- simulation: one position at a time, next-open fills ----------
def run_positions(df: pd.DataFrame, sig_fn, direction: int, target_r: float,
                  time_stop: int) -> list:
    trades = []  # (exit_bar_index, R_multiple_net)
    i = 50  # warmup for indicators
    n = len(df)
    while i < n - 2:
        s = sig_fn(df, i, direction)
        if s is None:
            i += 1
            continue
        entry = float(df.Open.iloc[i + 1])
        risk = float(s["stop_dist"])
        if not np.isfinite(entry) or not np.isfinite(risk) or risk <= 0:
            i += 1
            continue
        # breakout chase guard: entry must be near the level
        atr_now = float(df.atr.iloc[i])
        if abs(entry - float(s["level"])) > 1.0 * atr_now:
            i += 1
            continue
        if direction == 1:
            stop, tgt = entry - risk, entry + target_r * risk
        else:
            stop, tgt = entry + risk, entry - target_r * risk
        cost_r = 2 * (COST_BPS_PER_SIDE / 1e4) * entry / risk
        exit_r, exit_i = None, i + 1
        for j in range(i + 1, min(i + 1 + time_stop, n)):
            bar = df.iloc[j]
            if direction == 1:
                hit_stop = bar.Low <= stop
                hit_tgt = bar.High >= tgt
            else:
                hit_stop = bar.High >= stop
                hit_tgt = bar.Low <= tgt
            if hit_stop and hit_tgt:  # ambiguous bar: assume stop first (conservative)
                exit_r, exit_i = -1.0, j
                break
            if hit_stop:
                exit_r, exit_i = -1.0, j
                break
            if hit_tgt:
                exit_r, exit_i = target_r, j
                break
        if exit_r is None:  # time-stop at close
            j = min(i + time_stop, n - 1)
            px = float(df.Close.iloc[j])
            exit_r = direction * (px - entry) / risk
            exit_i = j
        trades.append((exit_i, exit_r - cost_r))
        i = exit_i + 1  # no overlapping positions
    return trades


def metrics(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0.0, "avg_win_R": 0.0, "expectancy_R": 0.0,
                "profit_factor": 0.0, "max_dd_R": 0.0}
    r = np.array([t[1] for t in trades])
    wins = r[r > 0]
    gross_w, gross_l = wins.sum(), -r[r <= 0].sum()
    cum = np.cumsum(r)
    return {"n": int(len(r)), "win_rate": float(len(wins) / len(r)),
            "avg_win_R": float(wins.mean()) if len(wins) else 0.0,
            "expectancy_R": float(r.mean()),
            "profit_factor": float(gross_w / gross_l) if gross_l > 0 else 99.0,
            "max_dd_R": float((np.maximum.accumulate(cum) - cum).max())}


def grade(exp: float, pf: float, n: int) -> str:
    if n < MIN_TEST_TRADES:
        return "B (thin)"
    if exp >= 0.50 and pf >= 1.5:
        return "A+"
    if exp >= 0.30 and pf >= 1.3:
        return "A"
    if exp >= 0.15:
        return "B+"
    return "B"


# ---------- driver ----------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe/universe_live.csv")
    ap.add_argument("--out", default="backtest/results.csv")
    ap.add_argument("--target-r", type=float, default=2.0,
                    help="profit target in R multiples (1:2 == 2.0)")
    ap.add_argument("--time-stop", type=int, default=20,
                    help="max bars to hold before exiting at close")
    a = ap.parse_args()

    import yfinance as yf

    tickers = pd.read_csv(a.universe)["ticker"].tolist()
    print(f"{len(tickers)} tickers, downloading 2y daily...", flush=True)
    px = yf.download(tickers, period="2y", interval="1d", auto_adjust=True,
                     progress=False, threads=True, group_by="ticker")

    def hist_of(t):
        try:
            h = px[t] if len(tickers) > 1 else px
            h = h.dropna(subset=["Close"])
            h.columns = [c.capitalize() for c in h.columns]
            return h
        except Exception:
            return None

    rows = []
    for t in tickers:
        h = hist_of(t)
        if h is None or len(h) < 260:
            print(f"skip {t}: insufficient history", flush=True)
            continue
        df = add_features(h).dropna()
        split = int(len(df) * 0.75)  # ~18mo train / ~6mo test
        for sname, fn in STRATEGIES.items():
            for direction in (1, -1):
                side = "long" if direction == 1 else "short"
                all_tr = run_positions(df, fn, direction, a.target_r,
                                       a.time_stop)
                tr_tr = [x for x in all_tr if x[0] < split]
                te_tr = [(x[0] - split, x[1]) for x in all_tr if x[0] >= split]
                m_tr, m_te = metrics(tr_tr), metrics(te_tr)
                rows.append({"ticker": t, "strategy": sname, "side": side,
                             "target_r": a.target_r,
                             "grade": grade(m_te["expectancy_R"],
                                            m_te["profit_factor"], m_te["n"]),
                             **{f"train_{k}": v for k, v in m_tr.items()},
                             **{f"test_{k}": v for k, v in m_te.items()}})

    res = pd.DataFrame(rows)
    res.to_csv(a.out, index=False)
    print(f"\nwrote {len(res)} rows -> {a.out}", flush=True)

    show = res[["ticker", "strategy", "side", "grade", "test_n",
                "test_win_rate", "test_expectancy_R",
                "test_profit_factor"]].sort_values("test_expectancy_R",
                                                   ascending=False)
    with pd.option_context("display.width", 130, "display.max_rows", 20):
        print("\n=== TOP 15 by test expectancy ===")
        print(show.head(15).to_string(index=False))
        print("\n=== grade counts ===")
        print(res["grade"].value_counts().to_string())
        print("\n=== tradable (A-range, test) ===")
        trad = show[show["grade"].isin(["A+", "A"])]
        print(trad.to_string(index=False) if len(trad) else "(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
