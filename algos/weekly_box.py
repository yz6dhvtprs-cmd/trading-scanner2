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

Tiers (2026-10-09 replay, 1,418 paged alerts, TRAIN/TEST halves agree):
  BUY   above EMA10, EMA10 rising, print in the upper 40% of the box
        (pos >= 0.60): 37-39% reach +1 box height before the box low,
        vs 15% for bottom-of-box names. Each BUY name books a WBOX paper
        row in signal_log (entry = Friday print, stop = box low, target
        = box top + box height) so the Saturday review can grade it.
  WATCH above EMA10 but lower in the box, or EMA10 flat.
  EMA20 above EMA20 only (7% win when bought inside: watch the break).
Grades on BUY lines: A = box <= 1.3 weekly ATRs AND inside-week volume
below the anchor week's (53-60% win), B+ = one of the two, B = neither;
"*" = weekly MACD histogram rising on the last completed bar (36-41%).

Every alerted box (all tiers) also goes on four-week breakout watch in
data/wbox_watch.json; ops/intraday.py pages the first print above the
box top on RVOL >= 1.5 (never more than 3% above it) and books a
WBOX-BO paper row with the stop at the box midpoint.

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
ATR_N = 14              # weekly ATR window behind the box-width marker
POS_MIN = 0.60          # BUY tier: print in the upper 40% of the box
WIDTH_ATR_MAX = 1.30    # grade: box height <= this many weekly ATRs
VOL_DRY_MAX = 0.95      # grade: inside-week volume / anchor-week volume
RVOL_MIN = 1.5          # breakout page: day volume vs 20d mean (paced)
CHASE_MAX = 0.03        # breakout page: never more than 3% over the top
WATCH_DAYS = 28         # a Friday box stays on breakout watch this long
GRADE_RANK = {"A": 0, "B+": 1, "B": 2}


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


def weekly_atr(weeks: pd.DataFrame, n: int = ATR_N) -> float:
    """Mean true range over the last n completed weekly bars (NaN when
    the history is thinner than n)."""
    h = weeks["High"].astype(float)
    lo = weeks["Low"].astype(float)
    pc = weeks["Close"].astype(float).shift(1)
    tr = pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()],
                   axis=1).max(axis=1).dropna()
    if len(tr) < n:
        return float("nan")
    return float(tr.tail(n).mean())


def box_features(weeks: pd.DataFrame, price: float, lower: float,
                 upper: float, span: int = 4) -> dict:
    """Quality markers for a completed box, read on completed bars only
    (like the gates) plus the live print for `pos`:
      pos         where the print sits in the box, 0 = low .. 1 = high
      width_atr   box height in weekly ATR14s (anchor range / ATR)
      vol_ratio   mean volume of the completed inside weeks (n+1..n+2)
                  over the anchor week's (<1 = drying up; the replay
                  averaged three full weeks, live has two completed)
      hist_rising weekly MACD histogram up on the last completed bar
      e10_rising  weekly EMA10 above its value four bars back
    NaN-safe: thin history leaves a marker NaN/False, never raises."""
    width = float(upper) - float(lower)
    out = {"pos": float("nan"), "width_atr": float("nan"),
           "vol_ratio": float("nan"), "hist_rising": False,
           "e10_rising": False}
    if width <= 0:
        return out
    out["pos"] = round((float(price) - float(lower)) / width, 3)
    atr = weekly_atr(weeks)
    if atr == atr and atr > 0:  # not NaN
        out["width_atr"] = round(width / atr, 2)
    if "Volume" in weeks.columns:
        anchor_vol = float(weeks["Volume"].iloc[-span + 1])
        inside = weeks["Volume"].iloc[-span + 2:].astype(float)
        if anchor_vol > 0 and len(inside):
            out["vol_ratio"] = round(float(inside.mean()) / anchor_vol, 2)
    c = weeks["Close"].astype(float).dropna()
    if len(c) >= MACD_SLOW + MACD_SIG:
        macd = ema(c, MACD_FAST) - ema(c, MACD_SLOW)
        hist = macd - macd.ewm(span=MACD_SIG, adjust=False).mean()
        out["hist_rising"] = bool(hist.iloc[-1] > hist.iloc[-2])
    if len(c) >= 5:
        e10 = ema(c, 10)
        out["e10_rising"] = bool(e10.iloc[-1] > e10.iloc[-5])
    return out


