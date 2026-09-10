"""Combination search over the S&P 500 pool: strategies x sides x ADX filter x exits.

Usage:
    source .venv/bin/activate
    python backtest/combos.py --refresh --out backtest/combos_r1.csv
    python backtest/combos.py --out backtest/combos_r2.csv   # reuse cache

Round 1 grid: 3 strategies x 2 sides x ADX{0,20,25} x exits{fixed2,fixed3,trail2.5,trail3}
= 72 combos. Ranking on TRAIN pooled expectancy (min 100 train trades);
TEST shown as confirmation only (never selected on).
Data: 5y daily (200d warmup + ~4y eval, 75/25 split => ~3y train / ~1y test).
Cache: backtest/cache_sp500.pkl (ticker -> feature frame), rebuilt with --refresh.
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "backtest", "cache_sp500.pkl")
SP500_URL = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
             "/main/data/constituents.csv")
COST_BPS = 2.5
MIN_TRAIN_N = 100


# ---------- indicators ----------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    ru = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    rd = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(h, l, c, n=14):
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), plus_di, minus_di


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]
    df["ema9"], df["ema21"], df["ema50"] = ema(c, 9), ema(c, 21), ema(c, 50)
    df["sma200"] = c.rolling(200).mean()
    df["rsi"] = rsi(c)
    df["atr"] = atr(h, l, c)
    df["rvol"] = v / v.rolling(50).mean()
    df["hi20"] = h.rolling(20).max().shift(1)
    df["lo20"] = l.rolling(20).min().shift(1)
    df["adx"], df["pdi"], df["mdi"] = adx(h, l, c)
    df["bull"] = c > df["sma200"]
    df["cross_up"] = (c.shift(1) < df["ema21"].shift(1)) & (c > df["ema21"])
    df["cross_dn"] = (c.shift(1) > df["ema21"].shift(1)) & (c < df["ema21"])
    df["cross_up5"] = df["cross_up"].rolling(6).sum() >= 1
    df["cross_dn5"] = df["cross_dn"].rolling(6).sum() >= 1
    df["cross_up10n"] = df["cross_up"].rolling(11).sum()
    df["cross_dn10n"] = df["cross_dn"].rolling(11).sum()
    return df


# ---------- vectorized signal masks ----------
def signal_mask(df: pd.DataFrame, strategy: str, side: int,
                adx_min: float, rvol_min: float = 2.0) -> np.ndarray:
    """rvol_min default 2.0 preserves all published backtest results; the
    live scanner passes the algo.json value (2.0 — a 1.5 relaxation was
    tested 2026-09-09 and failed TEST: +0.54 train / -0.15 test)."""
    bull = df["bull"].to_numpy()
    adx_ok = np.ones(len(df), dtype=bool) if adx_min <= 0 else \
        (df["adx"].to_numpy() >= adx_min)
    c = df["Close"].to_numpy(); o = df["Open"].to_numpy()
    h = df["High"].to_numpy(); l = df["Low"].to_numpy()
    e9 = df["ema9"].to_numpy(); e21 = df["ema21"].to_numpy()
    e50 = df["ema50"].to_numpy(); r = df["rsi"].to_numpy()
    if strategy == "pullback":
        if side == 1:
            m = (e9 > e21) & (e21 > e50) & bull & (l <= e9) & \
                (l >= e21 * 0.995) & (r >= 38) & (r <= 55) & \
                (c > e9) & (c > o)
        else:
            m = (e9 < e21) & (e21 < e50) & (~bull) & (h >= e9) & \
                (h <= e21 * 1.005) & (r >= 45) & (r <= 62) & \
                (c < e9) & (c < o)
    elif strategy == "breakout":
        rv = df["rvol"].to_numpy()
        if side == 1:
            m = (c > df["hi20"].to_numpy()) & (rv >= rvol_min) & (r >= 50) & \
                (r <= 67) & bull
        else:
            m = (c < df["lo20"].to_numpy()) & (rv >= rvol_min) & (r >= 33) & \
                (r <= 50) & (~bull)
    else:  # retest
        if side == 1:
            m = bull & (c > e21) & (l <= e21 * 1.002) & \
                df["cross_up5"].to_numpy() & (df["cross_up10n"].to_numpy() <= 2)
        else:
            m = (~bull) & (c < e21) & (h >= e21 * 0.998) & \
                df["cross_dn5"].to_numpy() & (df["cross_dn10n"].to_numpy() <= 2)
    valid = df[["ema50", "sma200", "adx", "rvol", "hi20"]].notna().all(axis=1)
    return (m & adx_ok & valid.to_numpy())


# ---------- simulation from signal indices (fixed or chandelier trail) ----------
def level_of(df, strategy, side, i):
    if strategy == "breakout":
        return float(df["hi20"].iloc[i] if side == 1 else df["lo20"].iloc[i])
    return float(df["Close"].iloc[i])


def risk_of(df, strategy, side, i):
    c = float(df["Close"].iloc[i])
    a = float(df["atr"].iloc[i])
    if strategy == "pullback":
        base = c - float(df["Low"].iloc[i]) if side == 1 else \
            float(df["High"].iloc[i]) - c
    elif strategy == "breakout":
        lvl = level_of(df, strategy, side, i)
        base = (c - (lvl - 0.25 * a)) if side == 1 else ((lvl + 0.25 * a) - c)
    else:
        base = c - float(df["Low"].iloc[i]) if side == 1 else \
            float(df["High"].iloc[i]) - c
    return float(min(max(base, 0.2 * a), 3 * a))


def simulate(df: pd.DataFrame, sig_idx: np.ndarray, strategy: str, side: int,
             exit_spec: tuple) -> list:
    """exit_spec: ('fixed', target_r, time_stop) or ('trail', mult, time_stop).
    Returns list of dicts(exit_bar, R, mae_R, mfe_R). Fills at next open."""
    out = []
    n = len(df)
    op = df["Open"].to_numpy(); hi = df["High"].to_numpy()
    lo = df["Low"].to_numpy(); cl = df["Close"].to_numpy()
    atrv = df["atr"].to_numpy()
    mode = exit_spec[0]
    i_ptr = 0
    sigs = sig_idx[(sig_idx >= 0) & (sig_idx < n - 2)]
    while i_ptr < len(sigs):
        i = int(sigs[i_ptr])
        entry, risk = float(op[i + 1]), risk_of(df, strategy, side, i)
        if not (np.isfinite(entry) and np.isfinite(risk)) or risk <= 0:
            i_ptr += 1
            continue
        if abs(entry - level_of(df, strategy, side, i)) > 1.0 * float(atrv[i]):
            i_ptr += 1
            continue
        cost_r = 2 * (COST_BPS / 1e4) * entry / risk
        tstop_n = int(exit_spec[2])
        mae = mfe = 0.0
        done, exit_i, exit_r = False, i + 1, 0.0
        if mode == "fixed":
            tgt_r = float(exit_spec[1])
            stop = entry - risk * side
            tgt = entry + tgt_r * risk * side
            for j in range(i + 1, min(i + 1 + tstop_n, n)):
                ext = (hi[j] - entry) / risk * side
                adv = (lo[j] - entry) / risk * side
                mfe, mae = max(mfe, ext), min(mae, adv)
                hit_s = (lo[j] <= stop) if side == 1 else (hi[j] >= stop)
                hit_t = (hi[j] >= tgt) if side == 1 else (lo[j] <= tgt)
                if hit_s:
                    exit_r, exit_i, done = -1.0, j, True
                    break
                if hit_t:
                    exit_r, exit_i, done = tgt_r, j, True
                    break
            if not done:
                j = min(i + tstop_n, n - 1)
                mfe = max(mfe, (hi[j] - entry) / risk * side)
                mae = min(mae, (lo[j] - entry) / risk * side)
                exit_r, exit_i = side * (cl[j] - entry) / risk, j
        else:  # chandelier trail
            mult = float(exit_spec[1])
            extreme, tstop = entry, entry - risk * side
            for j in range(i + 1, min(i + 1 + tstop_n, n)):
                mfe = max(mfe, (hi[j] - entry) / risk * side)
                mae = min(mae, (lo[j] - entry) / risk * side)
                extreme = max(extreme, hi[j]) if side == 1 else \
                    min(extreme, lo[j])
                tstop = (max(tstop, extreme - mult * atrv[j]) if side == 1
                         else min(tstop, extreme + mult * atrv[j]))
                touched = (lo[j] <= tstop) if side == 1 else (hi[j] >= tstop)
                if touched:
                    fill = min(op[j], tstop) if side == 1 else max(op[j], tstop)
                    exit_r = side * (fill - entry) / risk
                    exit_i, done = j, True
                    break
            if not done:
                j = min(i + tstop_n, n - 1)
                exit_r, exit_i = side * (cl[j] - entry) / risk, j
        out.append({"exit_bar": exit_i, "R": exit_r - cost_r,
                    "mae_R": mae, "mfe_R": mfe})
        i_ptr = int(np.searchsorted(sigs, exit_i + 1))
    return out


def pooled(trades: list) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "exp": 0.0, "pf": 0.0, "mae": 0.0, "mfe": 0.0}
    r = np.array([t["R"] for t in trades])
    w = r[r > 0]
    gw, gl = w.sum(), -r[r <= 0].sum()
    return {"n": int(len(r)), "wr": float((r > 0).mean()),
            "exp": float(r.mean()),
            "pf": float(gw / gl) if gl > 0 else 99.0,
            "mae": float(np.mean([t["mae_R"] for t in trades])),
            "mfe": float(np.mean([t["mfe_R"] for t in trades]))}


# ---------- driver ----------
def load_frames(refresh: bool, period: str = "5y",
                cache: str = CACHE) -> dict:
    if os.path.exists(cache) and not refresh:
        print(f"loading cache {cache}", flush=True)
        with open(cache, "rb") as f:
            return pickle.load(f)
    import yfinance as yf
    pool = pd.read_csv(SP500_URL)["Symbol"].str.replace(".", "-", regex=False)
    tickers = sorted(set(pool) - {"META"})
    print(f"{len(tickers)} tickers, downloading {period} daily...", flush=True)
    px = yf.download(tickers, period=period, interval="1d", auto_adjust=True,
                     progress=False, threads=True, group_by="ticker")
    frames = {}
    for t in tickers:
        try:
            h = px[t].dropna(subset=["Close"])
            h.columns = [c.capitalize() for c in h.columns]
            if len(h) < 300:
                continue
            frames[t] = add_features(h)
        except Exception:
            continue
    print(f"cached {len(frames)} frames", flush=True)
    with open(cache, "wb") as f:
        pickle.dump(frames, f)
    return frames


GRID = {
    "strategy": ["pullback", "breakout", "retest"],
    "side": [1, -1],
    "adx_min": [0, 20, 25],
    "exit": [("fixed", 2.0, 20), ("fixed", 3.0, 20),
             ("trail", 2.5, 40), ("trail", 3.0, 40)],
}


def parse_exit(spec: str) -> tuple:
    spec = spec.strip().lower()
    if spec.startswith("trail"):
        return ("trail", float(spec[5:]), 40)
    if spec.startswith("fixed"):
        return ("fixed", float(spec[5:]), 20)
    raise ValueError(f"bad exit spec: {spec}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--out", default="backtest/combos_r1.csv")
    ap.add_argument("--strategy", default="pullback,breakout,retest")
    ap.add_argument("--side", default="long,short")
    ap.add_argument("--adx", default="0,20,25")
    ap.add_argument("--exit", default="fixed2.0,fixed3.0,trail2.5,trail3.0")
    a = ap.parse_args()

    grid = {
        "strategy": [s.strip() for s in a.strategy.split(",")],
        "side": [1 if s.strip() == "long" else -1
                 for s in a.side.split(",")],
        "adx_min": [float(x) for x in a.adx.split(",")],
        "exit": [parse_exit(x) for x in a.exit.split(",")],
    }

    frames = load_frames(a.refresh, a.period, a.cache)
    rows = []
    total = len(grid["strategy"]) * len(grid["side"]) * len(grid["adx_min"]) * \
        len(grid["exit"])
    k = 0
    for s in grid["strategy"]:
        for d in grid["side"]:
            for ax in grid["adx_min"]:
                sigs = {t: np.flatnonzero(signal_mask(f, s, d, ax))
                        for t, f in frames.items()}
                for ex in grid["exit"]:
                    k += 1
                    tr_all, te_all = [], []
                    for t, f in frames.items():
                        idx = sigs[t]
                        if len(idx) == 0:
                            continue
                        split = int(len(f) * 0.75)
                        tr = simulate(f.iloc[:split], idx[idx < split], s, d, ex)
                        te = simulate(f.iloc[split:], idx[idx >= split] - split,
                                      s, d, ex)
                        tr_all += tr
                        te_all += te
                    m_tr, m_te = pooled(tr_all), pooled(te_all)
                    ex_name = f"{ex[0]}{ex[1]}"
                    rows.append({"strategy": s,
                                 "side": "long" if d == 1 else "short",
                                 "adx_min": ax, "exit": ex_name,
                                 **{f"train_{kk}": vv for kk, vv in m_tr.items()},
                                 **{f"test_{kk}": vv for kk, vv in m_te.items()}})
                    print(f"[{k}/{total}] {s} {'long' if d == 1 else 'short'} "
                          f"adx{ax} {ex_name}: train n={m_tr['n']} "
                          f"exp={m_tr['exp']:+.3f} | test n={m_te['n']} "
                          f"exp={m_te['exp']:+.3f}", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv(a.out, index=False)
    ok = res[res["train_n"] >= MIN_TRAIN_N].sort_values("train_exp",
                                                       ascending=False)
    print("\n=== TOP 12 by TRAIN expectancy (n>=100; test = confirmation) ===")
    print(ok.head(12)[["strategy", "side", "adx_min", "exit", "train_n",
                        "train_exp", "train_pf", "test_n", "test_exp",
                        "test_pf"]].to_string(index=False))
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
