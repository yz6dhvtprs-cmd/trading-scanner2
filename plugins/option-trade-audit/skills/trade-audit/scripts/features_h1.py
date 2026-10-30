"""Hourly-bar intraday context for episodes after 2024-10-14 (yfinance 1h) -> features.csv (adds *_h columns)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from paths import CACHE, ACCTS, PX, PX_H1, EPISODES, FEATURES, LOTS  # noqa: E402
ROOT = CACHE
import pandas as pd, numpy as np, os
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York"); F = pd.read_csv(f"{ROOT}/features.csv", parse_dates=["d0", "dx"])
F["t0"] = pd.to_datetime(F.t0, utc=True, format="ISO8601").dt.tz_convert(ET); F["tx"] = pd.to_datetime(F.tx, utc=True, format="ISO8601").dt.tz_convert(ET)
H = {}
def load_h(sym):
    if sym in H: return H[sym]
    p = f"{ROOT}/px/h1/{sym}.csv"
    if not os.path.exists(p): H[sym] = None; return None
    h = pd.read_csv(p); h["ts"] = pd.to_datetime(h.ts, utc=True).dt.tz_convert(ET); h = h.set_index("ts").sort_index()
    h["day"] = h.index.tz_localize(None).normalize(); H[sym] = h; return h
def px_at(h, t):
    """interpolated price inside the hourly bar containing t (bar start <= t < start+1h)"""
    i = h.index.searchsorted(t, side="right") - 1
    if i < 0: return np.nan, i
    b = h.iloc[i]; frac = min(max((t - h.index[i]).total_seconds() / 3600, 0), 1)
    if (t - h.index[i]).total_seconds() > 3600 * 1.5: return b.Close, i
    return b.Open + (b.Close - b.Open) * frac, i
cols = {}
for i, e in F.iterrows():
    h = load_h(e.sym)
    if h is None or e.d0 < pd.Timestamp("2024-10-14"): continue
    day = h[h.day == e.d0]
    if day.empty: continue
    S0, i0 = px_at(h, e.t0); Sx, ix = px_at(h, e.tx)
    if np.isnan(S0) or np.isnan(Sx): continue
    upto = day[day.index <= e.t0]
    r = dict(S_h=S0, Sx_h=Sx)
    r["otm_h"] = (e.strike / S0 - 1) if e.otype == "call" else (1 - e.strike / S0)
    r["from_open_h"] = S0 / day.iloc[0].Open - 1
    if not upto.empty:
        rh, rl = max(upto.High.max(), S0), min(upto.Low.min(), S0)
        r["pos_in_range_h"] = (S0 - rl) / (rh - rl) if rh > rl else 0.5
        r["range_so_far_atr"] = (rh - rl) / e.atr_pct / S0 if e.atr_pct and e.atr_pct > 0 else np.nan
    r["move_1h"] = S0 / h.iloc[i0 - 1].Close - 1 if i0 >= 1 else np.nan
    r["move_2h"] = S0 / h.iloc[i0 - 2].Close - 1 if i0 >= 2 else np.nan
    r["thesis_h"] = (Sx / S0 - 1) * e.sign
    seg = h.iloc[i0: ix + 1]
    if len(seg):
        r["mfe_h"] = (seg.High.max() / S0 - 1) if e.sign == 1 else (1 - seg.Low.min() / S0)
        r["mae_h"] = (seg.Low.min() / S0 - 1) if e.sign == 1 else (1 - seg.High.max() / S0)
    if ix + 1 < len(h): r["post_exit_1h"] = (h.iloc[ix + 1].Close / Sx - 1) * e.sign
    # rest-of-day after entry: did the underlying end the day in the thesis direction?
    r["entry_to_close_h"] = (day.iloc[-1].Close / S0 - 1) * e.sign
    cols[i] = r
X = pd.DataFrame.from_dict(cols, orient="index")
for c in X.columns: F[c] = X[c]
F.to_csv(f"{ROOT}/features.csv", index=False)
print("hourly context rows:", len(X), "of", len(F))
