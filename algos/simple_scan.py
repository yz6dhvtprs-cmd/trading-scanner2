"""Simple EMA pullback scanner + 20% backtest.

Setup (daily): Close below EMA 8, 13 AND 21, while within 1xATR of EMA50
(either side) — price raining under the short trends but sitting on value —
plus RSI14 from --rsi-target to --rsi-max (default 40 to 45;
--no-rsi disables) AND as-of weekly RSI14
above --wrsi-min (default 55: the pullback sits inside a weekly uptrend).
The previous daily close must still have been strictly above EMA50 — first
touch of value only, not names camped under the line.

Backtest per fresh signal: enter at the signal-day close (proxy for the
closing-15m fill — same print), then report days to +target% (default 20%,
first High at/above it), max profit % (best High since), and current
profit % vs the last daily close. Signals cluster day-runs: only the
FIRST bar of each consecutive run counts (no overlapping entries).
The `page` column mirrors the live 3-trading-day alert dedupe: Y means
live would have paged it, - means suppressed (signaled within 3 trading
sessions of the previous signal). Backtest stats still count every row.
`pslack`/`rslack` are the weakest-gate margins (price gates in ATRs, RSI
gates in points) — near-zero means any data wobble flips the row.

Usage:
    python algos/simple_scan.py --ticker NVDA --days 60
    python algos/simple_scan.py --pool spy50 --days 90 --target 0.20
    python algos/simple_scan.py --pool spy50 --days 60 --simulate
Pools: sp100 (top-100 S&P500 by weight, proxy), sp500, spy (=sp500),
qqq (=nasdaq100), spy50, qqq50 (top 50 by ETF weight, SlickCharts 09-2026).
Join with '+' for a union: --pool spy50+qqq50.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

from market_calendar import add_trading_days, trading_gap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_pool(name: str) -> list:
    """Weight-ranked tickers for a pool (yfinance form: BRK-B not BRK.B)."""
    def read(f):
        df = pd.read_csv(os.path.join(ROOT, "universe", f))
        return [t.replace(".", "-") for t in df["symbol"].tolist()]

    if name in ("sp500", "spy"):
        return read("sp500_w.csv")
    if name == "qqq":
        return read("nasdaq100_w.csv")
    if name == "spy50":
        return read("sp500_w.csv")[:50]
    if name == "qqq50":
        return read("nasdaq100_w.csv")[:50]
    if name == "sp100":
        return read("sp500_w.csv")[:100]  # proxy: 100 largest S&P weights
    raise ValueError(f"unknown pool {name}")


POOLS = ("sp100", "sp500", "spy", "qqq", "spy50", "qqq50")


def load_pools(spec: str) -> list:
    """One pool or '+'-joined union, e.g. spy50+qqq50. Order-preserving,
    de-duplicated (tickers in yfinance form)."""
    out, seen = [], set()
    for part in spec.split("+"):
        part = part.strip().lower()
        if part not in POOLS:
            raise ValueError(f"unknown pool {part!r} in {spec!r} "
                             f"(choose from {', '.join(POOLS)}, '+'-joined)")
        for t in load_pool(part):
            if t not in seen:
                seen.add(t)
                out.append(t)
    if not out:
        raise ValueError(f"empty pool spec {spec!r}")
    return out


def ema(s: pd.Series, n: int) -> pd.Series:
    """Plain exponential moving average (matches charting platforms)."""
    return s.ewm(span=n, adjust=False).mean()


def rsi14(close: pd.Series) -> pd.Series:
    """Wilder RSI14 (flat reads 50, pure-up 100)."""
    d = close.astype(float).diff()
    up, dn = d.clip(lower=0.0), -d.clip(upper=0.0)
    ru = up.ewm(alpha=1.0 / 14, adjust=False).mean()
    rd = dn.ewm(alpha=1.0 / 14, adjust=False).mean()
    out = 100.0 - 100.0 / (1.0 + ru / rd.replace(0.0, float("nan")))
    out = out.fillna(50.0)
    out.loc[(rd == 0) & (ru > 0)] = 100.0
    return out


def weekly_rsi(close: pd.Series) -> pd.Series:
    """Weekly Wilder RSI14 as-of each daily bar: completed Friday-close
    weeks plus this bar's own close as the forming week — the value a live
    chart shows mid-week. No lookahead (only data at/before the bar). Reads
    ~50 until ~14 weeks warm up, failing a >55 gate."""
    out = pd.Series(float("nan"), index=close.index)
    done: list = []
    for _, grp in close.resample("W-FRI"):
        if len(grp) == 0:
            continue
        for ts, px in grp.items():
            out.loc[ts] = rsi14(pd.Series(done + [float(px)])).iloc[-1]
        done.append(float(grp.iloc[-1]))
    return out


def add_ind(d: pd.DataFrame) -> pd.DataFrame:
    """Daily + EMA8/13/21/50, RSI14, as-of weekly RSI14 and Wilder ATR14."""
    d = d.copy()
    c = d["Close"].astype(float)
    for n in (8, 13, 21, 50):
        d[f"ema{n}"] = ema(c, n)
    d["rsi"] = rsi14(c)
    d["wrsi"] = weekly_rsi(c)
    h, l, pc = d["High"].astype(float), d["Low"].astype(float), c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    return d


def setup_row(r, rsi_on: bool = True, rsi_target: float = 40.0,
              rsi_max: float = 45.0, atr_mult: float = 1.0,
              wrsi_min: float = 55.0, prev_close=None,
              today_open=None) -> bool:
    """One-bar EMA50+RSI rule (shared by batch scan and live stage). Daily
    RSI must sit in the target band AND as-of weekly RSI must clear
    `wrsi_min` (uptrend regime for the long). The previous daily close AND
    today's open must both have printed strictly above EMA50 — first touch
    of value only, not names camped under the line or gapping under it."""
    if not (r["Close"] < r["ema8"] and r["Close"] < r["ema13"] and
            r["Close"] < r["ema21"]):
        return False
    if abs(r["Close"] - r["ema50"]) > atr_mult * r["atr"]:
        return False
    if prev_close is None or not (prev_close > r["ema50"]):
        return False
    if today_open is None or not (today_open > r["ema50"]):
        return False
    if not rsi_on:
        return True
    return rsi_target <= r["rsi"] <= rsi_max and r["wrsi"] > wrsi_min


def fresh_signals(d: pd.DataFrame, days: int, rsi_on: bool = True,
                  rsi_target: float = 40.0, rsi_max: float = 45.0,
                  atr_mult: float = 1.0,
                  wrsi_min: float = 55.0) -> pd.DataFrame:
    """First-bar-only signals inside the last `days` bars."""
    d = d.copy()
    closes = d["Close"].tolist()
    opens = d["Open"].tolist()
    sig = pd.Series([setup_row(r, rsi_on, rsi_target, rsi_max, atr_mult,
                               wrsi_min, prev, opn)
                     for (_, r), prev, opn in zip(d.iterrows(),
                                                  [None] + closes[:-1],
                                                  opens)],
                    index=d.index)
    d["sig"] = sig & ~sig.shift(1, fill_value=False)
    return d.tail(days)


def gate_slack(r, prev_close, today_open, rsi_target: float = 40.0,
              rsi_max: float = 45.0, atr_mult: float = 1.0,
              wrsi_min: float = 55.0) -> tuple:
    """Weakest-gate margins for a row setup_row accepted: price slack in
    ATRs (min over short-EMA distances, value band, prev-close and open
    distances above EMA50) and RSI slack in points (min over daily-band
    edges and weekly floor). Near-zero = margin call: any data wobble
    flips it, so read small slacks as weak, not wrong."""
    atr = float(r["atr"])
    pslack = min(float(r["ema8"]) - float(r["Close"]),
                 float(r["ema13"]) - float(r["Close"]),
                 float(r["ema21"]) - float(r["Close"]),
                 atr_mult * atr - abs(float(r["Close"]) - float(r["ema50"])),
                 float(prev_close) - float(r["ema50"]),
                 float(today_open) - float(r["ema50"])) / atr
    rslack = min(float(r["rsi"]) - rsi_target,
                 rsi_max - float(r["rsi"]),
                 float(r["wrsi"]) - wrsi_min)
    return round(pslack, 2), round(rslack, 1)


def drop_forming(d: pd.DataFrame, today=None) -> pd.DataFrame:
    """Drop bars dated today-or-later (still forming). Shared by batch,
    replay and live so all three evaluate the same completed bars."""
    today = today or dt.date.today()
    return d[d.index.date < today]


def rsi_window_pass(close: pd.Series, n_bars: int, lo: float,
                    hi: float) -> bool:
    """True if RSI14 lands in [lo, hi] on any of the last `n_bars` closes.
    Cheap AND-filter prefilter: closes only, no EMAs, no weekly loop. A
    ticker that never enters the band cannot signal, so prefiltering loses
    nothing — the full gate still rechecks survivors exactly."""
    if len(close) < 20:
        return False
    r = rsi14(close.astype(float)).tail(n_bars)
    return bool(((r >= lo) & (r <= hi)).any())


def _present(px, t) -> bool:
    """True if a batch frame actually holds usable closes for `t`."""
    try:
        if px is None or len(px) == 0:
            return False
        f = px[t] if isinstance(px.columns, pd.MultiIndex) else px
        col = "Close" if "Close" in f.columns else (
            "close" if "close" in f.columns else None)
        if col is None:
            return False
        c = f[col]
        if isinstance(c, pd.DataFrame):
            c = c.iloc[:, 0]
        return bool((c.notna() & (c != 0)).any())
    except Exception:
        return False


def batched_download(tickers: list, period: str, interval: str = "1d",
                     chunk: int = 100, pause: float = 1.0,
                     timeout: float = 20, downloader=None):
    """Chunked yfinance batch fetch with a serial retry for misses.

    One 517-ticker yf.download fans out into hundreds of threads: Yahoo
    throttles most of them (surfaced as 'possibly delisted; no price
    data') and the tz-cache sqlite db chokes under concurrent writers
    (OperationalError: unable to open database file). Fetching in
    ~100-ticker chunks with a short pause, then re-fetching just the
    misses serially, recovers both failure modes. Returns one combined
    frame shaped like yf.download(group_by='ticker') (flat for a single
    ticker), or None when nothing came back.
    """
    import time
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return None
    import yfinance as yf

    def fetch(ts, serial=False):
        kw = dict(period=period, interval=interval, auto_adjust=True,
                  progress=False, threads=not serial, group_by="ticker",
                  timeout=timeout if serial else 10)
        if downloader is not None:
            return downloader(ts, **kw)
        return yf.download(ts, **kw)

    def normalize(px, ts):
        if px is None or len(px) == 0:
            return None
        if (not isinstance(px.columns, pd.MultiIndex) and len(ts) == 1
                and len(tickers) > 1):
            px = px.copy()
            px.columns = pd.MultiIndex.from_product([ts, px.columns])
        return px

    frames: list = []
    have: set = set()
    groups = [tickers[i:i + chunk] for i in range(0, len(tickers), chunk)]
    for i, g in enumerate(groups):
        if i:
            time.sleep(pause)
        try:
            px = normalize(fetch(g), g)
        except Exception:
            px = None
        if px is not None:
            frames.append(px)
            for t in g:
                if _present(px, t):
                    have.add(t)
    missing = [t for t in tickers if t not in have]
    if missing:
        time.sleep(pause)
        for i in range(0, len(missing), 25):
            g = missing[i:i + 25]
            if i:
                time.sleep(pause)
            try:
                px = normalize(fetch(g, serial=True), g)
            except Exception:
                px = None
            if px is not None:
                frames.append(px)
                for t in g:
                    if _present(px, t):
                        have.add(t)
    if not frames:
        return None
    if len(tickers) == 1:
        return frames[0]
    try:
        out = pd.concat(frames, axis=1)
        out = out.loc[:, ~out.columns.duplicated(keep="last")]
        return out.sort_index()
    except Exception:
        return max(frames, key=len)


def _frame(px, t):
    """Normalized daily frame for one ticker: selects the ticker level,
    drops empty closes, capitalizes columns, drops forming bars."""
    f = px[t] if isinstance(px.columns, pd.MultiIndex) else px
    f = f.dropna(subset=["Close"])
    f.columns = [str(c).capitalize() for c in f.columns]
    return drop_forming(f)


PAGE_GAP = 3  # signals within this many trading sessions of the
# previous signal would be suppressed by the live alert dedupe


def mark_pages(locs: list) -> list:
    """Page flags for ascending signal bar positions. First signal pages;
    each later one pages only if > PAGE_GAP trading sessions after the
    previous signal (mirrors the live 3-trading-day dedupe)."""
    flags, last = [], None
    for loc in locs:
        page = last is None or (loc - last) > PAGE_GAP
        flags.append(page)
        if page:
            last = loc
    return flags


def backtest(d: pd.DataFrame, day, entry: float,
             target: float) -> dict:
    """Forward stats from the bar after `day`. Closes trigger nothing;
    Highs print the money (target touch, max excursion)."""
    fwd = d[d.index > day]
    if len(fwd) == 0:
        return {"days": None, "max": 0.0, "now": 0.0}
    hit = fwd[fwd["High"].astype(float) >= entry * (1.0 + target)]
    return {"days": int((hit.index[0] - day).days) if len(hit) else None,
            "max": float(fwd["High"].astype(float).max() / entry - 1.0),
            "now": float(fwd["Close"].astype(float).iloc[-1] / entry - 1.0)}


def live_sim(d: pd.DataFrame, eval_days: list, rsi_on: bool = True,
             rsi_target: float = 40.0, rsi_max: float = 45.0,
             atr_mult: float = 1.0, ignore_days: int = 5,
             wrsi_min: float = 55.0) -> list:
    """Replay the live EMA stage over `eval_days` (calendar dates ascending,
    one pass per day like the 15m loop). The bar seen on day D is the latest
    bar strictly before D — live drops the forming bar. Deep breaks park the
    ticker for `ignore_days` TRADING sessions (or until a close back over
    EMA50, whichever is sooner); setups inside the park or within PAGE_GAP
    sessions of the last page come back as MUTED rows with reasons, so the
    replay reconciles exactly against live paging. Pure per-ticker state."""
    rows = []
    bar_dates = d.index.date
    closes = d["Close"].tolist()
    opens = d["Open"].tolist()
    alerted, ignored_until = None, None
    for D in eval_days:
        past = d[bar_dates < D]
        if len(past) == 0:
            continue
        day = past.index[-1]
        r = past.iloc[-1]
        loc = d.index.get_loc(day)
        prev = closes[loc - 1] if loc >= 1 else None
        opn = opens[loc]
        px, d50 = float(r["Close"]), float(r["ema50"])
        if px < d50 - float(r["atr"]):  # broken too deep: park it
            ignored_until = add_trading_days(D, ignore_days)
            continue
        if ignored_until is not None:
            if px > d50 or D > ignored_until:  # recovered or expired
                ignored_until = None
            else:
                if setup_row(r, rsi_on, rsi_target, rsi_max, atr_mult,
                               wrsi_min, prev, opn):
                    ps, rs = gate_slack(r, prev, opn, rsi_target, rsi_max,
                                        atr_mult, wrsi_min)
                    rows.append(_row(D, day, r, "MUTED-IGNORE",
                                     f"parked till {ignored_until}", ps, rs))
                continue
        if not setup_row(r, rsi_on, rsi_target, rsi_max, atr_mult,
                         wrsi_min, prev, opn):
            continue
        ps, rs = gate_slack(r, prev, opn, rsi_target, rsi_max,
                            atr_mult, wrsi_min)
        if alerted is not None and trading_gap(alerted, D) <= PAGE_GAP:
            rows.append(_row(D, day, r, "MUTED-DEDUP",
                             f"paged {alerted}", ps, rs))
            continue
        alerted = D
        rows.append(_row(D, day, r, "PAGED", "text would go out", ps, rs))
    return rows


def _row(D, day, r, status: str, detail: str, ps=None, rs=None) -> dict:
    return {"text": D.isoformat(), "bar": day.date().isoformat(),
            "entry": round(float(r["Close"]), 2),
            "ema50": round(float(r["ema50"]), 2),
            "atr": round(float(r["atr"]), 2),
            "rsi": round(float(r["rsi"]), 1),
            "wrsi": round(float(r["wrsi"]), 1),
            "pslack": ps, "rslack": rs,
            "status": status, "detail": detail}


def scan(tickers: list, days: int, target: float, rsi_on: bool = True,
         rsi_target: float = 40.0, rsi_max: float = 45.0,
         atr_mult: float = 1.0, wrsi_min: float = 55.0) -> list:
    """Batched daily fetch, per-ticker signals + backtests. One row/setup.

    Staged funnel: a small short fetch first keeps only tickers whose RSI
    touched the band anywhere in the window (an AND-filter: the rest cannot
    signal); the 1y fetch + full indicators run on survivors only.
    """
    try:
        short = batched_download(tickers, period=f"{days + 40}d",
                                 interval="1d")
    except Exception:
        short = None
    pre = list(tickers)
    if short is not None:
        pre = []
        for t in tickers:
            try:
                f = _frame(short, t)
                if rsi_window_pass(f["Close"], days, rsi_target, rsi_max):
                    pre.append(t)
            except Exception:
                continue
        if not pre:
            return []
    # 1y floor: indicator warmup (EMA50 + weekly RSI) must match live no
    # matter how short the report window is; forming bars (>= today) are
    # dropped like the live stage drops them.
    need = max(days + 150, 365)
    period = f"{need}d" if need <= 720 else "5y"
    px = batched_download(pre, period=period, interval="1d")
    rows = []
    for t in pre:
        try:
            f = _frame(px, t)
            if len(f) < 80:
                continue
            d = fresh_signals(add_ind(f), days, rsi_on, rsi_target,
                              rsi_max, atr_mult, wrsi_min)
            sigd = d[d["sig"]]
            closes = d["Close"].tolist()
            opens = d["Open"].tolist()
            locs = [d.index.get_loc(i) for i in sigd.index]
            for (day, r), loc, page in zip(sigd.iterrows(), locs,
                                           mark_pages(locs)):
                entry = float(r["Close"])
                b = backtest(d, day, entry, target)
                prev = closes[loc - 1] if loc >= 1 else float("nan")
                ps, rs = gate_slack(r, prev, opens[loc], rsi_target,
                                    rsi_max, atr_mult, wrsi_min)
                rows.append({
                    "date": day.date().isoformat(), "ticker": t,
                    "entry": round(entry, 2),
                    "ema50": round(float(r["ema50"]), 2),
                    "atr": round(float(r["atr"]), 2),
                    "rsi": round(float(r["rsi"]), 1),
                    "wrsi": round(float(r["wrsi"]), 1),
                    "pslack": ps, "rslack": rs,
                    "days_to_%d%%" % int(target * 100):
                        b["days"] if b["days"] is not None else "never",
                    "max%": round(b["max"] * 100, 1),
                    "now%": round(b["now"] * 100, 1),
                    "page": "Y" if page else "-"})
        except Exception:
            continue
    return rows


def simulate(tickers: list, days: int, rsi_on: bool = True,
             rsi_target: float = 40.0, rsi_max: float = 45.0,
             atr_mult: float = 1.0, ignore_days: int = 5,
             wrsi_min: float = 55.0) -> list:
    """Live-paging replay per ticker over the last `days` calendar days.

    Same fetch shape as scan() but deeper (EMA50 needs the warmup). The
    state machine runs a 15-calendar-day lead-in before the report window
    so parks/pages just outside it still mute inside it. Text-days run
    through today so the freshest bar is evaluated.
    """
    need = max(days + 300, 365)  # same 1y warmup floor as scan()
    period = f"{need}d" if need <= 720 else "5y"
    px = batched_download(tickers, period=period, interval="1d")
    rows = []
    end = dt.date.today()
    for t in tickers:
        try:
            f = _frame(px, t)
            if len(f) < 80:
                continue
            d = add_ind(f)
            last_bar = d.index[-1].date()
            floor = last_bar - dt.timedelta(days=days)
            start = max(d.index[0].date(), floor - dt.timedelta(days=15))
            eval_days = []
            day = start
            stop = max(end, last_bar)
            while day <= stop:
                eval_days.append(day)
                day += dt.timedelta(days=1)
            for r in live_sim(d, eval_days, rsi_on, rsi_target, rsi_max,
                              atr_mult, ignore_days, wrsi_min):
                if dt.date.fromisoformat(r["text"]) > floor:
                    r["ticker"] = t
                    rows.append(r)
        except Exception:
            continue
    return rows


def run_simulate(tickers: list, a) -> int:
    print(f"live-replay {len(tickers)} names, last {a.days}d, "
          f"{'rsi=%g-%g wrsi>%g' % (a.rsi_target, a.rsi_max, a.wrsi_min)
             if a.rsi else 'rsi=off'}",
          flush=True)
    rows = simulate(tickers, a.days, a.rsi, a.rsi_target, a.rsi_max,
                    a.atr_mult, 5, a.wrsi_min)
    print(f"{'text':10} {'bar':10} {'ticker':6} {'entry':>8} {'ema50':>8} "
          f"{'atr':>6} {'rsi':>5} {'wrsi':>5} {'pslack':>6} {'rslack':>6} "
          f"{'status':>11} detail", flush=True)
    for r in sorted(rows, key=lambda r: (r["text"], r["ticker"])):
        print(f'{r["text"]:10} {r["bar"]:10} {r["ticker"]:6} '
              f'{r["entry"]:8.2f} {r["ema50"]:8.2f} {r["atr"]:6.2f} '
              f'{r["rsi"]:5.1f} {r["wrsi"]:5.1f} '
              f'{r["pslack"]:6.2f} {r["rslack"]:6.1f} '
              f'{r["status"]:>11} {r["detail"]}', flush=True)
    n = lambda s: sum(1 for r in rows if r["status"] == s)
    print(f"paged={n('PAGED')} muted-ignore={n('MUTED-IGNORE')} "
          f"muted-dedup={n('MUTED-DEDUP')}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", action="append", default=[],
                   help="repeatable, e.g. --ticker NVDA --ticker AAPL")
    g.add_argument("--pool", metavar="POOL",
                   help="one of sp100/sp500/spy/qqq/spy50/qqq50, or "
                        "'+'-joined union e.g. spy50+qqq50")
    ap.add_argument("--days", type=int, default=60,
                    help="signal window (trailing daily bars)")
    ap.add_argument("--target", type=float, default=0.20,
                    help="profit target fraction (0.20 = +20%)")
    ap.add_argument("--rsi", action=argparse.BooleanOptionalAction,
                    default=True, help="RSI14 inside target band gate")
    ap.add_argument("--rsi-target", type=float, default=40.0,
                    help="RSI floor for the setup")
    ap.add_argument("--rsi-max", type=float, default=45.0,
                    help="RSI ceiling for the setup")
    ap.add_argument("--atr-mult", type=float, default=1.0,
                    help="EMA50 proximity band in ATRs")
    ap.add_argument("--wrsi-min", type=float, default=55.0,
                    help="weekly RSI floor for the long (as-of weekly RSI)")
    ap.add_argument("--simulate", action="store_true",
                    help="replay live paging (PAGED/MUTED rows) instead of "
                         "the backtest list")
    a = ap.parse_args()
    if a.ticker:
        tickers = [t.upper() for t in a.ticker]
    else:
        try:
            tickers = load_pools(a.pool)
        except ValueError as e:
            ap.error(str(e))
    gate = f"rsi={a.rsi_target:.0f}-{a.rsi_max:.0f} " \
           f"wrsi>{a.wrsi_min:.0f}" if a.rsi else "rsi=off"
    if a.simulate:
        return run_simulate(tickers, a)
    print(f"simple-scan {len(tickers)} names, last {a.days}d, "
          f"target +{a.target * 100:.0f}%, {gate}", flush=True)
    rows = scan(tickers, a.days, a.target, a.rsi, a.rsi_target,
                a.rsi_max, a.atr_mult, a.wrsi_min)
    dk = "days_to_%d%%" % int(a.target * 100)
    print(f"{'date':10} {'ticker':6} {'entry':>8} {'ema50':>8} {'atr':>6} "
          f"{'rsi':>5} {'wrsi':>5} {'pslack':>6} {'rslack':>6} {dk:>8} "
          f"{'max%':>7} {'now%':>7} {'page':>4}", flush=True)
    for r in sorted(rows, key=lambda r: (r["date"], r["ticker"])):
        print(f'{r["date"]:10} {r["ticker"]:6} {r["entry"]:8.2f} '
              f'{r["ema50"]:8.2f} {r["atr"]:6.2f} {r["rsi"]:5.1f} '
              f'{r["wrsi"]:5.1f} {r["pslack"]:6.2f} {r["rslack"]:6.1f} '
              f'{str(r[dk]):>8} {r["max%"]:7.1f} {r["now%"]:7.1f} '
              f'{r["page"]:>4}', flush=True)
    hit = [r for r in rows if r[dk] != "never"]
    med = sorted(r[dk] for r in hit)
    med = med[len(med) // 2] if med else "-"
    print(f"setups={len(rows)} hit-target={len(hit)} "
          f"median-days={med}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
