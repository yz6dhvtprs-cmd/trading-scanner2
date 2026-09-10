"""Backtest the ARMED analyzer over historical 15-min bars (read-only).

Walks every 15m bar of the window and runs the live scanner/analyze.py
gates at each bar (directional Long, possible reversal, possible rejection),
with strictly causal data: completed daily bars + a forming daily bar rebuilt
from that day's 15m bars so far (exactly what the live scanner sees
intraday), completed 1h bars only, and indicators recomputed per bar.
Prints one line per NEWLY appearing state (live change-only semantics).

Usage:
    python scanner/backtest_analyzer.py [--ticker TICKER] [--days N]
        [--debug 1,3-4] [--grade B]
Missing args are prompted. No alerts, no state writes, no lookahead.
Every printed setup gets a unique #id; --debug replays the named setups
with the full per-gate decision trace. --grade sets the minimum grade
shown (B means B and higher: B, B+, A, A+); default shows all trades.

Continuations surface as Long (breakout/pullback triggers); there is no
SHORT leg while shorts stay paused per algo.json (short bias reads print
as Possible Rejection).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from analyze import analyze, flat  # noqa: E402
from combos import add_features  # noqa: E402
from indicators2 import add_extra  # noqa: E402
from reversals import (REV_ALGOS, r1_123, r2_zigzag_tema,  # noqa: E402
                       r3_trendline, r4_channel, r5_maslope, r6_donchian,
                       r7_macddiv, r8_obv, r9_climax, r10_volosc,
                       rps_confirm)

# algo -> backtest-namespace global holding its routine (module-global
# lookup so tests can stub routines by patching _bt.<name>).
ROUTER = {"R1": "r1_123", "R2": "r2_zigzag_tema", "R3": "r3_trendline",
          "R4": "r4_channel", "R5": "r5_maslope", "R6": "r6_donchian",
          "R7": "r7_macddiv", "R8": "r8_obv", "R9": "r9_climax",
          "R10": "r10_volosc"}
assert set(ROUTER) == set(REV_ALGOS), "router/registry drift"

# Tournament-selected pair for --algo RPS (20d, AAPL/MSFT/TSLA/NVDA/MSTR):
# RP=R3 trendline-break (4 wins, 4-1 decided = 80%, tied-most wins);
# RS=R10 VO-divergence (4 wins itself; co-won 2 of R3's 4 winners: the
# same-bar AAPL 08-12 long and the next-day NVDA 08-19 short).
# v2 (this rev): R3+R10 agreement + 15m-RSI washout gate + 30m/1H turn
# confirm (rps_confirm) + next-15m-open fills. 60d sweep over 13 ETF
# tickers: washout 30/70 -> 9-3 decided (75%, +0.50R); 35/65 -> 11-4
# (73%, +0.47R); 40/60 -> 12-7 (63%, +0.26R). Default 30/70.
RPS_PAIR = ("R3", "R10")

_LONG_STATES = {"Long", "Possible upcoming Reversal"}


def _sameside(s1: str, s2: str) -> bool:
    return (s1 in _LONG_STATES) == (s2 in _LONG_STATES)

PT = "America/Los_Angeles"
MIN_DAILY = 121   # swing_levels reads a 120-bar lookback
MIN_TF = 35       # ema21 + MACD(12,26,9) warmup on 1h/15m
H1_WARMUP_DAYS = 15  # extra 1h history behind the window, indicators only
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ALGO = None


def _algo_variants():
    """(strategy, side) -> grade from algo.json (the 10y backtest verdicts
    the nightly scanner alerts with; read live so re-grades apply)."""
    global _ALGO
    if _ALGO is None:
        try:
            algo = json.load(open(os.path.join(ROOT, "algo.json")))
            _ALGO = {(v["strategy"], v["side"]): v["grade"]
                     for v in algo.get("variants", [])}
        except Exception:
            _ALGO = {}
    return _ALGO


def grade_of(state: str, note: str) -> tuple:
    """(grade, why), mirroring the main analysis exactly:
    Long takes its backtested variant's grade from algo.json (breakout=A,
    pullback=B+); short-bias rejection sits at C (paused) per algo.json +
    GRADES.md; reversal is a valid pattern with no backtested variant, so B
    (thin/unproven) per the GRADES.md rubric."""
    n = (note or "").lower()
    if state == "Long":
        for strat in ("breakout", "pullback"):
            if strat in n:
                g = _algo_variants().get((strat, "long"), "?")
                return g, f"algo.json {strat}-long variant"
        return "B", "long trigger is not a backtested variant"
    if state == "Possible upcoming Rejection":
        return "C", "short bias paused per algo.json; GRADES.md shorts=C"
    if state == "Possible upcoming Reversal":
        return "B", "valid pattern, no backtested variant (thin/unproven)"
    return "B", "no backtested variant"


FWD_ROOM_DAYS = 40  # extra 15m history before the walk window. The walk
                  # then starts with full indicator warmup (no skipped
                  # bars); forward scoring uses bars after each signal
                  # (position-dependent; tail signals report 'recent').


def window_bars(m15_full: pd.DataFrame, days: int) -> pd.DataFrame:
    """15m bars on the last `days` distinct dates (the walk window)."""
    dates = sorted(set(m15_full.index.date))[-days:]
    keep = set(dates)
    return m15_full[[d in keep for d in m15_full.index.date]]


def fetch(ticker: str, days: int):
    """(daily_full, h1_full, m15_full), oldest-first, tz-aware. The 15m
    fetch overshoots the walk window by FWD_ROOM_DAYS (15m caps at 60d);
    main() walks only the last `days` of it via window_bars()."""
    import yfinance as yf
    d = flat(yf.download(ticker, period="1y", interval="1d",
                         auto_adjust=True, progress=False, threads=False))
    h1 = flat(yf.download(ticker, period=f"{days + H1_WARMUP_DAYS}d",
                          interval="1h", auto_adjust=True, progress=False,
                          threads=False))
    m15 = flat(yf.download(ticker,
                           period=f"{min(60, days + FWD_ROOM_DAYS)}d",
                           interval="15m", auto_adjust=True, progress=False,
                           threads=False))
    out = []
    for f in (d, h1, m15):
        f = f.dropna(subset=["Close"]).sort_index()
        if f.index.tz is None:
            f = f.tz_localize("America/New_York")
        out.append(f)
    return out[0], out[1], out[2]


def prep_tf(f: pd.DataFrame) -> pd.DataFrame:
    """ema21 + MACD on a truncated intraday frame (mirrors live main)."""
    f = f.copy()
    f["ema21"] = f["Close"].ewm(span=21, adjust=False).mean()
    m = f["Close"].ewm(span=12, adjust=False).mean() - \
        f["Close"].ewm(span=26, adjust=False).mean()
    f["macd"] = m
    f["macd_sig"] = m.ewm(span=9, adjust=False).mean()
    return f


def slices_for(d_full: pd.DataFrame, h1_full: pd.DataFrame,
               m15_full: pd.DataFrame, ts: pd.Timestamp):
    """Causal frames as of 15m bar OPEN ts: daily = completed days + a
    forming bar rebuilt from today's 15m bars so far; 1h = bars completed
    by this 15m bar's close; 15m = bars through ts."""
    day = ts.date()
    dhist = d_full[d_full.index.date < day].tail(199)
    daybars = m15_full[(m15_full.index.date == day) &
                       (m15_full.index <= ts)]
    if len(daybars) == 0:
        return None
    forming = pd.DataFrame([{
        "Open": float(daybars["Open"].iloc[0]),
        "High": float(daybars["High"].max()),
        "Low": float(daybars["Low"].min()),
        "Close": float(daybars["Close"].iloc[-1]),
        "Volume": float(daybars["Volume"].sum()),
    }], index=[daybars.index[-1]])
    d = pd.concat([dhist, forming]).tail(200)
    d = add_extra(add_features(d))
    h1 = prep_tf(h1_full[h1_full.index <= ts - pd.Timedelta(minutes=45)])
    m15 = prep_tf(m15_full[m15_full.index <= ts])
    return d, h1, m15


