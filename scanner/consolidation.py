"""Timeframe-agnostic consolidation detector (A+E gate, B confirm).

Gate A (range coil): (max High - min Low over the window) / ATR14
<= width_atr. The box is consolidating when N bars fit inside ~1 ATR.
Veto E (drift): |OLS slope of closes| <= slope_atr per bar AND every
bar's |residual| <= resid_atr. Rejects slow bleeds that pass A on
range alone (sinking into a line is distribution, not basing).
Confirm B (optional, never gates): stdev(closes) / mean <= b_pct.
Reported on every result so the alert line can cite it.

consolidation() evaluates the window ending at the last bar, then walks
the window back while the gate holds and reports the whole run:
boundaries (lower/upper over the run), width, length in bars, slope,
and the B flag. EMA-proximity (above EMA20/10 etc.) is deliberately
NOT part of this module — it is a strategy gate applied after, so the
detector stays reusable across algorithms and timeframes.

Usage:
    python scanner/consolidation.py --ticker NVDA --days 10
    python scanner/consolidation.py --ticker NVDA --days 20 --tf 1d,1w
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PAGE_ATR = 14  # Wilder ATR span, same formula as the live scanner


def atr14(h: pd.Series, l: pd.Series, c: pd.Series) -> pd.Series:
    """Wilder ATR14 (flat reads 0, first bar NaN -> backfilled)."""
    h, l, c = h.astype(float), l.astype(float), c.astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(),
                    (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / PAGE_ATR, adjust=False).mean().bfill()


def _ols(ys: list) -> tuple:
    """Slope per bar and max abs residual of the least-squares line."""
    n = len(ys)
    xs = list(range(n))
    mx, my = (n - 1) / 2.0, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    resid = max(abs(y - (my + slope * (x - mx))) for x, y in zip(xs, ys))
    return slope, resid


def window_gate(win: pd.DataFrame, atr: float, n: int, width_atr: float,
                slope_atr: float, resid_atr: float) -> dict:
    """A+E verdict on exactly one window (the caller's slice). `atr` is
    the warmed-up ATR value at the window end (computed on full history,
    not the window alone)."""
    closes = win["Close"].astype(float).tolist()
    slope, resid = _ols(closes)
    width = (float(win["High"].max()) - float(win["Low"].min()))
    stats = {"lower": float(win["Low"].min()),
             "upper": float(win["High"].max()),
             "width_atr": width / atr if atr > 0 else float("inf"),
             "slope_atr": slope / atr if atr > 0 else float("inf"),
             "resid_atr": resid / atr if atr > 0 else float("inf"),
             "b_pct": (statistics.pstdev(closes) / (sum(closes) / n)
                       * 100.0) if n > 1 else 0.0}
    stats["active"] = bool(
        atr > 0 and stats["width_atr"] <= width_atr
        and abs(stats["slope_atr"]) <= slope_atr
        and stats["resid_atr"] <= resid_atr)
    return stats


def consolidation(frame: pd.DataFrame, n: int = 4,
                  width_atr: float = 1.0, slope_atr: float = 0.10,
                  resid_atr: float = 0.50,
                  b_pct: float = None) -> dict:
    """Trailing consolidation ending at the last bar. Extends the window
    back while A+E holds and reports the whole run. Always includes the
    B reading; `b_tight` is set only when a `b_pct` threshold is given."""
    out = {"active": False, "lower": None, "upper": None,
           "width_atr": None, "bars": 0, "slope_atr": None,
           "b_pct": None, "b_tight": None}
    f = frame.dropna(subset=["Close", "High", "Low"])
    if len(f) < max(n, PAGE_ATR + 1):
        return out
    a = atr14(f["High"], f["Low"], f["Close"])
    run, bars = None, 0
    for k in range(n, len(f) + 1):
        win = f.iloc[-k:]
        g = window_gate(win, float(a.iloc[-1]), k, width_atr, slope_atr,
                        resid_atr)
        if not g["active"]:
            break
        run, bars = g, k
    if run is None:
        return out
    run["bars"] = bars
    run["active"] = True
    if b_pct is not None:
        run["b_tight"] = bool(run["b_pct"] <= b_pct)
    return run


def find_runs(frame: pd.DataFrame, n: int = 4, width_atr: float = 1.0,
              slope_atr: float = 0.10, resid_atr: float = 0.50,
              b_pct: float = None) -> list:
    """Every consolidation occurrence in the frame: fixed-n windows are
    scored at each bar-end, consecutive hits group into one run with the
    union box. Returns [{end, bars, lower, upper, ...}] ascending."""
    f = frame.dropna(subset=["Close", "High", "Low"])
    if len(f) < max(n, PAGE_ATR + 1):
        return []
    a = atr14(f["High"], f["Low"], f["Close"])
    hot = []
    for i in range(len(f)):
        if i + 1 < max(n, PAGE_ATR + 1):
            continue
        win = f.iloc[i + 1 - n:i + 1]
        g = window_gate(win, float(a.iloc[i]), n, width_atr, slope_atr,
                        resid_atr)
        if g["active"]:
            hot.append(i)
    runs, cur = [], []
    for i in hot:
        if cur and i > cur[-1] + 1:
            runs.append(_summarize(f, a, cur, n, width_atr, slope_atr,
                                   resid_atr, b_pct))
            cur = []
        cur.append(i)
    if cur:
        runs.append(_summarize(f, a, cur, n, width_atr, slope_atr,
                               resid_atr, b_pct))
    return runs


def _summarize(f, a, idxs, n, width_atr, slope_atr, resid_atr,
               b_pct) -> dict:
    seg = f.iloc[idxs[0] + 1 - n:idxs[-1] + 1]
    g = window_gate(seg, float(a.iloc[idxs[-1]]), len(seg), width_atr,
                    slope_atr, resid_atr)
    out = {"end": f.index[idxs[-1]], "bars": len(seg),
           "lower": g["lower"], "upper": g["upper"],
           "width_atr": round(g["width_atr"], 2),
           "slope_atr": round(g["slope_atr"], 3),
           "b_pct": round(g["b_pct"], 2), "b_tight": None}
    if b_pct is not None:
        out["b_tight"] = bool(g["b_pct"] <= b_pct)
    return out


def _fmt_when(end, tf: str) -> str:
    end = pd.Timestamp(end)
    if tf == "1h":
        return end.strftime("%m-%d %H:%M")
    if tf == "1w":
        return "week of " + end.strftime("%m-%d")
    return end.strftime("%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", action="append", default=[],
                    help="repeatable, e.g. --ticker NVDA")
    ap.add_argument("--days", type=int, default=10,
                    help="lookback window in calendar days")
    ap.add_argument("--tf", default="1h,1d,1w",
                    help="comma-separated timeframes")
    ap.add_argument("--n", type=int, default=4,
                    help="minimum consolidation bars")
    ap.add_argument("--width", type=float, default=1.0,
                    help="A gate: box width in ATRs")
    ap.add_argument("--slope", type=float, default=0.10,
                    help="E veto: slope per bar in ATRs")
    ap.add_argument("--resid", type=float, default=0.50,
                    help="E veto: max residual in ATRs")
    ap.add_argument("--b-pct", type=float, default=0.5,
                    help="B confirm threshold (close stdev %)")
    a = ap.parse_args()
    if not a.ticker:
        ap.error("give at least one --ticker")
    from daily_cache import ensure, load  # noqa: E402 (local bars)
    today = dt.date.today()
    floor = today - dt.timedelta(days=a.days)
    for t in [x.upper() for x in a.ticker]:
        for tf in [s.strip() for s in a.tf.split(",") if s.strip()]:
            ensure([t], today=today, tf=tf)  # single-ticker fill, cheap
            f = load(t, tf)
            if not len(f):
                print(f"{t} {tf}: no bars cached", flush=True)
                continue
            runs = [r for r in find_runs(
                f, a.n, a.width, a.slope, a.resid, a.b_pct)
                if r["end"].date() >= floor]
            if not runs:
                print(f"{t} {tf}: no consolidation in last "
                      f"{a.days}d", flush=True)
            for r in runs:
                b = (f"B {r['b_pct']}% "
                     f"{'tight' if r['b_tight'] else 'loose'}")
                print(f"{t} {tf} {_fmt_when(r['end'], tf)}, "
                      f"{r['bars']} bars, "
                      f"{r['lower']:.2f}\u2013{r['upper']:.2f}, "
                      f"w{float(r['width_atr']):.2f} ATR, {b}",
                      flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
