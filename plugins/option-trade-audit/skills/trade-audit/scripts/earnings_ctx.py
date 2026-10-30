"""Attach earnings proximity to features.csv (yfinance earnings dates)."""
import json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import CACHE, FEATURES  # noqa: E402
import numpy as np, pandas as pd  # noqa: E402

ETFS = {"SPY", "QQQ", "IWM", "DIA", "UVXY", "TQQQ", "SQQQ", "SLV", "GLD", "TLT", "SPXW", "VIX"}
CACHE_F = os.path.join(CACHE, "earnings.json")


def dates_for(syms):
    import yfinance as yf
    try:
        cache = json.load(open(CACHE_F))
    except Exception:
        cache = {}
    for s in syms:
        if s in cache:
            continue
        try:
            ed = yf.Ticker(s).get_earnings_dates(limit=24)
            cache[s] = sorted({str(i.date()) for i in ed.index}) if ed is not None else []
        except Exception:
            cache[s] = []
    json.dump(cache, open(CACHE_F, "w"))
    return {k: [pd.Timestamp(d).date() for d in v] for k, v in cache.items()}


F = pd.read_csv(FEATURES, parse_dates=["d0", "dx"])
cache = dates_for(sorted(s for s in F.sym.unique() if s not in ETFS))
nxt, prv, inhold = [], [], []
for _, e in F.iterrows():
    ds = cache.get(e.sym, [])
    d0, dx = e.d0.date(), e.dx.date()
    after = [d for d in ds if d >= d0]; before = [d for d in ds if d < d0]
    nxt.append((after[0] - d0).days if after else np.nan)
    prv.append((d0 - before[-1]).days if before else np.nan)
    inhold.append(any(d0 <= d <= dx for d in ds))
F["earn_next_d"], F["earn_prev_d"], F["earn_in_hold"] = nxt, prv, inhold
F["earn_ctx"] = np.select([F.earn_prev_d <= 1, F.earn_next_d <= 2, F.earn_next_d <= 7],
                          ["post-earnings day", "pre-earnings 0-2d", "earnings within week"], "none")
F.loc[F.earn_next_d.isna() & F.earn_prev_d.isna(), "earn_ctx"] = "n/a(etf)"
F.to_csv(FEATURES, index=False)
print("earnings context:", F.earn_ctx.value_counts().to_dict())