COOLDOWN_BARS = 26  # same pattern key can't re-fire within ~1 session:
                    # intraday trigger/pivot flicker around a level is one
                    # setup (one #id), a re-break days later is a new one


def _rnum(a: str) -> int:
    try:
        return int(a[1:])
    except (ValueError, IndexError):
        return 10 ** 6


def _canon_order(algos: set) -> list:
    """Deterministic per-bar eval order: OLD, singles ascending, combos."""
    canon = ["OLD"] + sorted(REV_ALGOS, key=_rnum)
    return [x for x in canon if x in algos] + \
        sorted(x for x in algos if "+" in x or x == "RPS")


def _rps_algo() -> str:
    """v2 tag. RPS1 (legacy v1: bare R3+R10 agreement, routine entries)
    keeps the pair tag so both can run side by side for A/B."""
    return "RPS"


def _rps_pair_tag() -> str:
    return f"{RPS_PAIR[0]}+{RPS_PAIR[1]}" if RPS_PAIR else ""


def _eval_rps(ticker: str, d: pd.DataFrame, h1: pd.DataFrame,
              m15: pd.DataFrame, trace: dict | None) -> list:
    """RPS two-step: daily R3+R10 agreement, then the 15m RSI washout gate
    with 30m/1H turn confirmation (rps_confirm). Entry is lifted to the
    next 15m open in walk() so fills always print after the alert."""
    algo = _rps_algo()
    sub = {"checks": [], "fired": []} if trace is not None else None
    paired = _eval_combo(_rps_pair_tag(), ticker, d, h1, m15, sub)
    out, gates = [], []
    for p in paired:
        side = "up" if p.get("state", "") in _LONG_STATES else "dn"
        try:
            ok, why = rps_confirm(m15, h1, side)
        except Exception:
            ok, why = False, "rsi-err"
        gates.append(f"RPS {side}: 2-step {why} -> "
                     f"{'PASS' if ok else 'skip'}")
        if not ok:
            continue
        q = dict(p)
        q["algo"] = algo
        q["note"] = f'{p.get("note", "")} & {why}'
        out.append(q)
    if trace is not None:
        trace.update({"algo": algo,
                      "checks": (sub.get("checks", []) if sub else []) +
                      gates,
                      "fired": [r["state"] for r in out]})
    return out


