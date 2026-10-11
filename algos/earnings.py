"""Earnings dates for the funnel pool: a yfinance fetch behind a local
cache, plus the pure helpers the earnings veto needs.

A swing fill a few sessions before a report is a bet on the gap, not on
the setup, so the funnel wants to know, per name, how many trading
sessions sit between a signal and the next report. yfinance's
Ticker(t).earnings_dates returns ~25 rows per name (past reports plus the
next scheduled one) at about a second a call, so the pool is fetched
sequentially with a small pause and cached in data/earnings_cache.json
with a fetched stamp per name:

    {"NVDA": {"fetched": "2026-10-10", "dates": ["2025-08-27", ...]}}

A name is refetched only when its stamp is older than max_age_days. A
name whose fetch raised keeps the dates it already had (an empty list
otherwise), gets today's stamp and an error note, and is retried after
ERR_AGE_DAYS: one bad symbol never blocks the pool and never turns into a
retry storm. Dates are ET calendar dates; an after-close report (16:00
ET) moves the NEXT session, a pre-open one moves its own.

Pure helpers (no network, cache dict in): earnings_dates, next_earnings,
last_earnings, sessions_to_earnings (market_calendar.trading_gap
sessions, 0 = reports that day, None = unknown), earnings_between.

Usage:
    python algos/earnings.py --fetch                      # funnel pool
    python algos/earnings.py --fetch --ticker NVDA --force
    python algos/earnings.py --coverage                   # cache health
    python algos/earnings.py --ticker NVDA --ticker AMD   # next report
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from market_calendar import trading_gap  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(ROOT_DIR, "data", "earnings_cache.json")
MAX_AGE_DAYS = 7      # refetch a good entry when its stamp is older
ERR_AGE_DAYS = 1      # retry an empty (failed) entry after this many days
PAUSE = 0.3           # seconds between yfinance calls
FAR_DAYS = 400        # a date further out than this is suspicious
NEAR_DAYS = 20        # two dates closer than this are one report twice
ET = "America/New_York"


def _as_date(x) -> dt.date:
    """date | datetime | pandas Timestamp | 'YYYY-MM-DD...' -> date."""
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return dt.date.fromisoformat(str(x)[:10])


# ---------- fetch + cache ----------
def frame_dates(frame) -> list:
    """Sorted ISO ET calendar dates off a yfinance earnings_dates frame
    (tz-aware index, past and future). Exact duplicates and NaT rows are
    dropped; None or an empty frame -> []."""
    if frame is None or len(frame) == 0:
        return []
    idx = frame.index
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(ET)
    except Exception:
        pass
    out = set()
    for ts in idx:
        try:   # str() of a converted stamp starts YYYY-MM-DD; NaT raises
            out.add(dt.date.fromisoformat(str(ts)[:10]).isoformat())
        except Exception:
            continue
    return sorted(out)


def yf_dates(ticker: str) -> list:
    """The one network call: yfinance Ticker(t).earnings_dates -> ISO
    dates. Raises on any failure; fetch_earnings catches per ticker."""
    import yfinance as yf  # noqa: E402 (network-only import)
    return frame_dates(yf.Ticker(ticker).earnings_dates)


def load_cache(path: str = CACHE_PATH) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cache(cache: dict, path: str = CACHE_PATH) -> None:
    """One ticker per line (diffable), written via a temp file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = [f"{json.dumps(t)}: {json.dumps(cache[t], separators=(',', ':'))}"
             for t in sorted(cache)]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("{\n" + ",\n".join(lines) + "\n}\n")
    os.replace(tmp, path)


