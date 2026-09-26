"""Monday screener: cap + RVOL gate over a broad US universe.

spy+qqq (517) is MASTER and never changes here. This module maintains a
separate adds list: candidates come from S&P 500 + Nasdaq-100 + Nasdaq
screener names >= $5B (≈ Russell-1000 breadth; iShares blocks scripted
R1000 pulls, so the screener's own $5B floor stands in).

Gates (calibrated 2026-09-26 to land 400-500 names):
  - market cap 5B..1000B (Nasdaq screener print, refreshed each run),
  - RVOL20 = Friday volume / 20-day mean in 1..10,
  - common equity only (drops ETFs via quoteType; -W/-U/-R suffixes).
Weekly rotation: names the screener added that fail this week are
removed; spy+qqq membership is never touched. Monday 6am PT run keeps
`screener_pool.csv` (the adds) + `screener_state.json` current; the
15-min loop and Friday scan read spy+qqq UNION the pool file.

Usage:
    python scanner/screener.py --calibrate   # RVOL window table
    python scanner/screener.py --run         # weekly add/remove update
    python scanner/screener.py --status      # pool counts (no network)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CAP_LO, CAP_HI = 5e9, 1e12
RVOL_N, RVOL_LO, RVOL_HI = 20, 1.0, 10.0
SP500_URL = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
             "/main/data/constituents.csv")
NDX_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
SCREENER_URL = ("https://api.nasdaq.com/api/screener/stocks?tableonly=true"
                "&limit=25&offset=0&download=true")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      "Accept": "application/json"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL_FILE = os.path.join(ROOT, "scanner", "screener_pool.csv")
STATE_FILE = os.path.join(ROOT, "scanner", "screener_state.json")
UNIVERSE_DIR = os.path.join(ROOT, "universe")


def yf_symbol(t: str) -> str:
    """Screener form -> yfinance form (BRK/A -> BRK-A)."""
    return t.replace("/", "-").replace(".", "-")


def drop_suffix(t: str) -> bool:
    """Warrant/unit/right stubs never qualify (BRK-B style survives)."""
    return t.endswith(("-W", "-U", "-R", "-WS"))


def parse_cap(s) -> float:
    m = re.match(r"([\d.]+)", str(s or "").replace(",", ""))
    return float(m.group(1)) if m else 0.0


NON_COMMON = re.compile(
    r"Preferred|Depositary Shares|\d+\.\d+%|Debenture|Notes due"
    r"|\bWarrants?\b|\bETFs?\b|\bETNs?\b", re.IGNORECASE)


def drop_name(name: str) -> bool:
    """Preferreds, notes, warrants, ETFs: fixed-income-like paper that
    never pages a stock setup (MLPs/REITs pass: operating companies)."""
    return bool(name and NON_COMMON.search(name))


def _read_url(url: str, parse):
    """Fetch bytes first: pandas rejects Request objects directly."""
    import io
    import urllib.request
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return parse(io.BytesIO(resp.read()))


def fetch_universe() -> dict:
    """Candidates with source tags: sp500 / ndx / screen>=5B."""
    tickers: dict = {}
    names: dict = {}

    def add(syms, src):
        for s in syms:
            s = str(s).strip().upper()
            if s and s != "NAN":
                tickers.setdefault(s, src)

    try:
        sp = _read_url(SP500_URL, pd.read_csv)
        add(sp["Symbol"].astype(str).str.replace(".", "-", regex=False),
            "sp500")
        print(f"sp500 leg ok", flush=True)
    except Exception as e:
        print(f"sp500 list failed ({e}), continuing", flush=True)
    try:
        def pick(buf):
            for tab in pd.read_html(buf):
                cols = [str(c).lower() for c in tab.columns]
                if any("ticker" in c or "symbol" in c for c in cols):
                    key = [c for c in tab.columns
                           if "icker" in str(c).lower()
                           or "ymbol" in str(c).lower()][0]
                    return tab[key].astype(str).str.strip()
            return []
        add(_read_url(NDX_URL, pick), "ndx")
        print(f"ndx leg ok", flush=True)
    except Exception as e:
        print(f"ndx list failed ({e}), continuing", flush=True)
    try:
        data = _read_url(SCREENER_URL, json.load)
        rows = data["data"]["rows"]
        caps = {}
        for r in rows:
            s = str(r.get("symbol", "")).strip().upper()
            c = parse_cap(r.get("marketCap"))
            names[s] = str(r.get("name", ""))
            if s and c >= 5e9:
                caps[s] = c
                tickers.setdefault(s, "screen")
        print(f"screener leg: {len(caps)} names >= $5B", flush=True)
        return {"tickers": tickers, "caps": caps, "names": names}
    except Exception as e:
        print(f"screener fetch failed ({e})", flush=True)
        return {"tickers": tickers, "caps": {}, "names": names}


def rvol(vol: pd.Series, n: int = RVOL_N) -> float:
    """Friday bar volume over the prior n-bar mean (NaN when unusable)."""
    v = vol.dropna()
    if len(v) < n + 1 or v.iloc[-n - 1:-1].mean() <= 0:
        return float("nan")
    return float(v.iloc[-1] / v.iloc[-n - 1:-1].mean())


def cap_pass(c: float) -> bool:
    return bool(c and CAP_LO <= c <= CAP_HI)


def rvol_pass(r: float) -> bool:
    return bool(r == r and RVOL_LO <= r <= RVOL_HI)


def is_equity(t: str) -> bool:
    """Common-stock check via quoteType (drops ETFs); suffix stubs out."""
    if drop_suffix(t):
        return False
    try:
        import yfinance as yf
        qt = yf.Ticker(yf_symbol(t)).fast_info.get("quoteType", "")
        if qt:
            return qt == "EQUITY"
        info = yf.Ticker(yf_symbol(t)).info or {}
        return info.get("quoteType", "EQUITY") == "EQUITY"
    except Exception:
        return True  # uncheckable: keep, RVOL gate still applies


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(state: dict):
    try:
        json.dump(state, open(STATE_FILE, "w"))
    except Exception:
        pass


def load_pool() -> list:
    """Current screener adds (tracked names passing this week)."""
    try:
        return pd.read_csv(POOL_FILE)["ticker"].astype(str).tolist()
    except Exception:
        return []


def scan_pool() -> list:
    """Live scan universe: spy+qqq master UNION screener adds."""
    from daily_cache import pool_tickers  # noqa: E402 (master untouched)
    out, seen = [], set()
    for t in pool_tickers() + load_pool():
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def rotate(tracked: dict, passed: set, week: str) -> tuple:
    """Weekly add/remove: new passers join (stamped), tracked names that
    fail this week leave. Returns (pool, added, removed). Pure logic,
    offline-testable."""
    pool, added, removed = [], [], []
    for t in sorted(passed):
        pool.append(t)
        if t not in tracked:
            added.append(t)
        tracked[t] = {"added": tracked.get(t, {}).get("added", week),
                      "last_pass": week}
    for t in sorted(tracked):
        if t not in passed:
            removed.append(t)
    for t in removed:
        del tracked[t]
    return pool, added, removed


def histories(tickers: list, days: int = 90) -> dict:
    """Recent daily volumes per ticker: pool names read the local cache,
    everyone else shares one chunked 3mo fetch (temp, uncached)."""
    from daily_cache import load  # noqa: E402
    from simple_scan import batched_download  # noqa: E402
    vols, missing = {}, [t for t in tickers
                         if len(load(yf_symbol(t), "1d")) < RVOL_N + 5]
    for t in tickers:
        f = load(yf_symbol(t), "1d")
        if len(f) >= RVOL_N + 5:
            vols[t] = f["Volume"]
    if missing:
        try:
            px = batched_download([yf_symbol(t) for t in missing],
                                  period="3mo", interval="1d")
        except Exception:
            px = None
        if px is not None:
            for t in missing:
                try:
                    v = (px[yf_symbol(t)] if isinstance(
                        px.columns, pd.MultiIndex) else px)["Volume"]
                    vols[t] = v.dropna()
                except Exception:
                    continue
    return vols


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibrate", action="store_true",
                    help="RVOL window table on this week's bar (report)")
    ap.add_argument("--run", action="store_true",
                    help="weekly add/remove update (Monday 6am slot)")
    ap.add_argument("--status", action="store_true",
                    help="pool counts (no network)")
    a = ap.parse_args()
    from daily_cache import pool_tickers  # noqa: E402 (master read-only)
    master = set(pool_tickers())
    if a.status or not (a.calibrate or a.run):
        pool = load_pool()
        print(f"master spy+qqq: {len(master)}, screener adds: "
              f"{len(pool)}, union: {len(master | set(pool))}",
              flush=True)
        return 0
    uni = fetch_universe()
    tickers = sorted(uni["tickers"])
    stamp = (dt.date.today().isoformat() + " "
             + ",".join(sorted({v for v in uni["tickers"].values()})))
    pd.DataFrame({"ticker": tickers,
                  "src": [uni["tickers"][t] for t in tickers]}).to_csv(
        os.path.join(UNIVERSE_DIR,
                     f"screener_universe_{dt.date.today()}.csv"),
        index=False)
    print(f"candidates: {len(tickers)} ({stamp})", flush=True)
    if a.calibrate:
        vols = histories(tickers)
        for n in (10, 20, 30):
            hits = [t for t in tickers
                    if cap_pass(uni["caps"].get(t, 0))
                    and rvol_pass(rvol(vols[t], n)) if t in vols]
            print(f"RVOL{n} in [{RVOL_LO:g},{RVOL_HI:g}]: {len(hits)}",
                  flush=True)
        return 0
    # --run: cap gate, RVOL gate on last completed bar, equity check on
    # qualifiers only, then rotate against tracked state.
    assert a.run
    vols = histories(tickers)
    today = dt.date.today()
    if today.weekday() == 0:  # Monday 6am: last Friday settles the week
        fri = today - dt.timedelta(days=3)
    elif today.weekday() < 5:
        fri = today
    else:
        fri = today - dt.timedelta(days=today.weekday() - 4)
    week = fri.isoformat()
    passed = set()
    for t in tickers:
        if not cap_pass(uni["caps"].get(t, 0)) or t not in vols:
            continue
        v = vols[t][vols[t].index.date <= fri]
        if rvol_pass(rvol(v)):
            passed.add(t)
    print(f"cap+rvol pass: {len(passed)}", flush=True)
    names = uni.get("names", {})
    common = sorted(t for t in passed if not drop_name(names.get(t, "")))
    print(f"name filter: {len(common)} "
          f"(dropped {len(passed) - len(common)} non-common)", flush=True)
    qualified = sorted(t for t in common if is_equity(t))
    print(f"equity check: {len(qualified)}", flush=True)
    adds_only = sorted(t for t in qualified if t not in master)
    state = load_state()
    tracked = state.get("names", {})
    pool, added, removed = rotate(tracked, set(adds_only), week)
    pd.DataFrame({"ticker": pool}).to_csv(POOL_FILE, index=False)
    save_state({"week": week, "names": tracked,
                "overlap": sorted(set(qualified) & master)})
    print(f"adds: {len(added)} removed: {len(removed)} "
          f"pool: {len(pool)} overlap: {len(set(qualified) & master)}",
          flush=True)
    if added:
        print("  + " + ", ".join(added[:30]), flush=True)
    if removed:
        print("  - " + ", ".join(removed[:30]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