def eval_bar(algo: str, ticker: str, d: pd.DataFrame, h1: pd.DataFrame,
             m15: pd.DataFrame, trace: dict | None = None) -> list:
    """One routine on one bar's frames. Walk and --debug share this so a
    debug replay can never diverge from what the walk evaluated. The trace
    kwarg is only passed when set, keeping the walk-time call identical to
    the direct routine call."""
    kw = {} if trace is None else {"trace": trace}
    if RPS_PAIR is not None and algo == _rps_algo():
        return _eval_rps(ticker, d, h1, m15, trace)
    if "+" in algo:
        return _eval_combo(algo, ticker, d, h1, m15, trace)
    if algo in ROUTER:
        return globals()[ROUTER[algo]](d, **kw)
    return analyze(ticker, d, h1, m15, **kw)


def _eval_combo(algo: str, ticker: str, d: pd.DataFrame, h1: pd.DataFrame,
                m15: pd.DataFrame, trace: dict | None) -> list:
    """Agreement filter: fires the FIRST routine's signals only where the
    second routine fires the same bar in the same direction. Entry/SL come
    from the first routine; the pair shares one sig_key."""
    parts = algo.split("+", 1)
    if len(parts) != 2 or not all(parts):
        return []
    tra, trb = ({"checks": []}, {"checks": []}) \
        if trace is not None else (None, None)
    ra = eval_bar(parts[0], ticker, d, h1, m15, trace=tra)
    rb = eval_bar(parts[1], ticker, d, h1, m15, trace=trb)
    paired = []
    for a in ra:
        partners = [x for x in rb
                    if _sameside(a.get("state", ""), x.get("state", ""))]
        if not partners:
            continue
        b = partners[0]
        p = dict(a)
        ka = a.get("sig_key") or (parts[0], a.get("state"))
        kb = b.get("sig_key") or (parts[1], b.get("state"))
        p["algo"] = algo
        p["note"] = a.get("note", "") + f" & {parts[1]}-agree"
        p["sig_key"] = ("combo", parts[0], parts[1], ka, kb)
        paired.append(p)
    if trace is not None:
        trace.update({
            "algo": algo,
            "checks": [f"{parts[0]}: {c}" for c in tra.get("checks", [])] +
                      [f"{parts[1]}: {c}" for c in trb.get("checks", [])],
            "fired": [r["state"] for r in paired]})
    return paired


