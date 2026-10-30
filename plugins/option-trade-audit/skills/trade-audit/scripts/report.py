"""Turn features.csv into a markdown report.

    python3 report.py                  # full, with dollars -> <CACHE>/ANALYSIS.md
    python3 report.py --sanitized -o LESSONS_TABLES.md

Sanitized mode drops every dollar column and keeps counts, win rate and profit
factor, so the tables can be shared or committed.
"""
import argparse, os, sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))
from paths import ANALYSIS_MD, FEATURES  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BOOLS = ["above_e10", "above_e20", "above_e50", "above_e200", "e10_gt_e20",
         "spy_above_e20", "spy_above_e50", "earn_in_hold"]

def load():
    F = pd.read_csv(FEATURES, parse_dates=["d0", "dx"])
    F["hold_min"] = pd.to_numeric(F.hold_min, errors="coerce")
    for c in BOOLS:
        if c in F:
            F[c] = F[c].astype(str).eq("True")
    F["year"] = F.d0.dt.year
    F["dte_c"] = np.select([F.dte_entry <= 1, F.dte_entry <= 7, F.dte_entry <= 30],
                           ["0-1", "2-7", "8-30"], "31+")
    F["trend"] = np.select([(F.above_e20 & F.above_e50), (~F.above_e20 & ~F.above_e50)],
                           ["up", "down"], "mixed")
    F["adds"] = np.where(F.n_open_orders == 1, "single",
                         np.where(F.avg_down > 0, "avg down", "add up"))
    F["bull"] = np.where(F.sign == 1, "bullish", "bearish")
    return F

def stats(g, money=True):
    w, l = g[g.pnl > 0], g[g.pnl <= 0]
    pf = w.pnl.sum() / -l.pnl.sum() if len(l) and l.pnl.sum() < 0 else np.inf
    out = {"n": len(g), "win%": round((g.pnl > 0).mean() * 100), "PF": round(pf, 2)}
    if money:
        out |= {"P&L": round(g.pnl.sum()), "avg": round(g.pnl.mean()),
                "avgW": round(w.pnl.mean()) if len(w) else 0,
                "avgL": round(l.pnl.mean()) if len(l) else 0}
    out["med hold (min)"] = round(g.hold_min.median(), 1)
    return pd.Series(out)