def stale(entry, today: dt.date, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """Refetch when the entry is missing, unstamped, older than
    max_age_days, or empty (a failed fetch) and ERR_AGE_DAYS or older."""
    if not isinstance(entry, dict) or not entry.get("fetched"):
        return True
    try:
        age = (today - dt.date.fromisoformat(str(entry["fetched"]))).days
    except Exception:
        return True
    if entry.get("dates"):
        return age > max_age_days
    return age >= min(ERR_AGE_DAYS, max_age_days)


def fetch_earnings(tickers, cache_path: str = CACHE_PATH,
                   max_age_days: int = MAX_AGE_DAYS, pause: float = PAUSE,
                   fetcher=None, today: dt.date = None, force: bool = False,
                   log=None) -> dict:
    """ticker -> sorted ISO dates for every name asked for: from the cache
    while the stamp is fresh, else one sequential yfinance call per name
    with `pause` seconds between calls. Every exception is caught per
    ticker (old dates kept, else an empty list, stamp + error note), so
    one bad symbol never blocks the pool. The cache file is rewritten
    after every network call: a killed run keeps its progress. `fetcher`
    and `today` are injectable for offline tests; `log` gets a progress
    line every 25 calls."""
    fetcher = fetcher or yf_dates
    today = today or dt.date.today()
    cache = load_cache(cache_path)
    out, calls, errs, t0 = {}, 0, 0, time.time()
    for t in tickers:
        t = str(t).upper()
        e = cache.get(t)
        if force or stale(e, today, max_age_days):
            if calls and pause > 0:
                time.sleep(pause)
            calls += 1
            try:
                dates = sorted(set(str(x)[:10] for x in fetcher(t)))
                cache[t] = {"fetched": today.isoformat(), "dates": dates}
            except Exception as ex:  # noqa: BLE001 - never block the pool
                errs += 1
                old = list(e.get("dates", [])) if isinstance(e, dict) else []
                cache[t] = {"fetched": today.isoformat(), "dates": old,
                            "error": f"{type(ex).__name__}: {ex}"[:120]}
            save_cache(cache, cache_path)
            if log and calls % 25 == 0:
                log(f"earnings: {calls} fetched, {errs} errors, "
                    f"{time.time() - t0:.0f}s")
        out[t] = list(cache[t].get("dates", []))
    if log:
        log(f"earnings: {len(out)} names, {calls} fetched, {errs} errors, "
            f"{time.time() - t0:.0f}s")
    return out


# ---------- pure helpers ----------
def earnings_dates(ticker: str, cache: dict) -> list:
    """Sorted datetime.dates for the name; [] when unknown. `cache` is the
    on-disk dict ({t: {fetched, dates}}) or fetch_earnings()'s {t: [iso]}."""
    e = cache.get(str(ticker).upper()) if isinstance(cache, dict) else None
    raw = e.get("dates", []) if isinstance(e, dict) else (
        e if isinstance(e, list) else [])
    out = set()
    for s in raw:
        try:
            out.add(_as_date(s))
        except Exception:
            continue
    return sorted(out)


def next_earnings(ticker: str, asof, cache: dict):
    """First cached date >= asof (a report that day counts), None when the
    name has no cached date that far out."""
    a = _as_date(asof)
    for d in earnings_dates(ticker, cache):
        if d >= a:
            return d
    return None


def last_earnings(ticker: str, asof, cache: dict):
    """Most recent cached date <= asof, None when unknown."""
    a = _as_date(asof)
    past = [d for d in earnings_dates(ticker, cache) if d <= a]
    return past[-1] if past else None


def sessions_to_earnings(ticker: str, asof, cache: dict):
    """Trading sessions from asof to the next report: 0 = reports that
    day, 1 = the next session (weekends and holidays don't count, per
    market_calendar.trading_gap). None when the next date is unknown:
    the caller decides, the funnel vetoes nothing on None."""
    nxt = next_earnings(ticker, asof, cache)
    if nxt is None:
        return None
    return trading_gap(_as_date(asof), nxt)


def earnings_between(ticker: str, start, end, cache: dict) -> bool:
    """True when a cached date falls inside [start, end], both ends
    inclusive. Unknown names are False (no date, not 'safe')."""
    s, e = _as_date(start), _as_date(end)
    return any(s <= d <= e for d in earnings_dates(ticker, cache))


# ---------- cache health ----------
def coverage(cache: dict, today: dt.date = None, tickers=None) -> dict:
    """For a pool: names with a date, names with a future date, dates
    more than FAR_DAYS out, pairs closer than NEAR_DAYS (one report
    listed twice), names with no dates, names whose last fetch failed."""
    today = today or dt.date.today()
    names = [str(t).upper() for t in (tickers or sorted(cache))]
    out = {"names": len(names), "with_dates": 0, "with_future": 0,
           "far": [], "near_dupes": [], "no_dates": [], "errors": []}
    for t in names:
        e = cache.get(t) if isinstance(cache, dict) else None
        if isinstance(e, dict) and e.get("error"):
            out["errors"].append(t)
        ds = earnings_dates(t, cache)
        if not ds:
            out["no_dates"].append(t)
            continue
        out["with_dates"] += 1
        if ds[-1] > today:
            out["with_future"] += 1
        for d in ds:
            if (d - today).days > FAR_DAYS:
                out["far"].append((t, d.isoformat()))
        for a, b in zip(ds, ds[1:]):
            if 0 < (b - a).days < NEAR_DAYS:
                out["near_dupes"].append((t, a.isoformat(), b.isoformat()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", action="append", default=[])
    ap.add_argument("--fetch", action="store_true",
                    help="refresh the cache for the funnel pool (or --ticker)")
    ap.add_argument("--force", action="store_true",
                    help="--fetch: refetch fresh entries too")
    ap.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--coverage", action="store_true",
                    help="cache health for the funnel pool (or --ticker)")
    ap.add_argument("--asof", default=None,
                    help="date for the --ticker report (default today)")
    a = ap.parse_args()
    today = dt.date.today()
    asof = dt.date.fromisoformat(a.asof) if a.asof else today
    tickers = named = [x.upper() for x in a.ticker]
    if (a.fetch or a.coverage) and not tickers:
        from funnel import funnel_pool  # noqa: E402
        tickers = funnel_pool()
    if a.fetch:
        fetch_earnings(tickers, max_age_days=a.max_age, pause=a.pause,
                       force=a.force, today=today,
                       log=lambda s: print(s, flush=True))
    cache = load_cache()
    if a.coverage:
        c = coverage(cache, today, tickers)
        print(f"earnings cache {CACHE_PATH}: {c['names']} names, "
              f"{c['with_dates']} with dates, {c['with_future']} with a "
              f"future date, {len(c['errors'])} fetch errors", flush=True)
        for k in ("no_dates", "errors"):
            if c[k]:
                print(f"  {k}: {' '.join(c[k])}", flush=True)
        for k in ("far", "near_dupes"):
            for row in c[k]:
                print(f"  {k}: {' '.join(row)}", flush=True)
        return 0
    for t in named:
        nxt, s = next_earnings(t, asof, cache), sessions_to_earnings(
            t, asof, cache)
        print(f"{t:6s} next {nxt or '-'} sessions "
              f"{s if s is not None else '-'} last "
              f"{last_earnings(t, asof, cache) or '-'} "
              f"({len(earnings_dates(t, cache))} dates cached)", flush=True)
    if not named and not a.fetch:
        ap.error("give --ticker, --fetch or --coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