def parse_algos(s: str) -> set:
    """'1,3,R5' -> {'R1','R3','R5'}. Keywords: both (R1+R2, legacy
    default), rev (all R*), all (OLD + all R*), rps (v2 two-step),
    rps1 (legacy v1: bare pair agreement, routine entries),
    'Rn+Rm' agreement combos. Raises ValueError on bad input."""
    valid = ["OLD", "BOTH", "REV", "ALL", "RPS", "RPS1"] + sorted(
        REV_ALGOS, key=_rnum)
    out: set = set()

    def one(tok: str) -> str:
        t = tok.strip().upper()
        if t.isdigit():
            t = "R" + t
        if t not in REV_ALGOS and t != "OLD":
            raise ValueError(f"bad --algo {tok!r} (want one of "
                             f"{', '.join(valid)} or combos like R4+R2)")
        return t

    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        t = tok.upper()
        if t == "BOTH":
            out |= {"R1", "R2"}
        elif t == "REV":
            out |= set(REV_ALGOS)
        elif t == "ALL":
            out |= {"OLD"} | set(REV_ALGOS)
        elif t == "RPS":
            if RPS_PAIR is None:
                raise ValueError("--algo RPS pair not selected yet "
                                 "(tournament step pending)")
            out.add(_rps_algo())
        elif t == "RPS1":
            if RPS_PAIR is None:
                raise ValueError("--algo RPS1 pair not selected yet "
                                 "(tournament step pending)")
            out.add(_rps_pair_tag())
        elif "+" in t:
            parts = [one(p) for p in t.split("+")]
            if len(parts) != 2 or parts[0] == parts[1]:
                raise ValueError(f"bad --algo combo {tok!r} (want Rn+Rm, "
                                 f"two different routines)")
            out.add("+".join(parts))
        elif tok.upper() == "OLD":
            out.add("OLD")
        else:
            out.add(one(tok))
    if not out:
        raise ValueError("--algo selects nothing")
    return out


def walk(ticker: str, d_full: pd.DataFrame, h1_full: pd.DataFrame,
         m15_walk: pd.DataFrame, algos=None, m15_hist=None) -> tuple:
    """Run the selected routines bar by bar over m15_walk. Returns (hits,
    stats); each hit is a newly appeared signal (change-only: same pattern
    key on the next bar does not re-alert, a new pattern does). Indicator
    history comes from m15_hist (defaults to the walk frame); pass the full
    fetch there and the walk starts fully warmed (zero skipped bars)."""
    if algos is None:
        algos = {"OLD", "R1", "R2"}
    if m15_hist is None:
        m15_hist = m15_walk
    hits, active, cooled = [], set(), {}
    scanned = evaluated = skipped = errors = 0
    for pos, ts in enumerate(m15_walk.index):
        scanned += 1
        sl = slices_for(d_full, h1_full, m15_hist, ts)
        if sl is None:
            skipped += 1
            continue
        d, h1, m15 = sl
        if len(d) < MIN_DAILY or len(h1) < MIN_TF or len(m15) < MIN_TF:
            skipped += 1
            continue
        evaluated += 1
        bar_keys = set()
        for algo in _canon_order(algos):
            try:
                results = eval_bar(algo, ticker, d, h1, m15)
            except Exception:
                errors += 1
                continue
            for r in results:
                key = r.get("sig_key", (algo, r["state"]))
                try:
                    hash(key)
                except Exception:
                    key = (algo, r["state"])
                bar_keys.add(key)
                if key in active:
                    continue
                if pos - cooled.get(key, -10 ** 9) < COOLDOWN_BARS:
                    continue  # same pattern flickering back: not a new setup
                cooled[key] = pos
                grade, why = grade_of(r["state"], r.get("note", ""))
                hit = {"id": len(hits) + 1, "ts": ts,
                       "time": ts + pd.Timedelta(minutes=15),
                       "algo": r.get("algo", ""),
                       "grade": grade, "grade_why": why, **r}
                if RPS_PAIR is not None and \
                        r.get("algo", "") == _rps_algo():
                    # RPS fills the next 15m open: entries always print
                    # after the alert (zero lookahead, live-fillable).
                    coming = m15_hist.index[m15_hist.index > ts]
                    if len(coming):
                        hit["price"] = round(float(
                            m15_hist["Open"].loc[coming[0]]), 2)
                        hit["note"] = f'{hit.get("note", "")} ' \
                            "fill=next-open"
                    else:
                        hit["note"] = f'{hit.get("note", "")} ' \
                            "fill=signal-EOD"
                hits.append(hit)
        active = bar_keys
    stats = {"scanned": scanned, "evaluated": evaluated, "skipped": skipped,
             "errors": errors}
    return hits, stats