def table(F, by, title, money=True, sort=None, top=None):
    t = F.groupby(by, observed=True).apply(stats, money=money)
    if sort and sort in t:
        t = t.sort_values(sort)
    if top:
        t = t.head(top)
    t = t.reset_index()
    head = "| " + " | ".join(str(c) for c in t.columns) + " |"
    rule = "|" + "|".join(["---"] * len(t.columns)) + "|"
    body = "\n".join("| " + " | ".join(
        f"{v:.0f}" if isinstance(v, (int, float, np.integer, np.floating)) and float(v).is_integer()
        else (f"{v:.2f}" if isinstance(v, (float, np.floating)) else str(v))
        for v in row) + " |" for row in t.values)
    return f"\n### {title}\n\n{head}\n{rule}\n{body}\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sanitized", action="store_true")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    money = not a.sanitized
    out = a.out or ANALYSIS_MD
    F = load()
    cut = lambda s, e, lab: pd.cut(s, e, labels=lab)
    F["hour_b"] = cut(F.entry_hour, [0, 10, 11, 13, 15, 15.5, 24],
                      ["9:30-10", "10-11", "11-13", "13-15", "15-15:30", "15:30-16"])
    F["dow_n"] = F.dow.map({0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"})
    F["rsi_b"] = cut(F.rsi, [0, 30, 40, 50, 60, 70, 100], ["<30", "30-40", "40-50", "50-60", "60-70", ">70"])
    F["ext_b"] = cut(F.dist20_atr, [-99, -2, -1, 0, 1, 2, 99], ["<-2", "-2..-1", "-1..0", "0..1", "1..2", ">2"])
    F["vix_b"] = cut(F.vix, [0, 14, 18, 22, 30, 99], ["<14", "14-18", "18-22", "22-30", ">30"])
    F["otm_b"] = cut(F.otm_pct * 100, [-99, -3, -1, 0, 1, 3, 99],
                     ["ITM>3%", "ITM1-3", "ITM<1", "OTM<1", "OTM1-3", "OTM>3%"])
    F["seq_b"] = cut(F.seq_in_day, [0, 1, 3, 6, 99], ["1st", "2-3", "4-6", "7+"])
    F["after"] = np.select([F.prev_pnl < -200, F.prev_pnl < 0, F.prev_pnl > 0],
                           ["after loss>200", "after small loss", "after win"], "first")
    F["reentry_b"] = cut(F.min_since_prev_exit, [-1, 2, 10, 60, 1e9], ["<2m", "2-10m", "10-60m", ">1h"])
    F["wait_b"] = cut(F.entry_wait_s, [-1, 2, 30, 300, 1e9], ["instant", "2-30s", "30s-5m", ">5m"])
    F["spy_reg"] = np.select([(F.spy_above_e20 & F.spy_above_e50), (~F.spy_above_e20 & ~F.spy_above_e50)],
                             ["SPY up", "SPY down"], "SPY mixed")
    s = [f"# Option trade audit — {F.d0.min():%Y-%m-%d} to {F.d0.max():%Y-%m-%d}",
         "", f"{len(F)} closed positions"
         + (f", net {F.pnl.sum():,.0f}, win {100 * (F.pnl > 0).mean():.0f}%, "
            f"PF {F[F.pnl > 0].pnl.sum() / -F[F.pnl <= 0].pnl.sum():.2f}" if money
            else f", win {100 * (F.pnl > 0).mean():.0f}%, "
                 f"PF {F[F.pnl > 0].pnl.sum() / -F[F.pnl <= 0].pnl.sum():.2f}") + ".",
         "", "PF is gross wins over gross losses. Rows with fewer than ~20 trades are noise.",
         "", "## Structure"]
    s += [table(F, "dte_b", "Days to expiry at entry", money),
          table(F, "hold_b", "Hold time", money),
          table(F, "otm_b", "Moneyness at entry (proxy)", money),
          table(F, "cost_bucket", "Premium per position", money),
          table(F, ["bull", "trend"], "Direction vs the name's own EMA20/50 trend", money),
          table(F, ["bull", "spy_reg"], "Direction vs the SPY regime", money),
          table(F, ["bull", "rsi_b"], "Direction vs RSI14 at entry", money),
          table(F, ["bull", "ext_b"], "Direction vs distance from EMA20 (ATRs)", money),
          table(F, "vix_b", "VIX at entry", money)]
    s += ["## Behaviour",
          table(F, "adds", "Scaling: single entry, adds above entry, adds below entry", money),
          table(F, "seq_b", "n-th new position of the day (per account)", money),
          table(F, "after", "Previous position's outcome", money),
          table(F, ["after", "reentry_b"], "Re-entry speed after the previous exit", money),
          table(F, "wait_b", "Patience: order placed to first fill", money),
          table(F, "hour_b", "Entry time of day (ET)", money),
          table(F, "dow_n", "Day of week", money)]
    if "earn_ctx" in F:
        s += [table(F, "earn_ctx", "Earnings proximity", money)]
    s += ["## Slices",
          table(F, ["acct", "year"], "Account-year", money),
          table(F, "sym", "Symbols (most traded first)", money, top=20),
          table(F, ["dte_c", "bull"], "DTE class x direction", money)]
    s += ["", "## Notes", "",
          "- Underlying price at entry is a proxy: hourly interpolation where hourly bars exist, "
          "else the day's open (entries in the first 30 minutes) or close.",
          "- Expiries with no closing order are booked at zero.",
          "- Positions are flat-to-flat per contract, FIFO matched; adds and partial exits "
          "belong to one position.", ""]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w").write("\n".join(s))
    print("wrote", out, f"({'sanitized' if a.sanitized else 'with dollars'})")

if __name__ == "__main__":
    main()
