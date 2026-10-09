"""Hybrid RH-technicals driver: plan MCP calls, compare vs local cache.

Bulk pool bars stay on yfinance + daily_cache. Once an algo narrows the
pool to single names, this tool prints the exact (scalar-only, bridge-
safe) MCP call specs for server-computed technicals, then diffs the
fetched values against the local cache computation.

Usage (project root, venv active):
    python ops/rh_check.py --plan --preset intraday AAPL MSFT
        # agent executes the printed specs via MCP, saves results JSON
    python ops/rh_check.py --compare --results rh_results.json AAPL MSFT
        # local (cached-bar) vs remote (RH server) drift report

--compare is side-effect-free (never persists state, never texts), so it
is safe under --channels dry conventions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "algos"))

from rh_mcp import call_plan, compare, local_technicals  # noqa: E402
from rh_mcp import merge_technicals  # noqa: E402


def cmd_plan(tickers: list, preset: str, start: str) -> int:
    print(json.dumps(call_plan(tickers, preset, start_time=start),
                     indent=2))
    return 0


def _remote_dates(results: list) -> dict:
    out: dict = {}
    for res in results:
        data = res.get("data", res) if isinstance(res, dict) else {}
        t = str(data.get("symbol", "")).strip().upper()
        for ind in data.get("indicators", []) or []:
            for pt in ind.get("series", []) or []:
                b = str(pt.get("begins_at", ""))
                if b and b > out.get(t, ""):
                    out[t] = b
    return out


def cmd_compare(tickers: list, results_path: str, tol: float,
                out_path: str = None) -> int:
    from daily_cache import load  # noqa: E402 (cached bars, no network)

    with open(results_path) as f:
        results = json.load(f)
    if isinstance(results, dict):
        results = [results]
    merged = merge_technicals(results)
    dates = _remote_dates(results)
    bad = 0
    names = [s.strip().upper() for s in tickers if s.strip()]
    for t in dict.fromkeys(names):
        frame = load(t)
        remote = merged.get(t)
        if not len(frame):
            print(f"{t}: SKIP no cached bars")
            continue
        if not remote:
            print(f"{t}: MISS no RH result (re-run --plan output first)")
            bad += 1
            continue
        local = local_technicals(frame)
        rep = compare(local, remote, tol_pct=tol)
        keys = sorted(set(local) & set(remote))
        vals = " ".join(
            f"{k}={local[k]:.2f}/{remote[k]:.2f}" for k in keys
            if isinstance(local[k], float))
        flag = "OK" if rep["ok"] else f"DRIFT {rep['breaches']}"
        if not rep["ok"]:
            bad += 1
        print(f"{t}: local[{frame.index[-1].date()}] vs "
              f"RH[{dates.get(t, '?')[:10]}] {vals} -> {flag}")
    print(f"compared {len(names)} names, {bad} need attention "
          f"(tol {tol}%)")
    if out_path:
        doc = {"date": dt.date.today().isoformat(),
               "tickers": {t: merged[t] for t in dict.fromkeys(names)
                           if t in merged}}
        with open(out_path, "w") as f:
            json.dump(doc, f)
        print(f"wrote {len(doc['tickers'])} remote rows -> {out_path} "
              f"(stamped {doc['date']}; drift above still needs eyes)")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", action="store_true",
                    help="print MCP call-plan JSON for the tickers")
    ap.add_argument("--compare", action="store_true",
                    help="diff --results JSON against cached-bar values")
    ap.add_argument("--preset", default="intraday",
                    choices=["intraday", "weekly", "reversals", "full"])
    ap.add_argument("--start", default=None,
                    help="range start RFC3339 (default 90d lookback)")
    ap.add_argument("--results", default=None,
                    help="MCP results JSON file for --compare")
    ap.add_argument("--write", default=None, metavar="OVERRIDES_JSON",
                    help="with --compare, write stamped remote rows for "
                         "the intraday gate (data/rh_overrides.json)")
    ap.add_argument("--tol", type=float, default=2.0,
                    help="drift %% breaching the report")
    ap.add_argument("tickers", nargs="*")
    a = ap.parse_args()
    if a.plan:
        return cmd_plan(a.tickers, a.preset, a.start)
    if a.compare:
        if not a.results:
            print("--compare needs --results rh_results.json")
            return 2
        return cmd_compare(a.tickers, a.results, a.tol, a.write)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
