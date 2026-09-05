"""Next-trading-day rehearsal: scan the pool with the finalized algo (algo.json).

Usage:
    source .venv/bin/activate
    python scanner/rehearse.py

Reads backtest/cache_sp500.pkl (refresh via combos.py --refresh if stale),
scores the LAST completed bar per ticker for primary + secondary variants,
prints (a) TRIGGERED setups with reference entry/stop and (b) BUILDING
watchlist (breakout within 0.5 ATR of the 20d high, ADX>=20, no trigger yet).
Reference prices come from the last bar; the pre-market gate (scanner/ROUTINE.md)
must still confirm before any alert fires. Paper only.
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from combos import level_of, risk_of, signal_mask  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    with open(os.path.join(ROOT, "algo.json")) as f:
        algo = json.load(f)
    with open(os.path.join(ROOT, "backtest", "cache_sp500.pkl"), "rb") as f:
        frames = pickle.load(f)

    variants = [(v["strategy"], 1 if v["side"] == "long" else -1,
                 v["filters"]["adx_min"], v["name"], v["grade"])
                for v in algo["variants"]]
    print(f"algo {algo['algo']} | {len(frames)} tickers | "
          f"last bar {max(f.index[-1].date().isoformat() for f in frames.values())}")
    trig, building = [], []
    for t, f in frames.items():
        i = len(f) - 1
        c = float(f["Close"].iloc[i])
        a = float(f["atr"].iloc[i])
        adx = float(f["adx"].iloc[i])
        for s, d, ax, name, grade in variants:
            m = signal_mask(f, s, d, ax)
            if m[i]:
                entry = c  # reference; real fill = next open per gate
                risk = risk_of(f, s, d, i)
                trig.append((t, name, grade, entry, entry - risk * d, adx))
        # building: breakout long within 0.5 ATR under the high, strong trend
        hi20 = float(f["hi20"].iloc[i])
        if np.isfinite(hi20) and adx >= 20 and 0 < (hi20 - c) <= 0.5 * a \
                and c > float(f["sma200"].iloc[i]):
            building.append((t, round(hi20, 2), round(adx, 1)))

    print(f"\n--- TRIGGERED ({len(trig)}) ---")
    for t, name, grade, e, s, adx in sorted(trig):
        print(f"{t} {name}[{grade}] ref_entry {e:.2f} ref_stop {s:.2f} "
              f"risk {e - s:.2f} ADX {adx:.0f} -> trail, no fixed target")
    print(f"\n--- BUILDING ({len(building)}) ---")
    for t, lvl, adx in sorted(building)[:30]:
        print(f"{t} 20d-high {lvl} ADX {adx} (trigger if close-through + RVOL)")
    if not trig and not building:
        print("(quiet tape: nothing triggered, nothing building)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
