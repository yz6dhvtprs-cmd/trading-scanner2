"""Saturday review: live-vs-baseline check, grade demotions, regime + universe age.

Usage:
    source .venv/bin/activate
    python review/saturday.py

Reads backtest/results.csv (baseline), scanner/signal_log.csv (live),
writes review/REPORT-YYYY-MM-DD.md + review/grade_overrides.csv.
Safe with an empty log: reports baseline-only status.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "backtest", "results.csv")
LOG = os.path.join(ROOT, "scanner", "signal_log.csv")
OVERRIDES = os.path.join(ROOT, "review", "grade_overrides.csv")
GRADE_ORDER = ["C (paused)", "B", "B+", "A", "A+"]


def demote(g: str) -> str:
    base = g.split(" ")[0]
    i = GRADE_ORDER.index(base) if base in GRADE_ORDER else 1
    return GRADE_ORDER[max(0, i - 1)]


def main() -> int:
    today = dt.date.today().isoformat()
    lines = [f"# Saturday review {today}", ""]
    base = pd.read_csv(RESULTS)
    base_exp = base.groupby(["strategy", "side"])["test_expectancy_R"].mean()

    if os.path.exists(LOG):
        log = pd.read_csv(LOG)
    else:
        log = pd.DataFrame()
    closed = log[log["outcome_R"].notna()].copy() if len(log) else log

    overrides = []
    if closed.empty:
        lines.append("No closed live/paper trades yet. Baseline-only review.")
        lines.append("Nothing to demote. Next: paper-scan so the loop has data.")
    else:
        lines.append(f"Closed tracked trades: {len(closed)}")
        for (s, d), grp in closed.groupby(["strategy", "side"]):
            last = grp.tail(20)
            n = len(last)
            live_exp = last["outcome_R"].mean()
            bl = float(base_exp.get((s, d), 0.0))
            news_n = int(last["news_flag"].fillna("").astype(bool).sum()) \
                if "news_flag" in last else 0
            verdict = "HOLD"
            if n >= 8 and live_exp < 0:
                cur = base.loc[(base.strategy == s) & (base.side == d),
                               "grade"].mode()
                cur_g = str(cur.iloc[0]) if len(cur) else "B"
                new_g = demote(cur_g)
                overrides.append({"strategy": s, "side": d, "from": cur_g,
                                  "to": new_g, "date": today,
                                  "reason": f"live last-{n} expectancy "
                                            f"{live_exp:+.2f}R vs baseline {bl:+.2f}R"
                                  + (f"; {news_n} news-flagged" if news_n else "")})
                verdict = f"DEMOTE {cur_g} -> {new_g}"
            lines.append(f"- {s} {d}: n={n} live_exp={live_exp:+.2f}R "
                         f"baseline={bl:+.2f}R news={news_n} => {verdict}")

    # universe age
    try:
        uni = pd.read_csv(os.path.join(ROOT, "universe", "universe_live.csv"))
        snap = dt.date.fromisoformat(str(uni["snapshot_date"].iloc[0]))
        age = (dt.date.today() - snap).days
        lines.append(f"\nUniverse snapshot age: {age} days "
                     + ("— REBALANCE DUE (>95d)" if age > 95 else "(ok)"))
    except Exception as e:
        lines.append(f"\nUniverse check failed: {e}")

    # regime: SPY vs SMA200 + VIX
    try:
        import yfinance as yf
        px = yf.download(["SPY", "^VIX"], period="1y", auto_adjust=True,
                         progress=False, threads=True)
        spy = px["Close"]["SPY"].dropna()
        vix = px["Close"]["^VIX"].dropna()
        regime = "bull" if spy.iloc[-1] > spy.rolling(200).mean().iloc[-1] else "bear"
        lines.append(f"Regime: SPY {regime} (vs 200d), VIX {vix.iloc[-1]:.1f}")
        if vix.iloc[-1] > 30:
            lines.append("VIX > 30: halve size, spreads-only per LOOP.md.")
    except Exception as e:
        lines.append(f"Regime check failed: {e}")

    report = os.path.join(ROOT, "review", f"REPORT-{today}.md")
    with open(report, "w") as f:
        f.write("\n".join(lines) + "\n")
    pd.DataFrame(overrides).to_csv(OVERRIDES, index=False)
    print("\n".join(lines))
    print(f"\nwrote {report} + {OVERRIDES} ({len(overrides)} demotions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
