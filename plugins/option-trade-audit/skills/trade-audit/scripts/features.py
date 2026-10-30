"""Attach technical context (daily) + sequence/behavioral features to each episode -> features.csv"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from paths import CACHE, ACCTS, PX, PX_H1, EPISODES, FEATURES, LOTS  # noqa: E402
ROOT = CACHE
import pandas as pd, numpy as np, os
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York"); eps = pd.read_csv(f"{ROOT}/episodes.csv")
eps = eps[eps.status != "open"].copy()
eps["t0"] = pd.to_datetime(eps.first_open, utc=True, format="ISO8601").dt.tz_convert(ET)
eps["tx"] = pd.to_datetime(eps.last_close, utc=True, format="ISO8601").dt.tz_convert(ET)
eps["d0"] = eps.t0.dt.normalize().dt.tz_localize(None); eps["dx"] = eps.tx.dt.normalize().dt.tz_localize(None)
eps["entry_hour"] = eps.t0.dt.hour + eps.t0.dt.minute / 60; eps["dow"] = eps.t0.dt.dayofweek
eps["min_since_open"] = (eps.entry_hour - 9.5) * 60
eps["exit_hour"] = eps.tx.dt.hour + eps.tx.dt.minute / 60
eps["sign"] = np.where(eps.otype == "call", 1, -1) * np.where(eps.dir == "long", 1, -1)   # +1 = bullish exposure

PX = {}
def load_px(sym):
    if sym in PX: return PX[sym]
    p = f"{ROOT}/px/{sym}.csv"
    if not os.path.exists(p): PX[sym] = None; return None
    d = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index(); d = d[~d.index.duplicated()]
    c = d.Close
    for n in (10, 20, 50, 200): d[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    delta = c.diff(); up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    d["rsi14"] = 100 - 100 / (1 + up.ewm(alpha=1/14, adjust=False).mean() / dn.ewm(alpha=1/14, adjust=False).mean())
    tr = pd.concat([d.High - d.Low, (d.High - c.shift()).abs(), (d.Low - c.shift()).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    d["ret1"] = c.pct_change(); d["ret5"] = c.pct_change(5); d["ret20"] = c.pct_change(20)
    d["hv20"] = np.log(c).diff().rolling(20).std() * np.sqrt(252)
    d["hi20"] = d.High.rolling(20).max(); d["lo20"] = d.Low.rolling(20).min()
    d["ema20_slope"] = d.ema20 / d.ema20.shift(5) - 1
    if "Volume" in d: d["rvol"] = d.Volume / d.Volume.rolling(20).mean()
    PX[sym] = d; return d

spy = load_px("SPY"); vix = load_px("VIX")
rows = []
for i, e in eps.iterrows():
    d = load_px(e.sym); r = {}
    if d is None or e.d0 not in d.index:
        rows.append(r); continue
    pos0 = d.index.get_loc(e.d0)
    if pos0 == 0: rows.append(r); continue
    prev = d.iloc[pos0 - 1]; r0 = d.iloc[pos0]
    S = r0.Close
    r.update(prev_close=prev.Close, o0=r0.Open, c0=S, h0=r0.High, l0=r0.Low,
             above_e10=prev.Close > prev.ema10, above_e20=prev.Close > prev.ema20, above_e50=prev.Close > prev.ema50, above_e200=prev.Close > prev.ema200,
             e10_gt_e20=prev.ema10 > prev.ema20, e20_slope=prev.ema20_slope, rsi=prev.rsi14, dist20_atr=(prev.Close - prev.ema20) / prev.atr14,
             dist50_atr=(prev.Close - prev.ema50) / prev.atr14, ret1=prev.ret1, ret5=prev.ret5, ret20=prev.ret20, hv20=prev.hv20, atr_pct=prev.atr14 / prev.Close,
             near_hi20=prev.Close / prev.hi20 - 1, near_lo20=prev.Close / prev.lo20 - 1, rvol_prev=prev.get("rvol", np.nan),
             gap=r0.Open / prev.Close - 1, day_ret=S / r0.Open - 1, day_range_atr=(r0.High - r0.Low) / prev.atr14,
             close_pos=(S - r0.Low) / (r0.High - r0.Low) if r0.High > r0.Low else 0.5, rvol0=r0.get("rvol", np.nan),
             # intraday-ish: price at entry proxied by open-weighted (first 30 min) else close
             S_proxy=r0.Open if e.min_since_open < 30 else S)
    Sp = r["S_proxy"]
    r["otm_pct"] = (e.strike / Sp - 1) if e.otype == "call" else (1 - e.strike / Sp)
    # thesis / follow-through
    posx = d.index.get_loc(e.dx) if e.dx in d.index else min(pos0 + 1, len(d) - 1)
    if e.dx > e.d0 and posx > pos0:
        seg = d.iloc[pos0 + 1: posx + 1]
        r["thesis_move"] = (d.iloc[posx].Close / S - 1) * e.sign
        r["mfe_und"] = ((seg.High.max() / S - 1) if e.sign == 1 else (1 - seg.Low.min() / S))
        r["mae_und"] = ((seg.Low.min() / S - 1) if e.sign == 1 else (1 - seg.High.max() / S))
    else:
        r["thesis_move"] = (S / r0.Open - 1) * e.sign if e.min_since_open < 30 else np.nan
    pe = min(posx + 5, len(d) - 1); r["post_exit_5d"] = (d.iloc[pe].Close / d.iloc[posx].Close - 1) * e.sign
    expd = pd.Timestamp(e.expiry); pexp = d.index.searchsorted(expd, side="right") - 1
    r["move_to_expiry"] = (d.iloc[min(pexp, len(d) - 1)].Close / S - 1) * e.sign if pexp > pos0 else np.nan
    # market regime
    if spy is not None and e.d0 in spy.index:
        ps = spy.index.get_loc(e.d0); sp = spy.iloc[ps - 1]; s0 = spy.iloc[ps]
        r.update(spy_above_e20=sp.Close > sp.ema20, spy_above_e50=sp.Close > sp.ema50, spy_ret5=sp.ret5, spy_dist20_atr=(sp.Close - sp.ema20) / sp.atr14,
                 spy_gap=s0.Open / sp.Close - 1, spy_day_ret=s0.Close / s0.Open - 1, spy_rsi=sp.rsi14)
    if vix is not None and e.d0 in vix.index:
        pv = vix.index.get_loc(e.d0); r.update(vix=vix.iloc[pv - 1].Close, vix_chg5=vix.iloc[pv - 1].Close / vix.iloc[max(pv - 6, 0)].Close - 1, vix_open=vix.iloc[pv].Open)
    rows.append(r)
F = pd.concat([eps.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
# sequence / behavioral features per account
F = F.sort_values(["acct", "t0"]).reset_index(drop=True)
F["prev_pnl"] = F.groupby("acct").pnl.shift(1)
F["prev_close_t"] = F.groupby("acct").tx.shift(1)
F["min_since_prev_exit"] = (F.t0 - F.prev_close_t).dt.total_seconds() / 60
F["day_key"] = F.acct + "_" + F.d0.astype(str)
F["seq_in_day"] = F.groupby("day_key").cumcount() + 1
F["trades_in_day"] = F.groupby("day_key").pnl.transform("size")
F["day_pnl_before"] = F.groupby("day_key").pnl.cumsum() - F.pnl
F["win"] = (F.pnl > 0).astype(int)
F["cost_bucket"] = pd.cut(F.open_cost, [0, 250, 500, 1000, 2500, 5000, 1e9], labels=["<250", "250-500", "500-1k", "1k-2.5k", "2.5k-5k", ">5k"])
F["dte_b"] = pd.cut(F.dte_entry, [-1, 0, 1, 2, 4, 7, 14, 30, 60, 10000], labels=["0", "1", "2", "3-4", "5-7", "8-14", "15-30", "31-60", "60+"])
F["hold_b"] = pd.cut(F.hold_min.astype(float), [-1, 5, 15, 30, 60, 180, 390, 2000, 1e9], labels=["<5m", "5-15m", "15-30m", "30-60m", "1-3h", "3-6.5h", "1-3d", ">3d"])
F.to_csv(f"{ROOT}/features.csv", index=False)
have = F.c0.notna()
print("episodes", len(F), "with daily context", int(have.sum()), "| after 2024-10-12 (hourly available):", int((F.d0 >= "2024-10-12").sum()))
print(F.groupby("dte_b", observed=True).agg(n=("pnl", "size"), pnl=("pnl", "sum"), win=("win", "mean"), avg_ret=("ret", "mean")).round(3))
print(F.groupby("hold_b", observed=True).agg(n=("pnl", "size"), pnl=("pnl", "sum"), win=("win", "mean"), avg_ret=("ret", "mean")).round(3))
