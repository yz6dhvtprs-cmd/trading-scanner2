"""Panel builder: cached OHLCV -> causal features + triple-barrier labels.

Every column here is computed from information available at the CLOSE of bar t.
The trade is assumed to be entered at the OPEN of bar t+1, so nothing that
touches bar t+1 or later may appear in a feature. Labels deliberately DO look
forward; they are only ever consumed by the scorer, never by the model at
predict time. The split between the two is enforced by naming: features have
no prefix, labels are prefixed `y_`.

Usage:
    from panel import build_panel
    df = build_panel(["AAPL", "MSFT"], horizon=20, stop_atr=1.5, tp_r=2.0)
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "research", "_cache")

FEATURES = [
    "ret1", "ret2", "ret3", "ret5", "ret10", "ret21", "ret63",
    "atr_pct", "vol_ratio", "rsi14", "rsi21", "macd_h_atr", "adx14",
    "d_sma20", "d_sma50", "d_sma200", "d_hi20", "d_lo20", "d_hi55", "d_lo55",
    "ema_stack", "rvol", "vol_z", "gap_atr", "body_atr", "range_atr",
    "spy_ret5", "spy_ret21", "spy_regime", "vix", "vix_chg", "beta_ret5",
]


# ---------------------------------------------------------------- indicators

def _tr(df: pd.DataFrame) -> pd.Series:
    return pd.concat([df["High"] - df["Low"],
                      (df["High"] - df["Close"].shift()).abs(),
                      (df["Low"] - df["Close"].shift()).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _tr(df).ewm(alpha=1 / n, adjust=False).mean()


def rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    ru = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    rd = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ADX. The repo's own backtests found the ADX>=20 gate to be a
    real, TEST-confirmed effect, so it belongs in the feature set."""
    up = df["High"].diff()
    dn = -df["Low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr_n = _tr(df).ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(
        alpha=1 / n, adjust=False).mean() / tr_n
    mdi = 100 * pd.Series(minus, index=df.index).ewm(
        alpha=1 / n, adjust=False).mean() / tr_n
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


# ------------------------------------------------------------------- loading

def load(ticker: str) -> pd.DataFrame:
    p = os.path.join(CACHE, f"{ticker.replace('^', '')}_1d.csv")
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"{p} missing - run: python research/cache_data.py")
    df = pd.read_csv(p, parse_dates=["Date"]).sort_values("Date")
    return df.reset_index(drop=True)


def _context() -> pd.DataFrame:
    """Market context keyed by date: SPY momentum/regime and VIX level."""
    spy = load("SPY").set_index("Date")
    vix = load("VIX").set_index("Date")
    ctx = pd.DataFrame(index=spy.index)
    ctx["spy_close"] = spy["Close"]
    ctx["spy_ret5"] = spy["Close"].pct_change(5)
    ctx["spy_ret21"] = spy["Close"].pct_change(21)
    ctx["spy_regime"] = (spy["Close"] > spy["Close"].rolling(200).mean()
                         ).astype(float)
    ctx["vix"] = vix["Close"].reindex(ctx.index).ffill()
    ctx["vix_chg"] = ctx["vix"].pct_change(5)
    return ctx.reset_index()


# ------------------------------------------------------------------ features