def classify(r: dict) -> tuple:
    """(tier, grade) for an alert verdict. Tier: BUY = above EMA10, EMA10
    rising, pos >= POS_MIN; WATCH = above EMA10 otherwise; EMA20 = above
    EMA20 only. Grade (box quality, stamped on every tier for the log):
    A = tight box AND volume drying, B+ = one of the two, B = neither.
    NaN markers fail their test — thin history never upgrades."""
    g, f = r.get("gates") or {}, r.get("features") or {}
    nan = float("nan")
    tight = bool(f.get("width_atr", nan) <= WIDTH_ATR_MAX)
    dry = bool(f.get("vol_ratio", nan) < VOL_DRY_MAX)
    grade = "A" if (tight and dry) else ("B+" if (tight or dry) else "B")
    if not g.get("above_ema10"):
        return "EMA20", grade
    if f.get("pos", nan) >= POS_MIN and f.get("e10_rising"):
        return "BUY", grade
    return "WATCH", grade


def evaluate(weeks: pd.DataFrame, price: float, live: bool = True,
             span: int = 4) -> dict:
    """Verdict for one ticker. `weeks`: completed weekly bars ascending;
    the window is the last `span` stamps: first bar anchors, the rest
    must sit inside it, and `price` is the final print (Friday-12pm live
    or holiday fallback close). Returns box, gates, alert flag and the
    box boundaries for the text."""
    out = {"box": False, "alert": False, "lower": None, "upper": None,
           "price": round(price, 2), "live": live, "gates": None,
           "span": span, "features": None, "tier": None, "grade": None}
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
    out["features"] = (box_features(w, float(price), out["lower"],
                                    out["upper"], span)
                       if out["box"] else None)
    out["tier"], out["grade"] = classify(out) if out["alert"] else (
        None, None)
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


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WBOX_WATCH = os.path.join(ROOT_DIR, "data", "wbox_watch.json")
SIGNAL_LOG = os.path.join(ROOT_DIR, "data", "signal_log.csv")
OVERRIDES = os.path.join(ROOT_DIR, "data", "grade_overrides.csv")
LOG_COLS = ["date", "ticker", "strategy", "side", "entry", "stop", "target",
            "grade", "timeframe", "status", "outcome_R", "exit_reason",
            "regime", "news_flag", "notes"]


