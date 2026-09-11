"""Intraday long-only breakout alerts on 15m bars, entry-as-stop.

The structure being tested
--------------------------
At some 15m bar t we emit an ALERT carrying a BUY PRICE sitting above the
current market. That price is a level whose break is the thesis, so if price
later trades back through it the thesis is dead and we are out. Entry is the
stop. Long only.

  alert at t  ->  resting buy stop at P
  fill        ->  first bar in the next FILL_BARS whose High >= P
  exit        ->  whichever comes first, within HOLD_BARS after the fill:
                    target  High >= P + TARGET_ATR * atr15
                    stop    Low  <= P - slip           (thesis dead)
                    timeout close of the last bar in the window
  no fill     ->  order cancelled, no trade, no cost

Why the question changes
------------------------
Because the stop sits at entry, direction accuracy is no longer the binding
constraint. What binds is the CONTINUATION RATE: given the level actually
broke, how often does price keep going rather than fall straight back? That is
a conditional question with a much better prior than "which way next", and it
is what this file measures.

Why it is not free money
------------------------
Losses are truncated, not removed. Price oscillating around a level it just
broke is the single most common outcome, so the scratch rate is high and every
scratch pays the spread. The expectancy therefore lives or dies on
continuation rate versus round-trip cost, both of which are measured below.

Honest limit: yfinance caps 15m history at 60 days. That is one market regime
and a few thousand events. This can rule things out. It cannot validate a
strategy for live capital.

    python research/intraday_alert.py --report
    python research/intraday_alert.py --live         # today's actionable alerts
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluate import auc, date_block_bootstrap  # noqa: E402
from models import Calibrator, LogitL2, StumpGBM  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "research", "_cache_intraday")

BREAK_LOOKBACK = 8      # bars whose high defines the level (2 hours)
FILL_BARS = 10          # the buy price must be reached within 2.5 hours
HOLD_BARS = 12          # then at most 3 hours in the trade
TARGET_ATR = 1.0        # profit target, in 15m ATR units above entry
SLIP_BPS = 3.0          # one-way slippage+spread on the underlying, bps
STOP_BUFFER_ATR = 0.0   # 0.0 = stop exactly at entry, per the spec

FEATS = [
    "tod", "bars_from_open", "atr_pct", "atr_ratio", "rvol", "vol_z",
    "ret1", "ret2", "ret4", "ret8", "ret26", "range_compress",
    "dist_vwap_atr", "day_pos", "dist_level_atr", "level_touches",
    "body_ratio", "upper_wick", "lower_wick", "close_loc",
    "spy_ret4", "spy_ret8", "spy_above_vwap", "rs_spy",
    "gap_open_atr", "prior_day_pos",
]


# ------------------------------------------------------------------ loading

def tickers_available() -> list[str]:
    if not os.path.isdir(CACHE):
        return []
    return sorted(f.split("_15m.csv")[0] for f in os.listdir(CACHE)
                  if f.endswith("_15m.csv"))


def load15(t: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(CACHE, f"{t}_15m.csv"), parse_dates=["ts"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(
        "America/New_York")
    return df.sort_values("ts").reset_index(drop=True)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - df["Close"].shift()).abs(),
                    (df["Low"] - df["Close"].shift()).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ----------------------------------------------------------------- features

def build(t: str, spy: pd.DataFrame | None) -> pd.DataFrame | None:
    df = load15(t)
    if len(df) < 300:
        return None
    df["date"] = df["ts"].dt.date
    df["atr15"] = _atr(df, 14)
    c, h, lo, v = df["Close"], df["High"], df["Low"], df["Volume"]

    g = df.groupby("date")
    df["bars_from_open"] = g.cumcount()
    df["tod"] = df["ts"].dt.hour + df["ts"].dt.minute / 60.0
    tp = (h + lo + c) / 3
    df["vwap"] = (tp * v).groupby(df["date"]).cumsum() / \
        v.groupby(df["date"]).cumsum()
    df["day_hi"] = g["High"].cummax()
    df["day_lo"] = g["Low"].cummin()

    df["atr_pct"] = df["atr15"] / c
    df["atr_ratio"] = df["atr15"] / df["atr15"].rolling(78).mean()
    vma = v.rolling(78).mean()
    df["rvol"] = v / vma
    df["vol_z"] = (v - vma) / v.rolling(78).std()
    for n in (1, 2, 4, 8, 26):
        df[f"ret{n}"] = c.pct_change(n)
    rng = (h - lo)
    df["range_compress"] = rng.rolling(BREAK_LOOKBACK).mean() / \
        rng.rolling(78).mean()
    df["dist_vwap_atr"] = (c - df["vwap"]) / df["atr15"]
    span = (df["day_hi"] - df["day_lo"]).replace(0, np.nan)
    df["day_pos"] = (c - df["day_lo"]) / span
    df["body_ratio"] = (c - df["Open"]).abs() / rng.replace(0, np.nan)
    df["upper_wick"] = (h - np.maximum(c, df["Open"])) / rng.replace(0, np.nan)
    df["lower_wick"] = (np.minimum(c, df["Open"]) - lo) / rng.replace(0, np.nan)
    df["close_loc"] = (c - lo) / rng.replace(0, np.nan)

    # the level: highest high of the prior BREAK_LOOKBACK bars, excluding now
    df["level"] = h.rolling(BREAK_LOOKBACK).max().shift(1)
    df["dist_level_atr"] = (df["level"] - c) / df["atr15"]
    df["level_touches"] = (
        (h.shift(1).rolling(BREAK_LOOKBACK).apply(
            lambda w: float(np.sum(w >= w.max() * 0.999)), raw=True)))

    # PRIOR day's close, mapped by date. The obvious-looking
    # g["Close"].transform("last") hands every bar of the day that day's
    # CLOSING price, which is pure lookahead; it inflated OOS AUC to 0.64.
    _last = df.groupby("date")["Close"].last().shift(1)
    _open = df.groupby("date")["Open"].first()
    df["prior_close"] = df["date"].map(_last)
    df["gap_open_atr"] = (df["date"].map(_open) - df["prior_close"]) / \
        df["atr15"]
    df["prior_day_pos"] = df["day_pos"].groupby(df["date"]).transform("first")

    if spy is not None:
        s = spy[["ts", "spy_ret4", "spy_ret8", "spy_above_vwap"]]
        df = df.merge(s, on="ts", how="left")
        df["rs_spy"] = df["ret8"] - df["spy_ret8"]
    else:
        for cname in ("spy_ret4", "spy_ret8", "spy_above_vwap", "rs_spy"):
            df[cname] = 0.0
    df["ticker"] = t
    return df


def build_spy() -> pd.DataFrame | None:
    if not os.path.exists(os.path.join(CACHE, "SPY_15m.csv")):
        return None
    s = load15("SPY")
    s["date"] = s["ts"].dt.date
    tp = (s["High"] + s["Low"] + s["Close"]) / 3
    vw = (tp * s["Volume"]).groupby(s["date"]).cumsum() / \
        s["Volume"].groupby(s["date"]).cumsum()
    s["spy_ret4"] = s["Close"].pct_change(4)
    s["spy_ret8"] = s["Close"].pct_change(8)
    s["spy_above_vwap"] = (s["Close"] > vw).astype(float)
    return s


# ---------------------------------------------------------------- simulation

def simulate(df: pd.DataFrame) -> pd.DataFrame:
    """Exact lifecycle for an alert placed at every bar. Causal: the level and
    all features come from bars <= t; fills and exits are read from t+1 on."""
    n = len(df)
    hi = df["High"].to_numpy()
    lo = df["Low"].to_numpy()
    cl = df["Close"].to_numpy()
    atr = df["atr15"].to_numpy()
    lvl = df["level"].to_numpy()
    same_day = df["date"].to_numpy()

    filled = np.zeros(n, bool)
    fill_bar = np.full(n, -1)
    outcome = np.full(n, "none", dtype=object)
    ret_pct = np.full(n, np.nan)
    bars_held = np.full(n, np.nan)

    for i in range(n):
        p = lvl[i]
        a = atr[i]
        if not np.isfinite(p) or not np.isfinite(a) or a <= 0:
            continue
        # the level must still be above the market to be a breakout buy
        if p <= cl[i]:
            continue
        f = -1
        for k in range(i + 1, min(i + 1 + FILL_BARS, n)):
            if same_day[k] != same_day[i]:
                break                      # no overnight carry of the order
            if hi[k] >= p:
                f = k
                break
        if f < 0:
            continue
        filled[i] = True
        fill_bar[i] = f
        entry = p * (1 + SLIP_BPS / 1e4)   # pay up to get filled on a stop
        stop = p * (1 - SLIP_BPS / 1e4) - STOP_BUFFER_ATR * a
        tgt = p + TARGET_ATR * a
        res, r, held = "timeout", np.nan, 0
        # start the bar AFTER the fill. On the fill bar price travelled up
        # THROUGH p, so that bar's low predates the entry; reading it as a
        # post-entry stop would scratch essentially every trade.
        for k in range(f + 1, min(f + 1 + HOLD_BARS, n)):
            if same_day[k] != same_day[i]:
                k -= 1
                break
            held = k - f
            # pessimistic ordering: if a bar spans both, assume the stop first
            if lo[k] <= stop:
                res, r = "scratch", (stop / entry - 1) * 100
                break
            if hi[k] >= tgt:
                res, r = "target", (tgt * (1 - SLIP_BPS / 1e4) / entry - 1) * 100
                break
        if res == "timeout":
            kk = min(f + HOLD_BARS, n - 1)
            if kk <= f:
                out_skip = True
            while kk > f and same_day[kk] != same_day[i]:
                kk -= 1
            r = (cl[kk] * (1 - SLIP_BPS / 1e4) / entry - 1) * 100
            held = kk - f
        outcome[i] = res
        ret_pct[i] = r
        bars_held[i] = held

    out = df.copy()
    out["filled"] = filled
    out["fill_bar"] = fill_bar
    out["outcome"] = outcome
    out["ret_pct"] = ret_pct
    out["bars_held"] = bars_held
    out["won"] = (out["outcome"] == "target").astype(float)
    return out


def panel(tickers: list[str] | None = None) -> pd.DataFrame:
    spy = build_spy()
    tk = tickers or tickers_available()
    frames = []
    for t in tk:
        try:
            d = build(t, spy)
        except Exception:  # noqa: BLE001
            continue
        if d is None:
            continue
        frames.append(simulate(d))
    if not frames:
        raise RuntimeError("no intraday cache - run research/cache_intraday.py")
    p = pd.concat(frames, ignore_index=True)
    # only bars that actually produced a resting order and a fill are trades;
    # bars that produced an order but no fill are kept for the fill model
    p = p[np.isfinite(p["atr15"]) & np.isfinite(p["level"])
          & (p["level"] > p["Close"])].reset_index(drop=True)
    return p.dropna(subset=[c for c in FEATS if c in p.columns]
                    ).reset_index(drop=True)


# --------------------------------------------------------------- modelling

def wf_model(p: pd.DataFrame, target: str, subset: np.ndarray | None = None,
             n_folds: int = 4, model: str = "gbm", seed: int = 0
             ) -> pd.DataFrame:
    """Purged walk-forward by DATE. A 15m label resolves within the session,
    so a one-day embargo is sufficient and is applied."""
    d = p if subset is None else p[subset]
    d = d.sort_values("ts").reset_index(drop=True)
    days = np.array(sorted(d["date"].unique()))
    if len(days) < 20:
        raise RuntimeError("not enough sessions")
    edges = np.linspace(int(len(days) * 0.5), len(days), n_folds + 1).astype(int)
    x = d[FEATS].to_numpy(dtype=float)
    y = d[target].to_numpy(dtype=float)
    dd = d["date"].to_numpy()
    out = []
    for k in range(n_folds):
        lo_i, hi_i = edges[k], edges[k + 1]
        if hi_i - lo_i < 2:
            continue
        test_days = set(days[lo_i:hi_i])
        train_days = set(days[:max(0, lo_i - 1)])       # 1-day embargo
        tr = np.array([x_ in train_days for x_ in dd])
        te = np.array([x_ in test_days for x_ in dd])
        if tr.sum() < 500 or te.sum() < 50 or len(np.unique(y[tr])) < 2:
            continue
        tri = np.flatnonzero(tr)
        ncal = max(50, int(len(tri) * 0.2))
        fit_i, cal_i = tri[:-ncal], tri[-ncal:]
        m = (StumpGBM(seed=seed, min_leaf=max(40, int(len(fit_i) * 0.02)))
             if model == "gbm" else LogitL2(lam=6.0))
        m.fit(x[fit_i], y[fit_i])
        cal = Calibrator().fit(m.predict_proba(x[cal_i]), y[cal_i])
        ch = d.loc[te].copy()
        ch["p"] = cal.transform(m.predict_proba(x[te]))
        ch["fold"] = k
        out.append(ch)
    if not out:
        raise RuntimeError("no usable folds")
    return pd.concat(out, ignore_index=True)


def perm_p(res: pd.DataFrame, col: str, q: float, n_perm: int = 2000,
           seed: int = 0) -> float:
    r = res.sort_values("ts").reset_index(drop=True)
    pv = r["p"].to_numpy()
    y = r[col].to_numpy(dtype=float)
    n = len(r)
    obs = float(y[pv >= np.quantile(pv, q)].mean())
    rng = np.random.default_rng(seed)
    losh = max(10, n // 50)
    hits = 0
    for _ in range(n_perm):
        ps = np.roll(pv, int(rng.integers(losh, n - losh)))
        sel = y[ps >= np.quantile(ps, q)]
        if len(sel) and sel.mean() >= obs:
            hits += 1
    return hits / n_perm


def report(p: pd.DataFrame) -> None:
    fills = p[p["filled"]].copy()
    print(f"alerts (bars with a resting buy stop above market): {len(p):,}")
    print(f"sessions {p['date'].nunique()}  tickers {p['ticker'].nunique()}  "
          f"{p['ts'].min().date()} -> {p['ts'].max().date()}")
    print(f"\nFILL: {p['filled'].mean():.1%} of orders fill within "
          f"{FILL_BARS} bars ({FILL_BARS*15/60:.1f}h)")
    oc = fills["outcome"].value_counts(normalize=True)
    print(f"\nGIVEN A FILL (n={len(fills):,}), within {HOLD_BARS} bars "
          f"({HOLD_BARS*15/60:.0f}h):")
    for k in ("target", "scratch", "timeout"):
        print(f"   {k:<8} {oc.get(k, 0):6.1%}")
    print(f"\nreturn per FILLED trade, net of {SLIP_BPS:.0f}bps/side:")
    print(f"   mean   {fills['ret_pct'].mean():+.4f}%   "
          f"median {fills['ret_pct'].median():+.4f}%")
    for k in ("target", "scratch", "timeout"):
        s = fills[fills["outcome"] == k]["ret_pct"]
        if len(s):
            print(f"   {k:<8} mean {s.mean():+.4f}%  n={len(s):,}")
    lo, hi = date_block_bootstrap(fills["date"].to_numpy(),
                                  fills["ret_pct"].to_numpy(), 2000)
    print(f"   95% CI on the mean: [{lo:+.4f}%, {hi:+.4f}%]")

    tgt = fills[fills.outcome == "target"]["ret_pct"].mean()
    scr = fills[fills.outcome == "scratch"]["ret_pct"].mean()
    if np.isfinite(tgt) and np.isfinite(scr) and tgt > scr:
        be = (0 - scr) / (tgt - scr)
        print(f"\n   break-even target rate at this cost = {be:.1%}"
              f"   (actual {oc.get('target', 0):.1%})")


def report_gated(res: pd.DataFrame, label: str) -> None:
    base = res["ret_pct"].mean()
    print(f"\n{label}: out-of-sample n={len(res):,}  "
          f"AUC(win)={auc(res['won'].to_numpy(), res['p'].to_numpy()):.4f}")
    rows = []
    for q in (0.0, 0.5, 0.7, 0.8, 0.9, 0.95):
        s = res if q == 0 else res[res["p"] >= res["p"].quantile(q)]
        if len(s) < 20:
            continue
        lo, hi = date_block_bootstrap(s["date"].to_numpy(),
                                      s["ret_pct"].to_numpy(), 1500)
        rows.append({"gate": "all" if q == 0 else f"p>=q{int(q*100)}",
                     "trades": len(s),
                     "target_rate": round(float((s.outcome == "target").mean()), 3),
                     "scratch_rate": round(float((s.outcome == "scratch").mean()), 3),
                     "mean_ret_%": round(float(s["ret_pct"].mean()), 4),
                     "CI95": f"[{lo:+.4f}, {hi:+.4f}]",
                     "vs_all_pp": round(float(s["ret_pct"].mean() - base), 4)})
    print(pd.DataFrame(rows).to_string(index=False))
    for q in (0.8, 0.9):
        print(f"   permutation p at q{int(q*100)} = "
              f"{perm_p(res, 'ret_pct', q):.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--model", default="gbm", choices=["gbm", "logit"])
    ap.add_argument("--tickers", default="")
    a = ap.parse_args()
    tk = [t.strip().upper() for t in a.tickers.split(",") if t.strip()] or None
    p = panel(tk)

    if a.report or not a.live:
        report(p)
        fills = p[p["filled"]].copy()
        res = wf_model(fills, "won", model=a.model)
        report_gated(res, "CONTINUATION model (rank fills by P(target))")
        res.to_csv(os.path.join(ROOT, "research",
                                "intraday_oos.csv"), index=False)
        print("\nwrote research/intraday_oos.csv")

    if a.live:
        last = p["ts"].max()
        today = p[p["ts"].dt.date == last.date()]
        print(f"\nlatest bar in cache: {last}")
        print(f"bars today: {len(today)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
