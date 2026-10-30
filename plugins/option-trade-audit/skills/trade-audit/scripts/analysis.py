import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from paths import CACHE, ACCTS, PX, PX_H1, EPISODES, FEATURES, LOTS  # noqa: E402
ROOT = CACHE
import pandas as pd, numpy as np, warnings; warnings.filterwarnings("ignore")
pd.set_option("display.width", 200); pd.set_option("display.max_columns", 30); pd.set_option("display.max_rows", 200)
F = pd.read_csv(f"{ROOT}/features.csv", parse_dates=["d0", "dx"])
F["t0"] = pd.to_datetime(F.t0, utc=True, format="ISO8601"); F["year"] = F.d0.dt.year
F["hold_min"] = F.hold_min.astype(float)
for c in ["above_e10","above_e20","above_e50","above_e200","e10_gt_e20","spy_above_e20","spy_above_e50"]: F[c] = F[c].astype(str).eq("True")
def stats(g):
    w = g[g.pnl > 0]; l = g[g.pnl <= 0]
    pf = w.pnl.sum() / -l.pnl.sum() if len(l) and l.pnl.sum() < 0 else np.inf
    return pd.Series(dict(n=len(g), pnl=g.pnl.sum(), win=(g.pnl > 0).mean(), avg=g.pnl.mean(), avg_ret=g.ret.mean(), pf=pf,
                          avgW=w.pnl.mean() if len(w) else 0, avgL=l.pnl.mean() if len(l) else 0, medhold=g.hold_min.median(), cost=g.open_cost.median()))
def tab(by, df=None, title=None, sort=False):
    df = F if df is None else df
    t = df.groupby(by, observed=True).apply(stats)
    if sort: t = t.sort_values("pnl")
    print(f"\n== {title or by} ==\n{t.round(2).to_string()}")
