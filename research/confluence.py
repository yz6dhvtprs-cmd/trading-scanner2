"""Multi-timeframe level confluence: build levels on 1d/4h/1h, score them by
TEMPORAL independence, and time the entry on 15m.

The idea being tested, and the correction to it
-----------------------------------------------
The natural version of this is "a level is important if the daily, 4h and 1h
charts all show it". That version does not work, and not for a subtle reason:
nested timeframes are not independent evidence. A daily bar's high IS the max
of that day's 4h highs, which IS the max of its 1h highs. Cross-timeframe
agreement at a price is mostly arithmetic, so it would mark nearly everything.

What does carry information is TEMPORAL independence: how many separate,
time-separated events cluster at one price. A swing high from March, a swing
low from June and a heavy volume node from last week sitting inside a quarter
ATR of each other are three real observations. Two adjacent pivots from the
same swing are one. So the score below counts distinct events, deduplicated in
TIME, and treats the timeframe only as a weight on how much each event counts.

Causality
---------
Every pivot carries `known_at`, the bar at which its fractal was confirmed
(pivot bar + k bars). A decision at time T may only see pivots with
known_at <= T. Nothing else in the file reads forward.

    python research/confluence.py --ticker AAPL --backtest 1h
    python research/confluence.py --ticker AAPL --backtest 15m
    python research/confluence.py --ticker AAPL --live
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_D = os.path.join(ROOT, "research", "_cache")
CACHE_I = os.path.join(ROOT, "research", "_cache_intraday")

# how much a pivot from each timeframe contributes to a confluence score
TF_WEIGHT = {"1d": 3.0, "4h": 1.8, "1h": 1.0}
# two pivots at the same price are the SAME event unless separated by this
INDEP_HOURS = {"1d": 24 * 5, "4h": 24 * 3, "1h": 24 * 2}
FRACTAL_K = 3


# --------------------------------------------------------------- data loading

def load_daily(t: str) -> pd.DataFrame:
    p = os.path.join(CACHE_D, f"{t}_1d.csv")
    df = pd.read_csv(p, parse_dates=["Date"]).rename(columns={"Date": "ts"})
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize("America/New_York")
    return df.sort_values("ts").reset_index(drop=True)


def load_intra(t: str, interval: str) -> pd.DataFrame:
    p = os.path.join(CACHE_I, f"{t}_{interval}.csv")
    df = pd.read_csv(p, parse_dates=["ts"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(
        "America/New_York")
    return df.sort_values("ts").reset_index(drop=True)


def to_4h(h1: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h into 4h buckets anchored on the 09:30 session open."""
    s = h1.set_index("ts")
    o = s.resample("4h", origin="start_day", offset="9h30min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last",
         "Volume": "sum"}).dropna(subset=["Close"])
    return o.reset_index()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - df["Close"].shift()).abs(),
                    (df["Low"] - df["Close"].shift()).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# -------------------------------------------------------------------- pivots

def pivots(df: pd.DataFrame, tf: str, k: int = FRACTAL_K) -> pd.DataFrame:
    """Fractal swing highs/lows with the confirmation lag made explicit."""
    h, lo = df["High"], df["Low"]
    w = 2 * k + 1
    is_hi = h == h.rolling(w, center=True).max()
    is_lo = lo == lo.rolling(w, center=True).min()
    ts = df["ts"].to_numpy()
    rows = []
    for i in np.flatnonzero(is_hi.to_numpy()):
        j = min(i + k, len(df) - 1)
        rows.append({"price": float(h.iloc[i]), "side": "high", "tf": tf,
                     "at": ts[i], "known_at": ts[j]})
    for i in np.flatnonzero(is_lo.to_numpy()):
        j = min(i + k, len(df) - 1)
        rows.append({"price": float(lo.iloc[i]), "side": "low", "tf": tf,
                     "at": ts[i], "known_at": ts[j]})
    if not rows:
        return pd.DataFrame(columns=["price", "side", "tf", "at", "known_at"])
    return pd.DataFrame(rows).sort_values("known_at").reset_index(drop=True)


def all_pivots(t: str) -> pd.DataFrame:
    """Pivots from every timeframe, in one table, sorted by when they became
    knowable. Daily history is long; 1h/4h reach back about two years."""
    out = [pivots(load_daily(t), "1d")]
    p1 = os.path.join(CACHE_I, f"{t}_1h.csv")
    if os.path.exists(p1):
        h1 = load_intra(t, "1h")
        out.append(pivots(h1, "1h"))
        out.append(pivots(to_4h(h1), "4h"))
    p = pd.concat(out, ignore_index=True)
    p["known_at"] = pd.to_datetime(p["known_at"], utc=True).dt.tz_convert(
        "America/New_York")
    p["at"] = pd.to_datetime(p["at"], utc=True).dt.tz_convert(
        "America/New_York")
    return p.sort_values("known_at").reset_index(drop=True)


