"""Per-ticker context builder: news mapped to 15m candles + earnings record.

Usage:
    python research/fetch_context.py --ticker AAPL

Writes research/<TICKER>/:
    news_15m.csv   recent headlines -> nearest 15m candle (ts, title,
                   publisher, link, candle_ts, fwd_4h_ret_pct)
    earnings.json  earnings dates + reported EPS/revenue surprise history
                   (yfinance; machine-usable) + IR-page pointers

Free-data limits (honest): yfinance exposes only ~recent headlines, so the
news map covers the current window, not years of history. Earnings dates
go back ~4 years. 10-K narrative lives on investor.apple.com (see the
pointers file); headline figures are pulled from filings metadata below.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    a = ap.parse_args()
    t = a.ticker.upper()
    out = os.path.join(ROOT, "research", t)
    os.makedirs(out, exist_ok=True)
    import yfinance as yf
    tk = yf.Ticker(t)

    # --- news -> 15m candles ---
    m15 = pd.read_csv(os.path.join(out, "m15_100.csv"), parse_dates=["ts"])
    m15 = m15.set_index("ts").sort_index()
    try:
        m15.index = m15.index.tz_localize(None)
    except TypeError:
        pass  # already tz-naive
    news = tk.news or []
    rows = []
    for n in news:
        c = n.get("content") or {}
        if isinstance(c, str):  # older yfinance: fields at top level
            c = n
        try:
            raw_ts = c.get("pubDate") or c.get("providerPublishTime")
            if isinstance(raw_ts, (int, float)):
                ts = pd.to_datetime(raw_ts, unit="s", utc=True)
            else:
                ts = pd.to_datetime(raw_ts, utc=True)
            ts = ts.tz_convert("America/Los_Angeles").tz_localize(None)
        except Exception:
            continue
        # nearest candle at-or-after publish time
        fut = m15.index[m15.index >= ts]
        if len(fut) == 0:
            continue
        c0 = fut[0]
        i = m15.index.get_loc(c0)
        j = min(i + 16, len(m15) - 1)  # ~4h forward
        fwd = (float(m15["Close"].iloc[j]) / float(m15["Close"].iloc[i]) - 1) \
            * 100 if j > i else float("nan")
        prov = c.get("provider") or {}
        rows.append({"published": ts.strftime("%Y-%m-%d %H:%M"),
                     "publisher": prov.get("displayName", "")
                     if isinstance(prov, dict) else str(prov),
                     "title": (c.get("title", "") or "")[:200],
                     "link": (c.get("canonicalUrl") or {}).get("url", "")
                     if isinstance(c.get("canonicalUrl"), dict)
                     else (n.get("link", "") or ""),
                     "candle_ts": str(c0),
                     "fwd_4h_ret_pct": round(float(fwd), 2)
                     if pd.notna(fwd) else ""})
    pd.DataFrame(rows).to_csv(os.path.join(out, "news_15m.csv"), index=False)

    # --- earnings record ---
    ed = None
    try:
        ed = tk.earnings_dates  # trailing 4y + next estimate
    except Exception as e:  # noqa: BLE001 - free API flakiness
        print(f"earnings_dates failed: {e}", flush=True)
    rec = {"ticker": t, "trailing": [], "ir_pointers": {
        "ir_home": "https://investor.apple.com/investor-relations/default.aspx",
        "quarterly_results": "https://investor.apple.com/earnings-releases/default.aspx",
        "sec_10k": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K"}}
    if ed is not None and len(ed):
        ed = ed.reset_index()
        for _, r in ed.iterrows():
            rec["trailing"].append({
                "date": str(r.iloc[0])[:10],
                "eps_estimate": _f(r.get("EPS Estimate")),
                "eps_actual": _f(r.get("Reported EPS")),
                "surprise_pct": _f(r.get("Surprise(%)"))})
    # --- quarterly fundamentals (10-Q/10-K headline figures) ---
    try:
        q = tk.quarterly_income_stmt
        if q is not None and len(q.columns):
            keep = [r for r in ("Total Revenue", "Gross Profit",
                                "Operating Income", "Net Income")
                    if r in q.index]
            fq = q.loc[keep].iloc[:, :8]
            fq.to_csv(os.path.join(out, "fundamentals_q.csv"))
            rec["fundamentals_rows"] = len(fq.columns)
    except Exception as e:  # noqa: BLE001 - free API flakiness
        print(f"quarterly income failed: {e}", flush=True)
    with open(os.path.join(out, "earnings.json"), "w") as f:
        json.dump(rec, f, indent=1)
    print(f"{t}: {len(rows)} news rows, "
          f"{len(rec['trailing'])} earnings rows", flush=True)
    return 0


def _f(v):
    try:
        return round(float(v), 4) if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
