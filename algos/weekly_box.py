"""Weekly anchor-box consolidation signal (runs Friday 12pm PT).

Pattern (4 weeks, no ATR math): anchor week n sets the box; weeks n+1
and n+2 keep their BODIES inside it (open AND close within anchor low
to anchor high, edges inclusive — wicks may poke out); week n+3 is
scored live — Friday 12pm current price inside the box completes it.
If Friday is a holiday, week n+3 falls back to the last daily close
(Thursday's print, equivalently the prior half-day logic).

Alert requires the regime gates on top of the box, all on completed
weekly bars (slow lines never read a forming bar):
  - current/live price above weekly EMA20,
  - weekly MACD line above its signal AND above 0.
Above weekly EMA10 is not required but prints as a strength marker.

Usage:
    python algos/weekly_box.py --ticker DELL
    python algos/weekly_box.py --ticker DELL --live 568.0
    python algos/weekly_box.py --scan --channels dry
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple_scan import ema, load_pool  # noqa: E402

MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9


def anchor_box_complete(anchor_high: float, anchor_low: float,
                        closes: list, bars=None) -> bool:
    """Box verdict: every follow-up close (c1, c2, live) sits inside the
    anchor week's [low, high], edges inclusive. When `bars` (the n+1/n+2
    week rows) is given, each week's open AND close must sit inside too —
    wicks may poke out, bodies may not."""
    if not all(anchor_low <= c <= anchor_high for c in closes):
        return False
    if bars is not None:
        for _, r in bars.iterrows():
            if not (anchor_low <= r["Open"] <= anchor_high
                    and anchor_low <= r["Close"] <= anchor_high):
                return False
    return True


def macd_line(closes: pd.Series) -> tuple:
    """Weekly MACD line and signal (standard 12/26/9 on closes)."""
    macd = ema(closes, MACD_FAST) - ema(closes, MACD_SLOW)
    signal = macd.ewm(span=MACD_SIG, adjust=False).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])


def weekly_gates_for(weekly_closes: pd.Series, price: float) -> dict:
    """Regime gates on completed weekly bars + a live/completed price.

    Returns above_ema20 / above_ema10 (price vs weekly EMA lines) and
    macd_bull (line > signal) / macd_pos (line > 0). Needs 40+ weekly
    bars for settled lines; thin history gates shut (False, not error).
    """
    out = {"above_ema20": False, "above_ema10": False,
           "macd_bull": False, "macd_pos": False, "ema20": None,
           "ema10": None, "macd": None, "signal": None}
    c = weekly_closes.astype(float).dropna()
    if len(c) < 40:
        return out
    e20, e10 = float(ema(c, 20).iloc[-1]), float(ema(c, 10).iloc[-1])
    m, s = macd_line(c)
    out.update({"ema20": round(e20, 2), "ema10": round(e10, 2),
                "macd": round(m, 2), "signal": round(s, 2),
                "above_ema20": bool(price > e20),
                "above_ema10": bool(price > e10),
                "macd_bull": bool(m > s), "macd_pos": bool(m > 0)})
    return out


def evaluate(weeks: pd.DataFrame, price: float, live: bool = True,
             span: int = 4) -> dict:
    """Verdict for one ticker. `weeks`: completed weekly bars ascending;
    the window is the last `span` stamps: first bar anchors, the rest
    must sit inside it, and `price` is the final print (Friday-12pm live
    or holiday fallback close). Returns box, gates, alert flag and the
    box boundaries for the text."""
    out = {"box": False, "alert": False, "lower": None, "upper": None,
           "price": round(price, 2), "live": live, "gates": None,
           "span": span}
    w = weeks.dropna(subset=["Close", "High", "Low", "Open"])
    if span < 3 or len(w) < span - 1:
        return out
    anchor = w.iloc[-span + 1]
    closes = [float(c) for c in w["Close"].iloc[-span + 2:].tolist()]
    closes.append(float(price))
    mid = w.iloc[-span + 2:]
    out["lower"], out["upper"] = round(float(anchor["Low"]), 2), round(
        float(anchor["High"]), 2)
    out["box"] = anchor_box_complete(float(anchor["High"]),
                                     float(anchor["Low"]), closes,
                                     bars=mid)
    out["gates"] = weekly_gates_for(w["Close"], float(price))
    g = out["gates"]
    out["alert"] = bool(out["box"] and g["above_ema20"] and
                        g["macd_bull"] and g["macd_pos"])
    return out


WBOX_STATE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "weekly_box_state.json")


def boxes_overlap(a_lower: float, a_upper: float, b_lower: float,
                  b_upper: float) -> bool:
    """Same-grind test: two boxes sharing any price have the ticker
    still working the same consolidation."""
    return a_lower <= b_upper and b_lower <= a_upper


def apply_refire(fired: dict, t: str, lower: float, upper: float,
                 week: str) -> bool:
    """True if this box may fire (records it as the ticker's last fired
    box); False when it overlaps the last fired box — week 2+ of the
    same grind stays silent until price escapes into a fresh box."""
    prev = fired.get(t)
    if prev and boxes_overlap(prev["lower"], prev["upper"],
                              lower, upper):
        return False
    fired[t] = {"lower": lower, "upper": upper, "week": week}
    return True


def load_fired() -> dict:
    try:
        with open(WBOX_STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_fired(fired: dict):
    try:
        with open(WBOX_STATE, "w") as f:
            json.dump(fired, f)
    except Exception:
        pass


def alert_line(t: str, r: dict) -> str:
    """One log row per alert (box detail lives in logs, not texts)."""
    g = r["gates"]
    star = ", \u2605>EMA10" if g["above_ema10"] else ""
    src = "live" if r["live"] else "fallback-close"
    return (f"{t} box {r['lower']:.2f}\u2013{r['upper']:.2f}, "
            f">EMA20, MACD bull>0{star} ({src})")


def weekly_batch_text(strong: list, rest: list,
                      span: int = 4) -> str:
    """The Friday text: above-EMA10 names first, then the EMA20-only
    remainder. Disjoint by construction — no ticker ever repeats. With
    no strong names it degrades to a single above-EMA20 list."""
    if strong:
        lines = [f"Weekly Consolidation({span} bars) above EMA10:"]
        lines += strong
        if rest:
            lines += ["Only above EMA20:"] + rest
        return "\n".join(lines)
    return "\n".join(
        [f"Weekly Consolidation({span} bars) above EMA20:"] + rest)


def session_now() -> bool:
    """True inside the 06:30-13:00 PT tape (same window as intraday)."""
    from market_calendar import market_open  # noqa: E402 (shared session rule)
    return market_open()


def friday_prints(tickers: list) -> dict:
    """Today's (day-open, live) per ticker from one small daily fetch.
    Absent names (holiday, no print) fall back to cached daily closes."""
    from simple_scan import batched_download  # noqa: E402
    try:
        px = batched_download(tickers, period="1d", interval="1d")
    except Exception:
        return {}
    out = {}
    for t in tickers:
        try:
            f = px[t] if isinstance(px.columns, pd.MultiIndex) else px
            f = f.dropna(subset=["Close"])
            if len(f):
                out[t] = (float(f["Close"].iloc[-1]),
                          float(f["Open"].iloc[-1]))
        except Exception:
            continue
    return out


def replay_universe(tickers: list) -> list:
    """Named tickers (uppercased) or the live scan pool (spy+qqq master
    UNION screener adds) when none."""
    if tickers:
        return [x.upper() for x in tickers]
    from screener import scan_pool  # noqa: E402 (union pool)
    return scan_pool()


def weeks_asof(weeks: pd.DataFrame, asof: dt.date) -> pd.DataFrame:
    """Bars at/before the eval date (a Friday close completes its week;
    nothing past asof is visible — no peeking)."""
    return weeks[weeks.index.date <= asof]


def week_ending(monday) -> dt.date:
    """Display date for a Monday-indexed bar: the Friday it completes."""
    return (pd.Timestamp(monday) + pd.Timedelta(days=4)).date()


def ticker_verdict(t: str, weeks: pd.DataFrame, daily: pd.DataFrame,
                   live_print=None, asof=None, span: int = 4) -> dict:
    """Assemble one ticker's verdict. live_print=(live, open) or None to
    use the last daily close at/before asof (holiday fallback rule)."""
    asof = asof or dt.date.today()
    if live_print is not None:
        return evaluate(weeks, live_print[0], live=True, span=span)
    closes = daily[daily.index.date <= asof]
    if not len(closes):
        return evaluate(weeks, float("nan"), live=False, span=span)
    return evaluate(weeks, float(closes["Close"].iloc[-1]), live=False,
                    span=span)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", action="append", default=[],
                    help="manual check, e.g. --ticker DELL")
    ap.add_argument("--live", type=float, default=None,
                    help="override week-n+3 print (manual mode)")
    ap.add_argument("--date", default=None,
                    help="eval date YYYY-MM-DD (default today)")
    ap.add_argument("--scan", action="store_true",
                    help="full spy+qqq pool scan + one batched text")
    ap.add_argument("--replay", type=int, default=0, metavar="DAYS",
                    help="no-lookahead replay over the pool: each past "
                    "week-end scored on bars available then (report only)")
    ap.add_argument("--ema10-only", action="store_true",
                    help="replay lists only above-EMA10 hits (the * rows)")
    ap.add_argument("--no-refire", action="store_true",
                    help="skip boxes overlapping the ticker's last fired "
                    "box (one alert per grind)")
    ap.add_argument("--on", action="append", default=[], metavar="DATE",
                    help="eval date(s) YYYY-MM-DD, repeatable, pool or "
                    "ticker. Report only, never texts; past dates score "
                    "on closes (holiday-fallback rule)")
    ap.add_argument("--seed", action="store_true",
                    help="with --on + --no-refire: persist the fired "
                    "boxes as ref points (go-live seeding, still no "
                    "texts)")
    ap.add_argument("--weeks", type=int, default=4,
                    help="consolidation window in weekly stamps: first "
                    "bar anchors, rest must sit inside (default 4)")
    ap.add_argument("--channels", default="dry",
                    help="notify channels (imessage for the Friday slot)")
    a = ap.parse_args()
    from daily_cache import ensure, load, pool_tickers  # noqa: E402
    asof = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    if a.replay:
        from daily_cache import load  # noqa: E402
        pool = replay_universe(a.ticker)
        floor = asof - dt.timedelta(days=a.replay)
        n_alert, n_boxonly, n_tickers, n_refire = 0, 0, 0, 0
        for t in pool:
            weeks = load(t, "1w")
            hits = []
            fired = {}
            for i in range(3, len(weeks)):
                if weeks.index[i].date() < floor:
                    continue
                past = weeks.iloc[:i + 1]  # nothing past week i: no peek
                r = evaluate(past, float(past["Close"].iloc[-1]),
                             live=False, span=a.weeks)
                if r["alert"]:
                    n_alert += 1
                    if a.no_refire and not apply_refire(
                            fired, t, r["lower"], r["upper"],
                            str(past.index[i].date())):
                        n_refire += 1
                        continue
                    star = " *" if r["gates"]["above_ema10"] else ""
                    if not a.ema10_only or star:
                        hits.append(f"{week_ending(past.index[i])} "
                                    f"{r['lower']:.0f}-{r['upper']:.0f}"
                                    f"{star}")
                elif r["box"]:
                    n_boxonly += 1
            if hits:
                n_tickers += 1
                print(f"{t}: " + "; ".join(hits), flush=True)
        print(f"replay {a.replay}d: {n_alert} alerts, {n_boxonly} "
              f"box-only, {n_refire} refires cut, "
              f"{n_tickers}/{len(pool)} tickers", flush=True)
        return 0
    if a.scan:
        from screener import scan_pool  # noqa: E402 (union pool)
        pool = scan_pool()
        prints = friday_prints(pool) if session_now() else {}
        fired = load_fired() if a.no_refire else {}
        hits, strong, cut = [], [], 0
        for t in pool:
            ensure([t], today=asof, tf="1w")
            weeks = load(t, "1w")
            daily = load(t, "1d")
            if not len(weeks):
                continue
            if t in prints:
                r = evaluate(weeks, prints[t][0], live=True,
                             span=a.weeks)
            else:
                r = ticker_verdict(t, weeks, daily, asof=asof,
                                   span=a.weeks)
            if r["alert"]:
                if a.no_refire and not apply_refire(
                        fired, t, r["lower"], r["upper"],
                        asof.isoformat()):
                    cut += 1
                    continue
                print(f"  {alert_line(t, r)}", flush=True)
                hits.append(t)
                if r["gates"]["above_ema10"]:
                    strong.append(t)
        if a.no_refire and a.channels != "dry":
            save_fired(fired)  # dry runs stay side-effect-free
        print(f"WEEKLY BOX {len(hits)}/{len(pool)}"
              f"{f' ({cut} refires cut)' if a.no_refire else ''}",
              flush=True)
        if hits:
            from notify import alert, load_config  # noqa: E402
            cfg = load_config()
            rest = [t for t in sorted(hits) if t not in strong]
            body = weekly_batch_text(sorted(strong), rest,
                                     span=a.weeks)
            print(body, flush=True)
            for r in alert(body, [c.strip() for c in
                                  a.channels.split(",")], cfg,
                           title="WBOX"):
                print("  ", r, flush=True)
        return 0
    if a.on:
        from daily_cache import load  # noqa: E402
        dates = sorted({dt.date.fromisoformat(d) for d in a.on})
        names = replay_universe(a.ticker)
        fired = {}
        for D in dates:
            live_now = D == dt.date.today() and session_now()
            prints = friday_prints(names) if live_now else {}
            hits, cut = [], 0
            for t in names:
                past = weeks_asof(load(t, "1w"), D)
                daily = load(t, "1d")
                if not len(past):
                    continue
                if t in prints:
                    r = evaluate(past, prints[t][0], live=True,
                                 span=a.weeks)
                else:
                    r = ticker_verdict(t, past, daily, asof=D,
                                       span=a.weeks)
                if not r["alert"]:
                    continue
                if a.no_refire and not apply_refire(
                        fired, t, r["lower"], r["upper"],
                        D.isoformat()):
                    cut += 1
                    continue
                star = " *" if r["gates"]["above_ema10"] else ""
                if not a.ema10_only or star:
                    hits.append(f"{t} {r['lower']:.0f}-{r['upper']:.0f}"
                                f"{star}")
            print(f"{D} WEEKLY BOX {len(hits)}/{len(names)}"
                  f"{f' ({cut} refires cut)' if a.no_refire else ''}"
                  + (": " + ", ".join(sorted(hits)) if hits else ""),
                  flush=True)
        if a.seed:
            if not a.no_refire:
                print("seed needs --no-refire (nothing persisted)",
                      flush=True)
            else:
                save_fired(fired)
                print(f"seeded {len(fired)} ref points", flush=True)
        return 0
    if not a.ticker:
        ap.error("give --ticker or --scan")
    for t in [x.upper() for x in a.ticker]:
        ensure([t], today=asof, tf="1w")
        weeks, daily = load(t, "1w"), load(t, "1d")
        if a.live is not None:
            r = evaluate(weeks, a.live, live=True, span=a.weeks)
        elif session_now() and asof == dt.date.today():
            p = friday_prints([t]).get(t)
            r = ticker_verdict(t, weeks, daily,
                               live_print=p, asof=asof, span=a.weeks)
        else:
            r = ticker_verdict(t, weeks, daily, asof=asof,
                               span=a.weeks)
        g = r["gates"]
        print(f"{t}: box={r['box']} alert={r['alert']} "
              f"price={r['price']} ({'live' if r['live'] else 'fallback'}) "
              f"box {r['lower']}\u2013{r['upper']}", flush=True)
        print(f"  EMA20 {g['ema20']} above={g['above_ema20']} | "
              f"EMA10 {g['ema10']} above={g['above_ema10']} | "
              f"MACD {g['macd']} vs {g['signal']} "
              f"bull={g['macd_bull']} >0={g['macd_pos']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