_SIDE = {"Long": "Buy", "Possible upcoming Rejection": "Short",
         "Possible upcoming Reversal": "Buy"}


def fmt(ticker: str, hit: dict) -> str:
    ts = hit["time"].tz_convert(PT).strftime("%Y-%m-%d %H:%MPT")
    side = _SIDE.get(hit["state"], "?")
    atag = f'[{hit["algo"]}]' if hit.get("algo") else ""
    return (f'#{hit["id"]} {ts} "[{hit.get("grade", "?")}]{atag} {ticker} - '
            f'{hit["state"]} - {side} @ {hit["price"]} - '
            f'SL {hit["stop"]} - target {hit["target"]}. ({hit["note"]})"')


FWD_BARS = 260  # ~10 sessions of 15m bars: daily-swing risks (2-6%)
                  # need 1-3 weeks to resolve; 5 sessions scored them all
                  # 'open' by construction


def score_hits(hits: list, m15_full: pd.DataFrame,
               fwd: int = FWD_BARS) -> list:
    """Forward first-touch check per hit (comparability proxy, NOT the live
    exit, which trails). Long wins if +1R tags before the SL, short
    mirrors; same-bar double touch counts as a loss (SL assumed first);
    'open' if neither touches in fwd bars; 'recent' when the window runs
    out before fwd bars elapse. Each result: {id, outcome, bars, mfe_R}."""
    out = []
    for h in hits:
        entry, sl = float(h["price"]), float(h["stop"])
        risk = abs(entry - sl)
        long = h["state"] in ("Long", "Possible upcoming Reversal")
        fwd_bars = m15_full[m15_full.index > h["ts"]].head(fwd)
        if risk <= 0:
            out.append({"id": h["id"], "outcome": "norisk", "bars": 0,
                        "mfe": None})
            continue
        if len(fwd_bars) < fwd:
            out.append({"id": h["id"], "outcome": "recent",
                        "bars": len(fwd_bars), "mfe": None})
            continue
        res, nb, mfe = "open", len(fwd_bars), 0.0
        for j, (_, b) in enumerate(fwd_bars.iterrows(), 1):
            hi, lo = float(b["High"]), float(b["Low"])
            if long:
                mfe = max(mfe, (hi - entry) / risk)
                if lo <= sl:
                    res, nb = "loss", j
                    break
                if hi >= entry + risk:
                    res, nb = "win", j
                    break
            else:
                mfe = max(mfe, (entry - lo) / risk)
                if hi >= sl:
                    res, nb = "loss", j
                    break
                if lo <= entry - risk:
                    res, nb = "win", j
                    break
        out.append({"id": h["id"], "outcome": res, "bars": nb,
                    "mfe": round(mfe, 2)})
    return out


