"""Download the bars the audit needs for every symbol that was traded.

    python3 fetch_px.py [--since 2023-01-01] [--h1-top 21]

Daily bars come from yfinance for every traded symbol plus ^VIX (saved as
VIX.csv). Hourly bars only go back ~730 days at yfinance, so they are fetched
for the most-traded names only. Robinhood's own `get_equity_historicals` is NOT
used for history: before roughly mid-2026 it returns interpolated filler bars
(`interpolated: true`, zero volume), which are useless for entry context.
"""
import argparse, collections, csv, os, sys, time, warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))
from paths import EPISODES, PX, PX_H1, ensure  # noqa: E402

# Renames and tickers yfinance spells differently from the option chain symbol.
MAP = {"BRKB": "BRK-B", "SQ": "XYZ", "SPXW": "^SPX"}

def traded_symbols():
    with open(EPISODES) as fh:
        rows = list(csv.DictReader(fh))
    counts = collections.Counter(r["sym"] for r in rows)
    return counts

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--h1-top", type=int, default=21)
    ap.add_argument("--h1-since", default=None, help="default: 730 days ago")
    a = ap.parse_args()
    ensure()
    import pandas as pd, yfinance as yf
    counts = traded_symbols()
    syms = sorted(counts)
    end = (pd.Timestamp.today() + pd.Timedelta(days=1)).date().isoformat()
    want = [MAP.get(s, s) for s in syms]
    df = yf.download(want, start=a.since, end=end, auto_adjust=False,
                     group_by="ticker", progress=False, threads=True)
    ok, bad = [], []
    for s in syms:
        t = MAP.get(s, s)
        try:
            d = df[t].dropna(subset=["Close"])
        except Exception:
            bad.append(s); continue
        if len(d) < 50:
            bad.append(s); continue
        d = d[["Open", "High", "Low", "Close", "Volume"]].copy()
        d.index.name = "Date"
        d.to_csv(os.path.join(PX, f"{s}.csv"))
        ok.append(s)
    v = yf.download("^VIX", start=a.since, end=end, auto_adjust=False, progress=False)
    v.columns = [c[0] if isinstance(c, tuple) else c for c in v.columns]
    v[["Open", "High", "Low", "Close"]].rename_axis("Date").to_csv(os.path.join(PX, "VIX.csv"))
    print(f"daily: {len(ok)} symbols, no data for {bad}, plus VIX")
    h1_since = a.h1_since or (pd.Timestamp.today() - pd.Timedelta(days=725)).date().isoformat()
    top = [s for s, _ in counts.most_common(a.h1_top)]
    done = []
    for s in top:
        try:
            h = yf.download(MAP.get(s, s), start=h1_since, end=end, interval="1h",
                            auto_adjust=False, progress=False)
            h.columns = [c[0] if isinstance(c, tuple) else c for c in h.columns]
            h = h[["Open", "High", "Low", "Close", "Volume"]]
            h.index.name = "ts"
            h.to_csv(os.path.join(PX_H1, f"{s}.csv"))
            done.append(f"{s}:{len(h)}")
        except Exception as ex:
            done.append(f"{s}:ERR")
        time.sleep(0.4)
    print(f"hourly from {h1_since}: {' '.join(done)}")

if __name__ == "__main__":
    main()