def bucket(s, edges, labels): return pd.cut(s, edges, labels=labels)
print("TOTAL", len(F), "pnl", F.pnl.sum().round(0), "win", (F.pnl > 0).mean().round(3), "avgW", F[F.pnl > 0].pnl.mean().round(0), "avgL", F[F.pnl <= 0].pnl.mean().round(0))
tab(["acct", "year"]); tab("otype"); tab("sign"); tab("dir"); tab("dte_b"); tab("hold_b"); tab("cost_bucket")
F["hour_b"] = bucket(F.entry_hour, [0, 10, 11, 13, 15, 15.5, 24], ["9:30-10", "10-11", "11-13", "13-15", "15-15:30", "15:30-16"]); tab("hour_b")
F["dow_n"] = F.dow.map({0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}); tab("dow_n")
top = F.sym.value_counts().head(14).index; tab("sym", F[F.sym.isin(top)], "top symbols", sort=True)
# --- technical context at entry (daily) ---
F["trend"] = np.select([(F.above_e20 & F.above_e50), (~F.above_e20 & ~F.above_e50)], ["up(>e20&e50)", "down(<e20&e50)"], "mixed")
F["aligned"] = np.where(F.sign == 1, F.above_e20, ~F.above_e20)
tab(["sign", "trend"], title="exposure x daily trend (prev close vs EMA20/50)")
tab(["dte_b", "aligned"], F[F.dte_b.isin(["0", "1", "2", "3-4"])], "short-dated: aligned with EMA20 trend?")
F["rsi_b"] = bucket(F.rsi, [0, 30, 40, 50, 60, 70, 100], ["<30", "30-40", "40-50", "50-60", "60-70", ">70"]); tab(["sign", "rsi_b"], title="exposure x RSI14 (prev close)")
F["ext_b"] = bucket(F.dist20_atr, [-99, -2, -1, 0, 1, 2, 99], ["<-2", "-2..-1", "-1..0", "0..1", "1..2", ">2"]); tab(["sign", "ext_b"], title="exposure x distance from EMA20 in ATRs")
F["gap_b"] = bucket(F.gap * 100, [-99, -1, -0.3, 0.3, 1, 99], ["gap<-1%", "-1..-0.3", "flat", "0.3..1", "gap>1%"]); tab(["sign", "gap_b"], title="exposure x opening gap")
F["ret5_b"] = bucket(F.ret5 * 100, [-99, -5, -2, 0, 2, 5, 99], ["<-5%", "-5..-2", "-2..0", "0..2", "2..5", ">5%"]); tab(["sign", "ret5_b"], title="exposure x prior 5-day return")
F["spy_reg"] = np.select([(F.spy_above_e20 & F.spy_above_e50), (~F.spy_above_e20 & ~F.spy_above_e50)], ["spy up", "spy down"], "spy mixed"); tab(["sign", "spy_reg"], title="exposure x SPY regime")
F["vix_b"] = bucket(F.vix, [0, 14, 18, 22, 30, 99], ["<14", "14-18", "18-22", "22-30", ">30"]); tab("vix_b")
F["hi_b"] = np.select([F.near_hi20 > -0.005, F.near_lo20 < 0.005], ["at 20d high", "at 20d low"], "inside"); tab(["sign", "hi_b"], title="exposure x position vs 20-day range")
F["otm_b"] = bucket(F.otm_pct * 100, [-99, -3, -1, 0, 1, 3, 99], ["ITM>3%", "ITM1-3", "ITM<1", "OTM<1", "OTM1-3", "OTM>3%"]); tab(["dte_b", "otm_b"], F[F.dte_b.isin(["0", "1", "2"])], "0-2 DTE moneyness (vs day close proxy)")
tab("otm_b", F[F.dte_b.isin(["15-30", "31-60", "60+"])], "15+ DTE moneyness")
# --- behavior ---
F["adds"] = np.where(F.n_open_orders == 1, "single", np.where(F.avg_down > 0, "avg down", "add up")); tab("adds")
F["seq_b"] = bucket(F.seq_in_day, [0, 1, 3, 6, 99], ["1st", "2-3", "4-6", "7+"]); tab("seq_b", title="n-th trade of the day (per account)")
F["tid_b"] = bucket(F.trades_in_day, [0, 1, 3, 6, 10, 99], ["1", "2-3", "4-6", "7-10", "11+"]); tab("tid_b", title="trades that day (per account)")
F["after"] = np.select([F.prev_pnl < -200, F.prev_pnl < 0, F.prev_pnl > 0], ["after loss>200", "after small loss", "after win"], "first"); tab("after")
F["daypnl_b"] = bucket(F.day_pnl_before, [-1e9, -1000, -300, 0, 300, 1000, 1e9], ["<-1k", "-1k..-300", "-300..0", "0..300", "300..1k", ">1k"]); tab("daypnl_b", title="day P&L before this trade")
F["reentry_b"] = bucket(F.min_since_prev_exit, [-1, 2, 10, 60, 1e9], ["<2m", "2-10m", "10-60m", ">1h"]); tab(["after", "reentry_b"], F[F.after != "first"], "re-entry speed after previous exit")
tab("cancels_before", F.assign(cancels_before=F.cancels_before.clip(upper=3)), "cancelled entry orders before fill (chasing)")
tab("cancels_during", F.assign(cancels_during=F.cancels_during.clip(upper=4)), "cancelled orders during the position")
F["wait_b"] = bucket(F.entry_wait_s, [-1, 2, 30, 300, 1e9], ["instant", "2-30s", "30s-5m", ">5m"]); tab("wait_b", title="time from order placed to first fill (limit patience)")
tab("status")
# --- asymmetry / holding ---
print("\n== hold time winners vs losers (median min) ==")
print(F.groupby([F.pnl > 0, "dte_b"], observed=True).hold_min.median().unstack(0).round(1).rename(columns={True: "winners", False: "losers"}))
print("\n== return distribution (ret on premium) ==")
print(F.ret.describe(percentiles=[.05, .1, .25, .5, .75, .9, .95]).round(3))
print("share of losers that lost >50% of premium:", ((F.ret <= -0.5).sum() / (F.pnl <= 0).sum()).round(3), "| share of winners >+50%:", ((F.ret >= 0.5).sum() / (F.pnl > 0).sum()).round(3))
# --- thesis follow-through ---
md = F[F.dx > F.d0]
print("\n== multi-day trades: underlying moved with thesis? ==")
print(md.groupby(md.thesis_move > 0).apply(stats).round(2))
print("losers multi-day: would the underlying have been in-thesis by expiry? ", (md[md.pnl <= 0].move_to_expiry > 0).mean().round(3), "| post-exit 5d move (signed) mean for winners", md[md.pnl > 0].post_exit_5d.mean().round(4), "losers", md[md.pnl <= 0].post_exit_5d.mean().round(4))
h = F[F.S_h.notna()]
print("\n== hourly-context subset (Oct-2024+):", len(h), "==")
h["m1_b"] = bucket(h.move_1h * 100 * h.sign, [-99, -0.5, -0.15, 0.15, 0.5, 99], ["against>0.5", "against", "flat", "with", "with>0.5"]); tab("m1_b", h, "last-1h underlying move relative to trade direction (chasing?)")
h["fo_b"] = bucket(h.from_open_h * 100 * h.sign, [-99, -1, -0.3, 0.3, 1, 99], ["against>1%", "against", "flat", "with", "with>1%"]); tab("fo_b", h, "move since open relative to trade direction")
h["pir_b"] = bucket(h.pos_in_range_h, [-0.01, 0.2, 0.4, 0.6, 0.8, 1.01], ["bottom", "low", "mid", "high", "top"]); tab(["sign", "pir_b"], h, "exposure x position in day's range so far")
h["otmh_b"] = bucket(h.otm_h * 100, [-99, -1, 0, 0.5, 1, 2, 99], ["ITM>1", "ITM<1", "OTM<0.5", "OTM0.5-1", "OTM1-2", "OTM>2"]); tab(["dte_b", "otmh_b"], h[h.dte_b.isin(["0", "1"])], "0-1 DTE moneyness at entry (hourly proxy)")
print("\nthesis_h>0 win rate:", h.groupby(h.thesis_h > 0).apply(stats).round(2).to_string())
print("\n0DTE: did the underlying go the trade's way by the close? ", h[h.dte_b == "0"].groupby(h.entry_to_close_h > 0).apply(stats).round(2).to_string())
# --- worst / best ---
cols = ["acct", "sym", "d0", "otype", "dir", "dte_entry", "max_qty", "open_cost", "pnl", "ret", "hold_min", "n_open_orders", "avg_down", "trend", "rsi", "dist20_atr", "gap", "seq_in_day", "day_pnl_before"]
print("\n== 25 worst ==\n", F.nsmallest(25, "pnl")[cols].round(2).to_string())
print("\n== 25 best ==\n", F.nlargest(25, "pnl")[cols].round(2).to_string())
# --- monthly ---
m = F.groupby(F.d0.dt.to_period("M")).apply(stats); m["dte0_share"] = F.groupby(F.d0.dt.to_period("M")).apply(lambda g: (g.dte_entry <= 1).mean()); m["tpd"] = F.groupby(F.d0.dt.to_period("M")).apply(lambda g: len(g) / g.d0.nunique())
print("\n== monthly ==\n", m.round(2).to_string())
# --- setup classes ---
def setup(r):
    if pd.isna(r.dist20_atr): return "na"
    if r.sign == 1:
        if r.trend == "up(>e20&e50)" and -1.5 <= r.dist20_atr <= 0.5 and r.rsi < 60: return "L pullback in uptrend"
        if r.trend == "up(>e20&e50)" and r.dist20_atr > 2: return "L extended uptrend"
        if r.trend == "up(>e20&e50)": return "L uptrend other"
        if r.trend == "down(<e20&e50)" and r.rsi < 35: return "L oversold bounce"
        if r.trend == "down(<e20&e50)": return "L counter-trend"
        return "L mixed"
    else:
        if r.trend == "down(<e20&e50)" and -0.5 <= r.dist20_atr <= 1.5 and r.rsi > 40: return "S bounce in downtrend"
        if r.trend == "down(<e20&e50)" and r.dist20_atr < -2: return "S extended downtrend"
        if r.trend == "down(<e20&e50)": return "S downtrend other"
        if r.trend == "up(>e20&e50)" and r.rsi > 70: return "S overbought fade"
        if r.trend == "up(>e20&e50)": return "S counter-trend"
        return "S mixed"
F["setup"] = F.apply(setup, axis=1)
F["dte_c"] = np.select([F.dte_entry <= 1, F.dte_entry <= 7, F.dte_entry <= 30], ["0-1", "2-7", "8-30"], "31+")
tab(["setup", "dte_c"], title="setup class x DTE class", sort=False)
F.to_csv(f"{ROOT}/features.csv", index=False)