def print_scores(hits: list, scored: list) -> None:
    print(f"FORWARD CHECK ({FWD_BARS} bars ~10 sessions; +1R first-touch "
          f"vs SL; proxy, not the live exit):", flush=True)
    by_id = {h["id"]: h for h in hits}
    for s in scored:
        h = by_id[s["id"]]
        atag = f'[{h["algo"]}]' if h.get("algo") else "[OLD]"
        mfe = "-" if s["mfe"] is None else f'{s["mfe"]:+.2f}R'
        print(f'  #{s["id"]}{atag} {s["outcome"]} '
              f'({s["bars"]} bars, mfe {mfe})', flush=True)
    agg: dict = {}
    for s in scored:
        if s["outcome"] in ("recent", "norisk"):
            continue
        a = by_id[s["id"]].get("algo") or "OLD"
        d = agg.setdefault(a, {"n": 0, "win": 0, "loss": 0, "mfe": []})
        d["n"] += 1
        if s["outcome"] == "win":
            d["win"] += 1
        elif s["outcome"] == "loss":
            d["loss"] += 1
        if s["mfe"] is not None:
            d["mfe"].append(s["mfe"])
    for a in sorted(agg):
        d = agg[a]
        decided = d["win"] + d["loss"]
        wr = f"{100.0 * d['win'] / decided:.0f}%" if decided else "-"
        avg = sum(d["mfe"]) / len(d["mfe"]) if d["mfe"] else 0.0
        print(f"  [{a}] scored={d['n']} win={d['win']} loss={d['loss']} "
              f"open={d['n'] - decided} win%={wr} avg_mfe={avg:+.2f}R",
              flush=True)


GRADE_ORDER = ["C", "B", "B+", "A", "A+"]


def grade_rank(g: str) -> int:
    """C < B < B+ < A < A+. 'B (thin)' counts as B; unknown ('?') ranks
    below everything so a --grade filter never passes it blindly."""
    base = (g or "").upper().strip().split(" ")[0]
    try:
        return GRADE_ORDER.index(base)
    except ValueError:
        return -1


def parse_grade(s: str) -> str:
    """Validate a --grade threshold. Returns the canonical grade."""
    g = (s or "").upper().strip()
    if g not in GRADE_ORDER:
        raise ValueError(f"bad --grade {s!r} (want one of "
                         f"{', '.join(GRADE_ORDER)})")
    return g


def filter_hits(hits: list, min_grade: str) -> tuple:
    """(shown, hidden_count): keep hits at/above min_grade, renumbered so
    #ids always match what is printed (debug ids refer to shown rows)."""
    if not min_grade:
        return hits, 0
    bar = grade_rank(min_grade)
    shown = [h for h in hits if grade_rank(h.get("grade", "?")) >= bar]
    for n, h in enumerate(shown, 1):
        h["id"] = n
    return shown, len(hits) - len(shown)


def parse_ids(s: str) -> list:
    """'1,3-4' -> [1, 3, 4]. Raises ValueError on bad input."""
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    if not out or any(i < 1 for i in out):
        raise ValueError(f"bad --debug ids: {s!r}")
    return sorted(set(out))