def load_watch() -> dict:
    """Friday boxes on breakout watch: {ticker: {lower, upper, week,
    tier, grade, price[, paged][, done]}}."""
    try:
        with open(WBOX_WATCH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_watch(watch: dict):
    try:
        with open(WBOX_WATCH, "w") as f:
            json.dump(watch, f)
    except Exception:
        pass


def prune_watch(watch: dict, today) -> dict:
    """Drop boxes older than WATCH_DAYS (the replay measured breakouts
    within four weeks of the alert; later ones are a different setup)."""
    out = {}
    for t, b in watch.items():
        try:
            age = (today - dt.date.fromisoformat(str(b["week"]))).days
        except Exception:
            continue
        if 0 <= age <= WATCH_DAYS:
            out[t] = b
    return out


def wbox_row(t: str, lower: float, upper: float, entry: float, today,
             grade: str, strategy: str = "WBOX", note: str = "") -> dict:
    """One signal_log paper row. WBOX (Friday BUY zone): stop = box low,
    target = box top + box height — the replay's definition. WBOX-BO
    (breakout page): same target, stop = box midpoint."""
    width = float(upper) - float(lower)
    stop = float(lower) if strategy == "WBOX" else float(lower) + width / 2
    return {"date": today.isoformat(), "ticker": t, "strategy": strategy,
            "side": "long", "entry": round(float(entry), 2),
            "stop": round(stop, 2), "target": round(float(upper) + width, 2),
            "grade": grade, "timeframe": "weekly", "status": "ENTERED",
            "outcome_R": "", "exit_reason": "", "regime": "",
            "news_flag": "",
            "notes": f"box {float(lower):.2f}-{float(upper):.2f}"
                     + (f" {note}" if note else "")}


def _box_key(notes) -> str:
    s = str(notes)
    return s.split(" ")[1] if s.startswith("box ") and " " in s else ""


def log_signal_rows(rows: list, path: str = None) -> int:
    """Append paper rows to signal_log.csv (header on a fresh file). A
    row whose ticker+strategy+box is already open is skipped, so a
    re-run of the same Friday never double-books."""
    path = path or SIGNAL_LOG
    have = set()
    if os.path.exists(path):
        try:
            old = pd.read_csv(path)
            if len(old):
                for _, r in old[old["outcome_R"].isna()].iterrows():
                    have.add((str(r["ticker"]), str(r["strategy"]),
                              _box_key(r["notes"])))
        except Exception:
            pass
    fresh = [r for r in rows if (r["ticker"], r["strategy"],
                                 _box_key(r["notes"])) not in have]
    if not fresh:
        return 0
    pd.DataFrame(fresh, columns=LOG_COLS).to_csv(
        path, mode="a", header=not os.path.exists(path), index=False)
    return len(fresh)


def wbox_paused() -> bool:
    """True when the Saturday review demoted WBOX long to C (paused):
    BUY names still print, but no new paper rows are booked."""
    try:
        o = pd.read_csv(OVERRIDES)
        hit = o[(o["strategy"] == "WBOX") & (o["side"] == "long")]
        return bool(len(hit) and str(hit["to"].iloc[-1]).startswith("C"))
    except Exception:
        return False


def breakout_decisions(watch: dict, prints: dict, avg20: dict, today,
                       elapsed: float = 1.0) -> tuple:
    """Breakout pages off the Friday boxes (pure, offline-testable).

    prints[t] = (last, day_open, day_volume); avg20[t] = mean volume of
    the last 20 completed days; `elapsed` = fraction of the session
    done, so a 10am print is judged on 10am-paced volume. Returns
    (pages, failed, missed): pages = dicts for the text/log, failed =
    printed under the box low (box broke down, watch dropped), missed =
    already more than CHASE_MAX over the top (no chasing). Boxes already
    paged/done, aged out, without a print or with unknown volume never
    page — a NaN RVOL is a no, not a yes."""
    pages, failed, missed = [], [], []
    el = min(max(float(elapsed), 0.1), 1.0)
    for t, b in watch.items():
        if b.get("paged") or b.get("done"):
            continue
        try:
            age = (today - dt.date.fromisoformat(str(b["week"]))).days
        except Exception:
            age = 0
        if age > WATCH_DAYS:
            continue
        q = prints.get(t)
        if q is None:
            continue
        last, vol = float(q[0]), q[2]
        lo, up = float(b["lower"]), float(b["upper"])
        if last < lo:
            failed.append(t)
            continue
        if last <= up:
            continue
        if last > up * (1 + CHASE_MAX):
            missed.append(t)
            continue
        try:
            rvol = float(vol) / (float(avg20.get(t)) * el)
        except Exception:
            rvol = float("nan")
        if not rvol >= RVOL_MIN:
            continue
        width = up - lo
        pages.append({"ticker": t, "last": round(last, 2),
                      "rvol": round(rvol, 1), "lower": lo, "upper": up,
                      "stop": round(lo + width / 2, 2),
                      "target": round(up + width, 2),
                      "tier": b.get("tier", ""), "grade": b.get("grade", "")})
    return pages, failed, missed


def breakout_text(pages: list) -> str:
    """One batched text per run: ticker, print, paced RVOL, the midpoint
    stop and the +1-box-height target."""
    lines = ["WBOX breakout (stop = box mid):"]
    for p in pages:
        lines.append(f"{p['ticker']} {p['last']:.2f} rvol {p['rvol']:.1f} "
                     f"stop {p['stop']:.2f} tgt {p['target']:.2f}")
    return "\n".join(lines)


def alert_line(t: str, r: dict) -> str:
    """One log row per alert (box detail lives in logs, not texts)."""
    g = r["gates"]
    star = ", \u2605>EMA10" if g["above_ema10"] else ""
    src = "live" if r["live"] else "fallback-close"
    f = r.get("features") or {}
    nan = float("nan")
    hist = " hist+" if f.get("hist_rising") else ""
    return (f"{t} box {r['lower']:.2f}\u2013{r['upper']:.2f}, "
            f">EMA20, MACD bull>0{star} ({src}) "
            f"{r.get('tier') or '-'}/{r.get('grade') or '-'} "
            f"pos={f.get('pos', nan):.2f} w/atr={f.get('width_atr', nan):.2f} "
            f"vol={f.get('vol_ratio', nan):.2f}{hist}")


def buy_label(t: str, grade: str, hist_rising: bool) -> str:
    """BUY-zone text line: ticker, grade, '*' when the histogram rises."""
    return f"{t} {grade}{'*' if hist_rising else ''}"


def weekly_batch_text(buy: list, watch: list, ema20: list,
                      span: int = 4, paused: bool = False) -> str:
    """The Friday text: three disjoint sections, empty ones skipped.
    BUY-zone lines ("DELL A*") first, then the above-EMA10 watch names,
    then the above-EMA20-only names. No ticker ever repeats."""
    head = f"Weekly Consolidation({span} bars)"
    lines: list = []
    if buy:
        lines += [f"{head} BUY zone (top of box, EMA10 rising)"
                  f"{' [PAUSED: paper off]' if paused else ''}:"] + buy
    if watch:
        lines += [("Watch (above EMA10):" if lines
                   else f"{head} watch (above EMA10):")] + watch
    if ema20:
        lines += [("Only above EMA20:" if lines
                   else f"{head} above EMA20:")] + ema20
    return "\n".join(lines)


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
    """Completed bars strictly before the week containing asof. On the
    eval day the current week is still forming — exactly what the
    Friday-noon live path sees — so asof's own week never counts as an
    inside bar; its Friday print is the live/fallback price instead.
    (Counting asof's week as completed AND as the print scored a 3-week
    pattern and reproduced only 7 of the 17 live alerts of 2026-10-02;
    this convention reproduces 15.)"""
    start = asof - dt.timedelta(days=asof.weekday())  # Monday of asof
    return weeks[weeks.index.date < start]


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
                # week i is the forming week, scored at its Friday close;
                # only the bars before it are completed (live mirror)
                past = weeks.iloc[:i]
                r = evaluate(past, float(weeks["Close"].iloc[i]),
                             live=False, span=a.weeks)
                if r["alert"]:
                    n_alert += 1
                    if a.no_refire and not apply_refire(
                            fired, t, r["lower"], r["upper"],
                            str(weeks.index[i].date())):
                        n_refire += 1
                        continue
                    star = " *" if r["gates"]["above_ema10"] else ""
                    if not a.ema10_only or star:
                        hits.append(f"{week_ending(weeks.index[i])} "
                                    f"{r['lower']:.0f}-{r['upper']:.0f}"
                                    f"{star} {r['tier']}/{r['grade']}")
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
        paused = wbox_paused()
        hits, cut = [], 0
        buy, watch_names, ema20, rows, wnew = [], [], [], [], {}
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
                f, tier, grade = r["features"], r["tier"], r["grade"]
                if tier == "BUY":
                    buy.append((GRADE_RANK.get(grade, 9), t,
                                buy_label(t, grade, f["hist_rising"])))
                    rows.append(wbox_row(
                        t, r["lower"], r["upper"], r["price"], asof,
                        grade, note=f"pos={f['pos']:.2f} "
                        f"w/atr={f['width_atr']:.2f} "
                        f"vol={f['vol_ratio']:.2f}"
                        f"{' hist+' if f['hist_rising'] else ''}"))
                elif tier == "WATCH":
                    watch_names.append(t)
                else:
                    ema20.append(t)
                wnew[t] = {"lower": r["lower"], "upper": r["upper"],
                           "week": asof.isoformat(), "tier": tier,
                           "grade": grade, "price": r["price"]}
        if a.no_refire and a.channels != "dry":
            save_fired(fired)  # dry runs stay side-effect-free
        n_logged = 0
        if a.channels != "dry" and wnew:
            w = prune_watch(load_watch(), asof)
            w.update(wnew)  # every tier goes on four-week breakout watch
            save_watch(w)
            if rows and not paused:
                n_logged = log_signal_rows(rows)
        print(f"WEEKLY BOX {len(hits)}/{len(pool)}"
              f"{f' ({cut} refires cut)' if a.no_refire else ''}: "
              f"{len(buy)} buy, {len(watch_names)} watch, "
              f"{len(ema20)} ema20-only; {n_logged} paper rows logged"
              f"{' [WBOX paused]' if paused else ''}", flush=True)
        if hits:
            from notify import alert, load_config  # noqa: E402
            cfg = load_config()
            body = weekly_batch_text(
                [lbl for _, _, lbl in sorted(buy)], sorted(watch_names),
                sorted(ema20), span=a.weeks, paused=paused)
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
                                f"{star} {r['tier']}/{r['grade']}")
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
        if asof < dt.date.today():
            weeks = weeks_asof(weeks, asof)  # past date: no peeking
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
        if r["features"]:
            f = r["features"]
            print(f"  tier={r['tier']} grade={r['grade']} "
                  f"pos={f['pos']:.2f} width/ATR={f['width_atr']:.2f} "
                  f"vol={f['vol_ratio']:.2f} hist_rising="
                  f"{f['hist_rising']} ema10_rising={f['e10_rising']}",
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
