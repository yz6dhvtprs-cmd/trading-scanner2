"""Local rotating bar cache for the full spy+qqq pool (1d/1w/1h).

Completed bars don't change, so re-downloading history for 517 names on
every run is pure waste (and what got us throttled). Instead: one full
bootstrap per ticker per timeframe, then small delta merges; scans
compute EMA/RSI/ATR off local CSVs. The 1d cache feeds the live 15-min
loop (refresh once a day); 1w/1h feed weekend/consolidation work.

Rotation: each ticker keeps the newest `keep` bars per timeframe; older
rows are dropped on every store. The still-forming bar is never stored:
today's daily bar, this week's weekly bar, this hour's hourly bar.
Volume is stored for future participation filters.

Corporate actions (splits/dividends rewrite adjusted history): when the
fresh overlap disagrees with cache by more than RESYNC_PCT on the same
stamp, the ticker is re-bootstrapped from the full period instead.

Usage:
    python scanner/daily_cache.py --bootstrap            # 1d fill
    python scanner/daily_cache.py --tf 1w --bootstrap    # weekly fill
    python scanner/daily_cache.py --tf 1h --refresh      # hourly delta
    python scanner/daily_cache.py --tf 1d --status       # coverage
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")


def _strip_tz(idx):
    """Hourly bars arrive tz-aware (ET); cache works in naive ET-wall
    time so cutoffs compare cleanly. (A PT-wall cutoff would eat most of
    the session — 10:00 PT is 13:00 ET.)"""
    idx = pd.to_datetime(idx)
    if getattr(idx, "tz", None) is not None:
        try:
            idx = idx.tz_convert(ET)
        except Exception:
            pass
        idx = idx.tz_localize(None)
    return idx


def _et_wall(now):
    """Now as naive ET-wall time. Naive inputs are already ET-wall;
    aware ones convert."""
    if isinstance(now, dt.datetime) and now.tzinfo is not None:
        return now.astimezone(ET).replace(tzinfo=None)
    return now

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple_scan import batched_download, load_pool  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_ROOT = os.path.join(ROOT, "scanner")
TFS = {
    # dir/keep/min-bars/delta-period/full-period/download-chunk
    "1d": {"dir": "daily_cache", "keep": 400, "min": 200,
           "delta": "5d", "full": "1y", "chunk": 100, "interval": "1d"},
    "1w": {"dir": "weekly_cache", "keep": 200, "min": 100,
           "delta": "3mo", "full": "2y", "chunk": 100, "interval": "1wk"},
    "1h": {"dir": "hourly_cache", "keep": 1000, "min": 500,
           "delta": "5d", "full": "6mo", "chunk": 50, "interval": "1h"},
}
CACHE_DIR = os.path.join(CACHE_ROOT, TFS["1d"]["dir"])  # 1d compat alias
META = os.path.join(CACHE_DIR, "meta.json")
RESYNC_PCT = 2.0       # same-stamp close drift above this => split/rewrite


def _dir(tf: str = "1d") -> str:
    return os.path.join(CACHE_ROOT, TFS[tf]["dir"])


def _meta(tf: str = "1d") -> str:
    return os.path.join(_dir(tf), "meta.json")


def _path(t: str, tf: str = "1d") -> str:
    return os.path.join(_dir(tf), t.replace("/", "_") + ".csv")


def _cutoff(tf: str, today):
    """Forming-bar boundary: cache keeps stamps strictly before it. 1d ->
    today; 1w -> this Monday; 1h -> this ET hour (a date means midnight)."""
    if isinstance(today, dt.datetime):
        day = _et_wall(today).date()
    else:
        day = today
    if tf == "1w":
        return day - dt.timedelta(days=day.weekday())
    if tf == "1h":
        now = today if isinstance(today, dt.datetime) else dt.datetime(
            today.year, today.month, today.day)
        return _et_wall(now).replace(minute=0, second=0, microsecond=0)
    return day


def load(t: str, tf: str = "1d") -> pd.DataFrame:
    """Cached frame for one ticker/timeframe (ascending, capitalized
    columns). Empty frame when nothing is cached yet."""
    try:
        f = pd.read_csv(_path(t, tf), parse_dates=["Date"],
                        index_col="Date")
    except Exception:
        return pd.DataFrame()
    f.columns = [str(c).capitalize() for c in f.columns]
    if tf == "1h":
        f.index = _strip_tz(f.index)
    else:
        f.index = pd.to_datetime(f.index).normalize()
    return f.sort_index()


def _extract(px, t: str) -> pd.DataFrame:
    """One ticker's OHLC frame out of a batch download (KeyError when the
    ticker came back empty, so the caller can count it missing)."""
    f = px[t] if isinstance(px.columns, pd.MultiIndex) else px
    return f.dropna(subset=["Close"]).copy()


def _norm(f: pd.DataFrame, tf: str = "1d") -> pd.DataFrame:
    f = f.copy()
    if isinstance(f.columns, pd.MultiIndex):
        top = f.columns.get_level_values(0).unique()
        if len(top) == 1:
            f = f[top[0]]
        else:  # defensive: per-ticker stores never hit this
            f.columns = f.columns.get_level_values(-1)
    f.columns = [str(c).capitalize() for c in f.columns]
    if tf == "1h":
        f.index = _strip_tz(f.index).floor("h")
    else:
        f.index = pd.to_datetime(f.index).normalize()
    return f[~f.index.duplicated(keep="last")].sort_index()


def _completed(f: pd.DataFrame, tf: str, today) -> pd.DataFrame:
    """Drop the still-forming bar (and anything newer)."""
    co = _cutoff(tf, today)
    if tf == "1h":
        return f[f.index < co]
    return f[[d < co for d in f.index.date]]


def store(t: str, fresh: pd.DataFrame, replace: bool = False,
          keep: int = None, today=None, tf: str = "1d") -> int:
    """Merge `fresh` into cache (fresh wins on overlap), drop the forming
    bar and rows older than `keep`. Returns bars kept."""
    today = today or dt.date.today()
    keep = TFS[tf]["keep"] if keep is None else keep
    f = _completed(_norm(fresh, tf), tf, today)
    if not replace:
        old = load(t, tf)
        if len(old):
            f = pd.concat([old, f])
            f = f[~f.index.duplicated(keep="last")].sort_index()
    f = f.tail(keep)
    os.makedirs(_dir(tf), exist_ok=True)
    f.to_csv(_path(t, tf), index_label="Date")
    return len(f)


def _col(f: pd.DataFrame, name: str) -> pd.Series:
    c = f[name]
    if isinstance(c, pd.DataFrame):
        c = c.iloc[:, 0]
    return c.astype(float)


def _needs_resync(old: pd.DataFrame, fresh: pd.DataFrame,
                  pct: float = RESYNC_PCT, today=None,
                  tf: str = "1d") -> bool:
    """True if fresh rewrites history vs cache (split/dividend), i.e. the
    same stamps now print different adjusted closes."""
    today = today or dt.date.today()
    try:
        f = _completed(_norm(fresh, tf), tf, today)
        common = old.index.intersection(f.index)
        if len(common) == 0:
            return False
        a, b = _col(old.loc[common], "Close"), _col(f.loc[common], "Close")
        denom = a.replace(0.0, float("nan")).fillna(1.0)
        return bool((((a - b).abs() / denom * 100) > pct).any())
    except Exception:
        return False


def refresh(tickers: list, period: str = None, downloader=None,
            today=None, tf: str = "1d") -> dict:
    """Small delta merge for every ticker (gap-fill + new bars). Tickers
    whose history was rewritten are re-bootstrapped from the full period
    instead."""
    today = today or dt.date.today()
    cfg = TFS[tf]
    period = cfg["delta"] if period is None else period
    tickers = list(dict.fromkeys(tickers))
    stats: dict = {"ok": [], "missing": [], "resynced": []}
    try:
        px = batched_download(tickers, period=period,
                              interval=cfg["interval"],
                              downloader=downloader, chunk=cfg["chunk"])
    except Exception:
        px = None
    if px is None:
        stats["missing"] = list(tickers)
        return stats
    resync = []
    for t in tickers:
        try:
            f = _extract(px, t)
        except Exception:
            stats["missing"].append(t)
            continue
        if len(f) == 0:
            stats["missing"].append(t)
            continue
        old = load(t, tf)
        if len(old) and _needs_resync(old, f, today=today, tf=tf):
            resync.append(t)
            continue
        store(t, f, today=today, tf=tf)
        stats["ok"].append(t)
    if resync:
        try:
            full = batched_download(resync, period=cfg["full"],
                                    interval=cfg["interval"],
                                    downloader=downloader,
                                    chunk=cfg["chunk"])
        except Exception:
            full = None
        for t in resync:
            try:
                f = _extract(full, t) if full is not None else pd.DataFrame()
            except Exception:
                f = pd.DataFrame()
            if len(f):
                store(t, f, replace=True, today=today, tf=tf)
                stats["resynced"].append(t)
            else:
                stats["missing"].append(t)
    return stats


def ensure(tickers: list, min_bars: int = None, downloader=None,
           today=None, tf: str = "1d") -> list:
    """Full-period bootstrap for tickers with no (or too-thin) cache.
    Returns the tickers actually filled."""
    today = today or dt.date.today()
    cfg = TFS[tf]
    min_bars = cfg["min"] if min_bars is None else min_bars
    thin = [t for t in dict.fromkeys(tickers)
            if len(load(t, tf)) < min_bars]
    if not thin:
        return []
    try:
        px = batched_download(thin, period=cfg["full"],
                              interval=cfg["interval"],
                              downloader=downloader, chunk=cfg["chunk"])
    except Exception:
        return []
    booted = []
    for t in thin:
        try:
            f = _extract(px, t) if px is not None else pd.DataFrame()
        except Exception:
            f = pd.DataFrame()
        if len(f):
            store(t, f, replace=True, today=today, tf=tf)
            booted.append(t)
    return booted


def coverage(tickers: list, min_bars: int = None,
             tf: str = "1d") -> dict:
    """Cache health: full / thin / missing counts (no network)."""
    min_bars = TFS[tf]["min"] if min_bars is None else min_bars
    full = thin = missing = 0
    for t in dict.fromkeys(tickers):
        n = len(load(t, tf))
        if n >= min_bars:
            full += 1
        elif n:
            thin += 1
        else:
            missing += 1
    return {"full": full, "thin": thin, "missing": missing,
            "tickers": len(set(tickers))}


def _read_stamp(tf: str = "1d"):
    try:
        with open(_meta(tf)) as f:
            return json.load(f).get("date")
    except Exception:
        return None


def _write_stamp(today, tf: str = "1d"):
    try:
        os.makedirs(_dir(tf), exist_ok=True)
        json.dump({"date": today.isoformat()
                   if isinstance(today, dt.date) else str(today)},
                  open(_meta(tf), "w"))
    except Exception:
        pass


def _stamp_today(today) -> str:
    if isinstance(today, dt.datetime):
        return today.date().isoformat()
    if isinstance(today, dt.date):
        return today.isoformat()
    return str(today)


def sync_pool(tickers: list, today=None, downloader=None,
              tf: str = "1d") -> dict:
    """Refresh+bootstrap once per day; later same-day calls are local-only.
    (For 1h the caller passes a datetime; the stamp is still daily.)"""
    today = today or dt.date.today()
    tickers = list(dict.fromkeys(tickers))
    if _read_stamp(tf) == _stamp_today(today):
        return {"refreshed": False, "ok": [], "missing": [],
                "resynced": [], "booted": [], **coverage(tickers, tf=tf)}
    ref = refresh(tickers, downloader=downloader, today=today, tf=tf)
    boot = ensure(tickers, downloader=downloader, today=today, tf=tf)
    _write_stamp(today, tf)
    out = {"refreshed": True, "booted": boot,
           **coverage(tickers, tf=tf)}
    out.update({k: ref[k] for k in ("ok", "missing", "resynced")})
    return out


def pool_tickers() -> list:
    """Full spy+qqq union, order-preserving and de-duplicated."""
    out, seen = [], set()
    for t in load_pool("spy") + load_pool("qqq"):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="1d", choices=sorted(TFS),
                    help="bar timeframe (default 1d)")
    ap.add_argument("--bootstrap", action="store_true",
                    help="full-period bootstrap for thin/missing tickers")
    ap.add_argument("--refresh", action="store_true",
                    help="delta merge for the whole pool")
    ap.add_argument("--status", action="store_true",
                    help="cache coverage report (no network)")
    a = ap.parse_args()
    pool = pool_tickers()
    print(f"pool: {len(pool)} names, tf={a.tf}, "
          f"cache: {_dir(a.tf)}", flush=True)
    if a.status or not (a.bootstrap or a.refresh):
        print(coverage(pool, tf=a.tf), flush=True)
    if a.refresh:
        s = refresh(pool, today=dt.date.today(), tf=a.tf)
        print(f"refresh: ok={len(s['ok'])} resynced={len(s['resynced'])} "
              f"missing={len(s['missing'])}", flush=True)
        if s["missing"]:
            print(f"  missing: {s['missing']}", flush=True)
    if a.bootstrap:
        b = ensure(pool, today=dt.date.today(), tf=a.tf)
        print(f"bootstrap: filled {len(b)}/{len(pool)}", flush=True)
        print(coverage(pool, tf=a.tf), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