def print_trace(ticker: str, hit: dict, tr: dict) -> None:
    """Human-readable dump of one setup's gate decisions."""
    print(f'--- DEBUG #{hit["id"]} ' + fmt(ticker, hit), flush=True)
    print(f'  grade={hit.get("grade", "?")} ({hit.get("grade_why", "")})',
          flush=True)
    dl = tr.get("daily_last", {})
    print(f'  forming daily O/H/L/C/V={dl} prev_close={tr.get("daily_prev_close")}',
          flush=True)
    print(f'  15m bar O/H/L/C/V={tr.get("m15_last")}', flush=True)
    if "votes" not in tr:
        print("  (standalone R* routine: votes/RSI/ADX/supports N/A - "
              "daily frame + checks above are its full inputs)", flush=True)
    else:
        for tf in ("D", "1h", "15m"):
            vi = tr.get("vote_inputs", {}).get(tf, {})
            print(f'  vote {tf}={tr.get("votes", {}).get(tf)} inputs={vi}',
                  flush=True)
        print(f'  rsi={tr.get("rsi")} adx={tr.get("adx")} '
              f'rvol={tr.get("rvol")} hi20={tr.get("hi20")} '
              f'lo20={tr.get("lo20")} pats={tr.get("pats_last3")}',
              flush=True)
        print(f'  supports={tr.get("supports")} '
              f'resistances={tr.get("resistances")}', flush=True)
    for c in tr.get("checks", []):
        print(f'  {c}', flush=True)
    print(f'  fired={tr.get("fired")}', flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="")
    ap.add_argument("--days", default="")
    ap.add_argument("--debug", default="",
                    help="setup ids to trace, e.g. '1' or '1,3-4'")
    ap.add_argument("--grade", default="",
                    help="minimum grade shown, e.g. B (=B,B+,A,A+)")
    ap.add_argument("--algo", default="both",
                    help="1..10, OLD, combos Rn+Rm, extras (both/rev/all/rps/rps1)")
    ap.add_argument("--score", action=argparse.BooleanOptionalAction,
                    default=True, help="forward 1R-vs-SL check per setup")
    a = ap.parse_args()
    sel = (a.algo or "both").strip()
    try:
        algos = parse_algos(sel)
        debug_ids = parse_ids(a.debug) if a.debug.strip() else []
        min_grade = parse_grade(a.grade) if a.grade.strip() else ""
    except ValueError as e:
        print(e, flush=True)
        return 1
    ticker = (a.ticker or input("Ticker: ")).strip().upper()
    days_raw = (str(a.days) or input("Days of 15m/1h history (1-60): ")) \
        .strip()
    if not ticker:
        print("no ticker given", flush=True)
        return 1
    try:
        days = int(days_raw or 20)
    except ValueError:
        print(f"bad days: {days_raw!r}", flush=True)
        return 1
    if not 1 <= days <= 60:
        print(f"days must be 1-60 (15m fetch limit), got {days}", flush=True)
        return 1
    try:
        d_full, h1_full, m15_full = fetch(ticker, days)
    except Exception as e:
        print(f"fetch failed for {ticker}: {e}", flush=True)
        return 1
    if len(m15_full) == 0 or len(d_full) == 0:
        print(f"no data for {ticker} (bad ticker or empty window)",
              flush=True)
        return 1
    m15_walk = window_bars(m15_full, days)
    span0 = m15_walk.index[0].tz_convert(PT).strftime("%Y-%m-%d")
    span1 = m15_walk.index[-1].tz_convert(PT).strftime("%Y-%m-%d")
    print(f"BACKTEST {ticker} | 15m window {span0}..{span1} "
          f"({len(m15_walk)} bars) | daily tail-200 | "
          f"1h {days}+{H1_WARMUP_DAYS}d warmup | algos {sel} | "
          f"change-only hits", flush=True)
    hits, stats = walk(ticker, d_full, h1_full, m15_walk,
                       algos=algos, m15_hist=m15_full)
    hits, hidden = filter_hits(hits, min_grade)
    filt = f" | grade filter {min_grade}+ ({hidden} below-grade hidden)" \
        if min_grade else ""
    for h in hits:
        print(fmt(ticker, h), flush=True)
    print(f"done: {len(hits)} trade setups | {stats['evaluated']} bars "
          f"evaluated, {stats['skipped']} warmup-skipped, "
          f"{stats['errors']} errors{filt}", flush=True)
    if a.score and hits:
        print_scores(hits, score_hits(hits, m15_full))
    if debug_ids:
        by_id = {h["id"]: h for h in hits}
        unknown = [i for i in debug_ids if i not in by_id]
        if unknown:
            print(f"--debug: unknown setup ids {unknown} "
                  f"(have 1..{len(hits)})", flush=True)
            return 1
        for i in debug_ids:
            h = by_id[i]
            sl = slices_for(d_full, h1_full, m15_full, h["ts"])
            if sl is None:
                print(f"--debug #{i}: bar no longer sliceable", flush=True)
                return 1
            tr: dict = {}
            re_fired = [r["state"] for r in eval_bar(
                h.get("algo") or "OLD", ticker, *sl, trace=tr)]
            dd, _, mm = sl  # frames the routine actually read
            tr["daily_last"] = {
                k: round(float(dd[k].iloc[-1]), 2)
                for k in ("Open", "High", "Low", "Close", "Volume")}
            tr["m15_last"] = {
                k: round(float(mm[k].iloc[-1]), 2)
                for k in ("Open", "High", "Low", "Close", "Volume")}
            print_trace(ticker, h, tr)
            if h["state"] not in re_fired:
                print(f"  WARNING: replay fired {re_fired}, hit was "
                      f'{h["state"]} (non-deterministic?)', flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