def features_for(df: pd.DataFrame, ctx: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    c, h, lo, v = f["Close"], f["High"], f["Low"], f["Volume"]
    a = atr(f, 14)
    f["atr14"] = a
    f["atr_pct"] = a / c

    for n in (1, 2, 3, 5, 10, 21, 63):
        f[f"ret{n}"] = c.pct_change(n)

    r1 = c.pct_change()
    f["vol_ratio"] = (r1.rolling(10).std() / r1.rolling(63).std())
    f["rsi14"] = rsi(c, 14)
    f["rsi21"] = rsi(c, 21)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26,
                                                       adjust=False).mean()
    f["macd_h_atr"] = (macd - macd.ewm(span=9, adjust=False).mean()) / a
    f["adx14"] = adx(f, 14)

    for n in (20, 50, 200):
        f[f"d_sma{n}"] = (c - c.rolling(n).mean()) / a
    # distance to the prior-N extreme; shift(1) so today's bar is excluded
    for n in (20, 55):
        f[f"d_hi{n}"] = (c - h.rolling(n).max().shift(1)) / a
        f[f"d_lo{n}"] = (c - lo.rolling(n).min().shift(1)) / a

    e9 = c.ewm(span=9, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    f["ema_stack"] = ((e9 > e21).astype(float) + (e21 > e50).astype(float)
                      + (c > e9).astype(float))

    vma = v.rolling(50).mean()
    f["rvol"] = v / vma
    f["vol_z"] = (v - vma) / v.rolling(50).std()
    f["gap_atr"] = (f["Open"] - c.shift(1)) / a
    f["body_atr"] = (c - f["Open"]) / a
    f["range_atr"] = (h - lo) / a

    f = f.merge(ctx, on="Date", how="left")
    f["beta_ret5"] = f["ret5"] - f["spy_ret5"]
    return f


# -------------------------------------------------------------------- labels

def triple_barrier(f: pd.DataFrame, side: int, horizon: int,
                   stop_atr: float, tp_r: float) -> pd.DataFrame:
    """Label each bar t by what a trade entered at OPEN[t+1] would have done.

    Risk unit R = stop_atr * ATR14[t]. Stop sits 1R against, target tp_r R in
    favour, and the position is closed at CLOSE[t+horizon] if neither is hit.
    When a single bar's range spans BOTH barriers the stop is assumed to fill
    first: intrabar order is unknowable from daily data, so we take the
    pessimistic branch rather than flatter ourselves.

    Returns y_R (realised R multiple), y_win (target hit first), y_bars.
    """
    n = len(f)
    o = f["Open"].to_numpy()
    hi = f["High"].to_numpy()
    lo_ = f["Low"].to_numpy()
    cl = f["Close"].to_numpy()
    a = f["atr14"].to_numpy()

    entry = np.full(n, np.nan)
    entry[:-1] = o[1:]                       # fill at the next open
    risk = stop_atr * a
    with np.errstate(invalid="ignore"):
        ok = np.isfinite(entry) & np.isfinite(risk) & (risk > 0)

    tp = entry + side * tp_r * risk
    sl = entry - side * risk

    first_tp = np.full(n, np.inf)
    first_sl = np.full(n, np.inf)
    for hstep in range(1, horizon + 1):
        idx = np.arange(n) + hstep
        valid = idx < n
        j = np.where(valid, idx, 0)
        if side > 0:
            tp_hit = valid & (hi[j] >= tp)
            sl_hit = valid & (lo_[j] <= sl)
        else:
            tp_hit = valid & (lo_[j] <= tp)
            sl_hit = valid & (hi[j] >= sl)
        first_tp = np.where(tp_hit & np.isinf(first_tp), hstep, first_tp)
        first_sl = np.where(sl_hit & np.isinf(first_sl), hstep, first_sl)

    end = np.arange(n) + horizon
    end_ok = end < n
    exit_close = np.where(end_ok, cl[np.where(end_ok, end, 0)], np.nan)
    timeout_r = side * (exit_close - entry) / risk

    # stop wins ties (first_sl <= first_tp), per the note above
    y_r = np.where(np.isinf(first_tp) & np.isinf(first_sl), timeout_r,
                   np.where(first_sl <= first_tp, -1.0, tp_r))
    y_bars = np.where(np.isinf(first_tp) & np.isinf(first_sl), horizon,
                      np.minimum(first_tp, first_sl))

    out = pd.DataFrame(index=f.index)
    out["y_R"] = np.where(ok & end_ok, y_r, np.nan)
    out["y_win"] = np.where(np.isfinite(out["y_R"]),
                            (out["y_R"] > 0).astype(float), np.nan)
    out["y_bars"] = np.where(ok & end_ok, y_bars, np.nan)
    out["entry"] = np.where(ok, entry, np.nan)
    out["risk"] = np.where(ok, risk, np.nan)
    return out


# --------------------------------------------------------------------- build

def build_panel(tickers: list[str], side: int = 1, horizon: int = 20,
                stop_atr: float = 1.5, tp_r: float = 2.0,
                min_price: float = 5.0) -> pd.DataFrame:
    ctx = _context()
    frames = []
    for t in tickers:
        try:
            df = load(t)
        except FileNotFoundError:
            continue
        if len(df) < 300:
            continue
        f = features_for(df, ctx)
        y = triple_barrier(f, side, horizon, stop_atr, tp_r)
        f = pd.concat([f, y], axis=1)
        f["ticker"] = t
        frames.append(f)
    if not frames:
        raise RuntimeError("no tickers loaded - populate research/_cache first")
    p = pd.concat(frames, ignore_index=True)
    p = p[p["Close"] >= min_price]
    need = FEATURES + ["y_R", "y_win", "Date", "ticker", "atr14", "entry",
                       "risk"]
    p = p.dropna(subset=need).reset_index(drop=True)
    return p.sort_values(["Date", "ticker"]).reset_index(drop=True)


DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM",
    "V", "UNH", "XOM", "JNJ", "WMT", "PG", "HD", "MA", "COST", "ORCL", "CVX",
    "ABBV", "KO", "PEP", "MRK", "AMD", "CRM", "NFLX", "ADBE", "INTC", "CSCO",
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV",
]