# --------------------------------------------------------------- confluence

def score_levels(av: pd.DataFrame, price: float, tol: float,
                 lo_mult: float = 0.05, hi_mult: float = 2.5,
                 atr_ref: float = 1.0) -> pd.DataFrame:
    """Score every candidate level sitting above `price`.

    A candidate is a pivot price. Its score is the weighted count of OTHER
    pivots within `tol`, after collapsing pivots that are the same event seen
    twice: same price band AND within INDEP_HOURS of each other on that
    timeframe count once.
    """
    if len(av) == 0:
        return pd.DataFrame()
    cand = av[(av["price"] > price + lo_mult * atr_ref)
              & (av["price"] < price + hi_mult * atr_ref)]
    if len(cand) == 0:
        return pd.DataFrame()
    prices = av["price"].to_numpy()
    tfs = av["tf"].to_numpy()
    times = (av["known_at"].dt.tz_localize(None).to_numpy()
             .astype("datetime64[h]").astype(np.int64))

    rows = []
    for lvl in np.unique(np.round(cand["price"].to_numpy(), 4)):
        near = np.abs(prices - lvl) <= tol
        if not near.any():
            continue
        idx = np.flatnonzero(near)
        # collapse repeats: greedily keep events separated in time per tf
        kept, score, tf_seen = [], 0.0, set()
        for i in sorted(idx, key=lambda z: times[z]):
            tf = tfs[i]
            gap = INDEP_HOURS.get(tf, 48)
            if any(tfs[j] == tf and abs(times[i] - times[j]) < gap
                   for j in kept):
                continue
            kept.append(i)
            score += TF_WEIGHT.get(tf, 1.0)
            tf_seen.add(tf)
        rows.append({"level": float(lvl), "score": round(score, 2),
                     "n_events": len(kept), "n_tf": len(tf_seen),
                     "tfs": "+".join(sorted(tf_seen)),
                     "dist_atr": round((lvl - price) / atr_ref, 3)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("score", ascending=False
                                          ).reset_index(drop=True)


def best_level(av: pd.DataFrame, price: float, tol: float, atr_ref: float,
               max_dist_atr: float = 2.5) -> dict | None:
    s = score_levels(av, price, tol, hi_mult=max_dist_atr, atr_ref=atr_ref)
    if len(s) == 0:
        return None
    return s.iloc[0].to_dict()


# --------------------------------------------------------------- simulation

def _sim_one(hi, lo, cl, i, buy, tgt, stop, fill_bars, hold_bars,
             day, intraday_only, slip_bps):
    """One resting buy-stop order placed at bar i. Returns dict or None."""
    n = len(hi)
    f = -1
    for k in range(i + 1, min(i + 1 + fill_bars, n)):
        if intraday_only and day[k] != day[i]:
            break
        if hi[k] >= buy:
            f = k
            break
    if f < 0:
        return {"filled": False, "outcome": "nofill", "ret_pct": 0.0,
                "fill_bar": -1, "bars_held": 0}
    entry = buy * (1 + slip_bps / 1e4)
    st = stop * (1 - slip_bps / 1e4)
    res, r, held = "timeout", np.nan, 0
    last = f
    # exit scan begins the bar AFTER the fill: on the fill bar price travelled
    # up through `buy`, so that bar's low predates entry.
    for k in range(f + 1, min(f + 1 + hold_bars, n)):
        if intraday_only and day[k] != day[i]:
            break
        last = k
        held = k - f
        if lo[k] <= st:                    # pessimistic: stop wins a tie
            res, r = "scratch", (st / entry - 1) * 100
            break
        if hi[k] >= tgt:
            res, r = "target", (tgt * (1 - slip_bps / 1e4) / entry - 1) * 100
            break
    if res == "timeout":
        r = (cl[last] * (1 - slip_bps / 1e4) / entry - 1) * 100
        held = last - f
    return {"filled": True, "outcome": res, "ret_pct": r, "fill_bar": f,
            "bars_held": held}


def backtest(ticker: str, exec_tf: str = "1h", min_score: float = 6.0,
             tol_atr: float = 0.25, fill_bars: int = 3, hold_bars: int = 8,
             target_atr: float = 1.0, buffer_atr: float = 0.05,
             max_dist_atr: float = 2.0, slip_bps: float = 3.0,
             intraday_only: bool = False, warmup: int = 200) -> pd.DataFrame:
    ex = load_intra(ticker, exec_tf)
    ex["atr_ex"] = atr(ex, 14)
    d = load_daily(ticker)
    d["atr_d"] = atr(d, 14)
    dmap = d.set_index(d["ts"].dt.date)["atr_d"]
    ex["atr_d"] = ex["ts"].dt.date.map(dmap).ffill()
    ex = ex.dropna(subset=["atr_ex", "atr_d"]).reset_index(drop=True)

    piv = all_pivots(ticker)
    pk = piv["known_at"].to_numpy()
    ts = ex["ts"].to_numpy()
    hi, lo, cl = (ex["High"].to_numpy(), ex["Low"].to_numpy(),
                  ex["Close"].to_numpy())
    day = ex["ts"].dt.date.to_numpy()
    aex, ad = ex["atr_ex"].to_numpy(), ex["atr_d"].to_numpy()

    rows = []
    for i in range(warmup, len(ex) - 1):
        cut = np.searchsorted(pk, ts[i], side="right")
        if cut < 20:
            continue
        av = piv.iloc[:cut]
        s = score_levels(av, cl[i], tol_atr * ad[i], hi_mult=max_dist_atr,
                         atr_ref=ad[i])
        if len(s) == 0:
            continue
        top = s.iloc[0]
        # matched control: a level at a comparable distance but weakest score
        near_dist = s[(s["dist_atr"] - top["dist_atr"]).abs() <= 0.35]
        ctrl = near_dist.iloc[-1] if len(near_dist) > 1 else None

        # true null: same distance from spot, but a price with NO pivot on it.
        # The weak-score control is still a pivot; this one is not, so it
        # separates "is a LEVEL special" from "is a breakout at distance d
        # profitable in a rising tape".
        rnd = {"level": cl[i] + float(top["dist_atr"]) * ad[i],
               "score": -1.0, "n_events": 0, "tfs": "random",
               "dist_atr": float(top["dist_atr"])}
        for tag, row in (("signal", top), ("control", ctrl), ("random", rnd)):
            if row is None:
                continue
            if tag == "signal" and row["score"] < min_score:
                continue
            buy = float(row["level"]) + buffer_atr * aex[i]
            r = _sim_one(hi, lo, cl, i, buy,
                         buy + target_atr * aex[i], float(row["level"]),
                         fill_bars, hold_bars, day, intraday_only, slip_bps)
            rows.append({"ts": ex["ts"].iloc[i], "kind": tag,
                         "close": round(cl[i], 2),
                         "level": round(float(row["level"]), 2),
                         "buy_price": round(buy, 2),
                         "stop": round(float(row["level"]), 2),
                         "target": round(buy + target_atr * aex[i], 2),
                         "score": float(row["score"]),
                         "n_events": int(row["n_events"]),
                         "tfs": row["tfs"], "dist_atr": float(row["dist_atr"]),
                         **r})
    return pd.DataFrame(rows)


def summarise(bt: pd.DataFrame, label: str) -> None:
    if len(bt) == 0:
        print(f"{label}: no rows")
        return
    fills = bt[bt["filled"]]
    print(f"\n{label}")
    print(f"  alerts {len(bt):,}   fill rate {bt['filled'].mean():.1%}"
          f"   filled {len(fills):,}")
    if len(fills) == 0:
        return
    oc = fills["outcome"].value_counts(normalize=True)
    print(f"  given fill:  target {oc.get('target',0):.1%}"
          f"   scratch {oc.get('scratch',0):.1%}"
          f"   timeout {oc.get('timeout',0):.1%}")
    m = fills["ret_pct"].mean()
    boot = np.array([fills["ret_pct"].sample(len(fills), replace=True,
                                             random_state=b).mean()
                     for b in range(1000)])
    print(f"  mean return per filled trade: {m:+.4f}%"
          f"   95% CI [{np.percentile(boot,2.5):+.4f}%, "
          f"{np.percentile(boot,97.5):+.4f}%]")
    print(f"  mean return per ALERT (no-fills as 0): "
          f"{bt['ret_pct'].mean():+.4f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--backtest", default="1h", choices=["1h", "15m"])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--min-score", type=float, default=6.0)
    ap.add_argument("--tol-atr", type=float, default=0.25)
    ap.add_argument("--target-atr", type=float, default=1.0)
    ap.add_argument("--max-dist-atr", type=float, default=2.0)
    ap.add_argument("--slip-bps", type=float, default=3.0)
    ap.add_argument("--show", type=int, default=25)
    a = ap.parse_args()
    t = a.ticker.upper()

    if a.backtest == "15m":
        fb, hb, intraday = 10, 12, True
    else:
        fb, hb, intraday = 3, 8, False

    print(f"=== {t} confluence backtest, execution on {a.backtest} ===")
    piv = all_pivots(t)
    print(f"pivot inventory: " + ", ".join(
        f"{k}={v}" for k, v in piv['tf'].value_counts().items())
        + f"   (total {len(piv):,}, "
        f"{piv['known_at'].min().date()} -> {piv['known_at'].max().date()})")

    bt = backtest(t, a.backtest, a.min_score, a.tol_atr, fb, hb,
                  a.target_atr, 0.05, a.max_dist_atr, a.slip_bps, intraday)
    if len(bt) == 0:
        print("no signals produced")
        return 1
    sig = bt[bt.kind == "signal"]
    ctl = bt[bt.kind == "control"]
    rnd = bt[bt.kind == "random"]
    print(f"\nwindow {bt['ts'].min()} -> {bt['ts'].max()}")
    print(f"min confluence score for a signal: {a.min_score}")
    summarise(sig, f"HIGH-CONFLUENCE signals (score >= {a.min_score})")
    summarise(ctl, "MATCHED CONTROL (same distance, weakest pivot score)")
    summarise(rnd, "RANDOM LEVEL (same distance, no pivot at all)")

    if len(sig[sig.filled]) and len(ctl[ctl.filled]):
        d = sig[sig.filled]["ret_pct"].mean() - ctl[ctl.filled]["ret_pct"].mean()
        rng = np.random.default_rng(0)
        s_, c_ = sig[sig.filled]["ret_pct"].to_numpy(), \
            ctl[ctl.filled]["ret_pct"].to_numpy()
        boot = np.array([rng.choice(s_, len(s_)).mean()
                         - rng.choice(c_, len(c_)).mean() for _ in range(2000)])
        print(f"\nCONFLUENCE EFFECT (signal minus matched control): {d:+.4f}%"
              f"   95% CI [{np.percentile(boot,2.5):+.4f}%, "
              f"{np.percentile(boot,97.5):+.4f}%]"
              f"   P(effect<=0)={float((boot<=0).mean()):.3f}")

    by = sig.copy()
    by["bucket"] = pd.cut(by["score"], [0, 6, 9, 12, 18, 100])
    g = by[by.filled].groupby("bucket", observed=True).agg(
        n=("ret_pct", "size"), target=("outcome", lambda x: (x == "target").mean()),
        mean_ret=("ret_pct", "mean")).round(4)
    print("\ndoes MORE confluence help? (filled trades by score bucket)")
    print(g.to_string())

    cols = ["ts", "close", "level", "buy_price", "stop", "target", "score",
            "n_events", "tfs", "dist_atr", "filled", "outcome", "ret_pct"]
    out = os.path.join(ROOT, "research", f"confluence_{t}_{a.backtest}.csv")
    sig[cols].to_csv(out, index=False)
    print(f"\nlast {a.show} signal timestamps (this is the alert feed):")
    print(sig[cols].tail(a.show).to_string(index=False))
    print(f"\nwrote {out}")

    if a.live:
        ex = load_intra(t, a.backtest)
        ex["atr_ex"] = atr(ex, 14)
        d = load_daily(t)
        d["atr_d"] = atr(d, 14)
        ad = float(d["atr_d"].iloc[-1])
        now = ex["ts"].iloc[-1]
        av = piv[piv["known_at"] <= now]
        s = score_levels(av, float(ex["Close"].iloc[-1]),
                         a.tol_atr * ad, hi_mult=a.max_dist_atr, atr_ref=ad)
        print(f"\n=== LIVE READ {t} @ {now} close "
              f"{ex['Close'].iloc[-1]:.2f} (daily ATR {ad:.2f}) ===")
        if len(s) == 0:
            print("no candidate level above price inside the window")
        else:
            s = s.head(6).copy()
            s["buy_price"] = (s["level"] + 0.05 * ex["atr_ex"].iloc[-1]).round(2)
            s["target"] = (s["buy_price"] + a.target_atr
                           * ex["atr_ex"].iloc[-1]).round(2)
            s["actionable"] = s["score"] >= a.min_score
            print(s.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
