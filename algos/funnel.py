"""Multi-stage swing funnel: pool -> watch -> setup -> fill -> manage.

Swing trades are found while they form, not in one pass: a name shows
interest, goes on a watch pool, waits for a defined setup, fills on the
trigger, and is then managed. Every stage below reads COMPLETED daily
bars; the premarket slot runs the scan, the 15-min loop fills and manages.

  0. Pool    top FUNNEL_N names of the scan union by 20-day dollar volume,
             price >= FUNNEL_MIN_PX: options-liquid names only.
  1. Watch   structural flags that say "something is coiling": volatility
             squeeze (Bollinger inside Keltner), tight base near the 20-day
             high/low, pullback zone at a trending EMA21, undercut of the
             20-day low (mirror: overshoot of the high). RS extremes and
             volume dry-up are modifiers. Any structural flag puts the
             name on data/funnel_watch.json with a bias (long/short/either).
  2. Setup   four families, each a completed-bar signal with a level, a
             risk and an exit spec, filled at the NEXT open within 1 ATR of
             the level (the backtest convention - no chasing):
               PB  trend pullback   algo.json secondary, mask verbatim
               BO  20-day breakout  algo.json primary, mask verbatim
               SQ  squeeze release  (new) >= SQ_MIN_DAYS inside the bands,
                   first close outside with momentum and EMA21 agreeing
               RC  undercut & reclaim (new) first close back above a
                   20-day low undercut within RC_WINDOW bars (mirror:
                   overshoot & fail at the 20-day high)
  3. Fill    the premarket text lists the day's setups; the first intraday
             run books the open as the paper fill or invalidates it.
  4. Manage  the tracker: stop / target / chandelier trail / time stop,
             one batched FNL text per run; Saturday grades each family.
Options mapping per family and horizon: options_plan().

Usage:
    python algos/funnel.py --ticker NVDA              # flags + setups
    python algos/funnel.py --backtest                 # all families, pool
    python algos/funnel.py --backtest --family SQ --family RC
    python algos/funnel.py --scan --channels dry      # premarket path, dry
    python algos/funnel.py --watch                    # print the watch file
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backtest"))
from combos import COST_BPS, add_features, atr, ema  # noqa: E402
from combos import level_of, risk_of, signal_mask  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNNEL_WATCH = os.path.join(ROOT_DIR, "data", "funnel_watch.json")
SIGNAL_LOG = os.path.join(ROOT_DIR, "data", "signal_log.csv")

FUNNEL_N, FUNNEL_MIN_PX = 200, 15.0
MIN_BARS = 260            # features need sma200 + a year of signals
BAND_N, BB_MULT, KC_MULT = 20, 2.0, 1.5
SQ_MIN_DAYS = 6           # bars inside the bands before a release counts
SQ_WATCH_DAYS = 5         # bars inside the bands to make the watch pool
BASE_MAX_RNG = 0.08       # 20-day range / close for a tight base ...
BASE_FLAT = 0.015         # ... whose 20-day mean moved < this in 10 bars
BASE_NEAR = 0.97          # close within 3% of the 20-day high (low mirror)
WATCH_MIN = 1.5           # score to make the watch pool
DRYUP_MAX = 0.70          # 5-day / 20-day volume
RS_N, RS_EDGE = 63, 0.10  # 3-month return vs pool median, +-10% = extreme
ADX_MIN, RVOL_MIN = 20.0, 2.0   # algo.json gates for PB / BO
CHASE_ATR = 1.0           # next-open fill only within this many ATRs
RC_WINDOW = 3             # bars to reclaim an undercut level
FAMILIES = ("PB", "BO", "SQ", "RC")
FAMILY_NAME = {"PB": "trend pullback", "BO": "20d breakout",
               "SQ": "squeeze release", "RC": "undercut reclaim"}
# exit spec per family: ('trail', chandelier ATR mult, time stop bars) or
# ('fixed', target R, time stop bars). PB/BO are algo.json's; SQ/RC are
# the a-priori specs the 2026-10-10 backtest measured.
EXITS = {"PB": ("trail", 2.5, 40), "BO": ("trail", 3.0, 40),
         "SQ": ("fixed", 2.0, 20), "RC": ("fixed", 2.0, 15)}
# grades per (family, side) from the 2026-10-10 backtest (top-200 pool,
# 2025-07..2026-10, TRAIN/TEST on the median trade date, 2.5 bps/side):
#   SQ long  n=165 win 49% +0.19R PF 1.41 (TRAIN +0.18 / TEST +0.22) -> B+
#   SQ short n=159 win 43% +0.00R                                  -> B
#   PB long  n=49  win 20% +0.65R PF 1.78 (TEST +1.68, runners)     -> B+
#   BO long  n=24  win 21% -0.37R in this window (algo.json: A on 5y
#            S&P; kept tradable at B+, Saturday arbitrates)         -> B+
#   RC long  n=2002 win 39% +0.09R, no slice better, 30+/week       -> C
#   shorts PB/BO/RC negative or starved                            -> C
# Rubric: B+ and up are texted; B books paper rows silently; C never
# books (the pattern still feeds the watch pool as a flag).
GRADES = {("PB", "long"): "B+", ("PB", "short"): "C",
          ("BO", "long"): "B+", ("BO", "short"): "C",
          ("SQ", "long"): "B+", ("SQ", "short"): "B",
          ("RC", "long"): "C", ("RC", "short"): "C"}
PAUSED = {k for k, g in GRADES.items() if g.startswith("C")}
TEXT_GRADES = ("A+", "A", "B+")
SQ_VOL_MARK = 1.2         # release-bar RVOL that marked the better half
LOG_COLS = ["date", "ticker", "strategy", "side", "entry", "stop", "target",
            "grade", "timeframe", "status", "outcome_R", "exit_reason",
            "regime", "news_flag", "notes"]


# ---------- stage 0: pool ----------
def funnel_pool(n: int = FUNNEL_N, min_px: float = FUNNEL_MIN_PX,
                tickers: list = None) -> list:
    """Top-n cached names by 20-day mean dollar volume at or above
    min_px. Local only (no network): the daily cache is the source."""
    from daily_cache import load  # noqa: E402
    if tickers is None:
        from screener import scan_pool  # noqa: E402
        tickers = scan_pool()
    rows = []
    for t in tickers:
        d = load(t, "1d")
        if len(d) < 60:
            continue
        px = float(d["Close"].iloc[-1])
        if px < min_px:
            continue
        dv = float((d["Close"] * d["Volume"]).tail(20).mean())
        rows.append((dv, t))
    rows.sort(reverse=True)
    return [t for _, t in rows[:n]]


# ---------- features ----------
def features(df: pd.DataFrame) -> pd.DataFrame:
    """combos.add_features (ema9/21/50, sma200, rsi, atr, rvol, shifted
    hi20/lo20, adx, bull) plus the funnel's own columns. Causal: every
    column at bar i reads bars <= i only."""
    d = add_features(df)
    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    sma20, sd = c.rolling(BAND_N).mean(), c.rolling(BAND_N).std()
    d["bb_up"], d["bb_dn"] = sma20 + BB_MULT * sd, sma20 - BB_MULT * sd
    e20, a20 = ema(c, BAND_N), atr(h, l, c, BAND_N)
    d["kc_up"], d["kc_dn"] = e20 + KC_MULT * a20, e20 - KC_MULT * a20
    d["sq_on"] = (d["bb_up"] < d["kc_up"]) & (d["bb_dn"] > d["kc_dn"])
    sq = d["sq_on"].astype(int)
    d["sq_days"] = sq.groupby((sq != sq.shift()).cumsum()).cumsum() * sq
    hh20, ll20 = h.rolling(20).max(), l.rolling(20).min()  # inclusive
    d["mom"] = c - ((hh20 + ll20) / 2 + sma20) / 2
    d["rng20"] = (hh20 - ll20) / c
    d["flat20"] = (sma20 / sma20.shift(10) - 1).abs() <= BASE_FLAT
    d["near_hi"], d["near_lo"] = c / hh20, c / ll20
    d["vol5_20"] = v.rolling(5).mean() / v.rolling(20).mean()
    d["ret63"] = c / c.shift(RS_N) - 1
    d["e21_up"] = d["ema21"] > d["ema21"].shift(5)
    d["e21_dn"] = d["ema21"] < d["ema21"].shift(5)
    # undercut & reclaim: the undercut level is the prior 20-day low that
    # printed under; it stays live for RC_WINDOW bars; the reclaim is the
    # first up-close back above it (same-day spring included).
    uc = l < d["lo20"]
    lvl = d["lo20"].where(uc).ffill(limit=RC_WINDOW)
    d["rc_lvl"] = lvl
    d["rc_long"] = (lvl.notna() & (c > lvl) & (c > o)
                    & ((c.shift(1) < lvl) | uc))
    oc = h > d["hi20"]
    lvl_s = d["hi20"].where(oc).ffill(limit=RC_WINDOW)
    d["rc_lvl_s"] = lvl_s
    d["rc_short"] = (lvl_s.notna() & (c < lvl_s) & (c < o)
                     & ((c.shift(1) > lvl_s) | oc))
    d["uc_lo"] = l.rolling(RC_WINDOW + 1).min()
    d["oc_hi"] = h.rolling(RC_WINDOW + 1).max()
    # squeeze release: first bar outside the bands after >= SQ_MIN_DAYS
    # inside, momentum and EMA21 on the same side, close in that direction
    rel = (~d["sq_on"]) & (d["sq_days"].shift(1) >= SQ_MIN_DAYS)
    d["sq_long"] = rel & (d["mom"] > 0) & (c > d["ema21"]) & (c > o)
    d["sq_short"] = rel & (d["mom"] < 0) & (c < d["ema21"]) & (c < o)
    d["sq_lo"] = l.rolling(SQ_MIN_DAYS + 1).min()
    d["sq_hi"] = h.rolling(SQ_MIN_DAYS + 1).max()
    return d


# ---------- stage 2: setups ----------
def family_signals(d: pd.DataFrame, family: str, side: int) -> np.ndarray:
    """Boolean mask of completed-bar signals for one family and side
    (+1 long, -1 short). PB/BO are algo.json's masks verbatim."""
    if family == "PB":
        return signal_mask(d, "pullback", side, ADX_MIN)
    if family == "BO":
        return signal_mask(d, "breakout", side, ADX_MIN, RVOL_MIN)
    valid = d[["atr", "ema21", "sma200"]].notna().all(axis=1).to_numpy()
    col = {("SQ", 1): "sq_long", ("SQ", -1): "sq_short",
           ("RC", 1): "rc_long", ("RC", -1): "rc_short"}[(family, side)]
    return d[col].fillna(False).to_numpy() & valid


def level_for(d: pd.DataFrame, family: str, side: int, i: int) -> float:
    """Reference level the next-open fill must sit within CHASE_ATR of."""
    if family == "BO":
        return level_of(d, "breakout", side, i)
    return float(d["Close"].iloc[i])


def risk_for(d: pd.DataFrame, family: str, side: int, i: int) -> float:
    """Risk unit from the signal bar: PB/BO per algo.json (risk_of); SQ =
    distance to the far side of the squeeze range; RC = distance to the
    undercut extreme. Clamped to 0.2..3 ATR like risk_of."""
    if family in ("PB", "BO"):
        return risk_of(d, "pullback" if family == "PB" else "breakout",
                       side, i)
    c, a = float(d["Close"].iloc[i]), float(d["atr"].iloc[i])
    if family == "SQ":
        base = (c - float(d["sq_lo"].iloc[i]) if side == 1
                else float(d["sq_hi"].iloc[i]) - c)
    else:
        base = (c - float(d["uc_lo"].iloc[i]) if side == 1
                else float(d["oc_hi"].iloc[i]) - c)
    return float(min(max(base, 0.2 * a), 3 * a))


def setup_at(d: pd.DataFrame, i: int, family: str, side: int) -> dict:
    """One pending setup off signal bar i: level, risk, exit spec, grade.
    Stop/target are expressed from the (unknown) fill: stop = fill -
    risk*side; fixed target = fill + R*risk*side."""
    sd = "long" if side == 1 else "short"
    spec = EXITS[family]
    rv = float(d["rvol"].iloc[i]) if pd.notna(d["rvol"].iloc[i]) else None
    return {"family": family, "side": sd, "level": round(
        level_for(d, family, side, i), 2),
        "risk": round(risk_for(d, family, side, i), 2),
        "atr": round(float(d["atr"].iloc[i]), 2), "exit": spec[0],
        "exit_mult": spec[1], "time": spec[2],
        "grade": GRADES[(family, sd)],
        "paused": (family, sd) in PAUSED,
        "bull": bool(d["bull"].iloc[i]),
        "rvol": round(rv, 2) if rv is not None else None,
        "sq_days": (int(d["sq_days"].shift(1).fillna(0).iloc[i])
                    if family == "SQ" else None),
        "asof": d.index[i].date().isoformat()}


# ---------- backtest (port of combos.simulate with the funnel's risk) ----
def simulate(d: pd.DataFrame, sig_idx: np.ndarray, family: str, side: int,
             exit_spec: tuple = None) -> list:
    """Next-open fills within CHASE_ATR of the level, 2.5 bps per side,
    fixed target or chandelier trail, time stop; no overlapping trades per
    ticker/family/side. Returns dicts with the signal bar and the R."""
    exit_spec = exit_spec or EXITS[family]
    out, n = [], len(d)
    op, hi, lo, cl = (d[k].to_numpy() for k in ("Open", "High", "Low",
                                                   "Close"))
    atrv = d["atr"].to_numpy()
    mode, tstop_n = exit_spec[0], int(exit_spec[2])
    sigs = sig_idx[(sig_idx >= 0) & (sig_idx < n - 2)]
    i_ptr = 0
    while i_ptr < len(sigs):
        i = int(sigs[i_ptr])
        entry, risk = float(op[i + 1]), risk_for(d, family, side, i)
        if not (np.isfinite(entry) and np.isfinite(risk)) or risk <= 0:
            i_ptr += 1
            continue
        if abs(entry - level_for(d, family, side, i)) > CHASE_ATR * float(
                atrv[i]):
            i_ptr += 1
            continue
        cost_r = 2 * (COST_BPS / 1e4) * entry / risk
        mae = mfe = 0.0
        done, exit_i, exit_r = False, i + 1, 0.0
        if mode == "fixed":
            tgt_r = float(exit_spec[1])
            stop = entry - risk * side
            tgt = entry + tgt_r * risk * side
            for j in range(i + 1, min(i + 1 + tstop_n, n)):
                mfe = max(mfe, (hi[j] - entry) / risk * side)
                mae = min(mae, (lo[j] - entry) / risk * side)
                hit_s = (lo[j] <= stop) if side == 1 else (hi[j] >= stop)
                hit_t = (hi[j] >= tgt) if side == 1 else (lo[j] <= tgt)
                if hit_s:
                    exit_r, exit_i, done = -1.0, j, True
                    break
                if hit_t:
                    exit_r, exit_i, done = tgt_r, j, True
                    break
            if not done:
                j = min(i + tstop_n, n - 1)
                exit_r, exit_i = side * (cl[j] - entry) / risk, j
        else:
            mult = float(exit_spec[1])
            extreme, tstop = entry, entry - risk * side
            for j in range(i + 1, min(i + 1 + tstop_n, n)):
                mfe = max(mfe, (hi[j] - entry) / risk * side)
                mae = min(mae, (lo[j] - entry) / risk * side)
                extreme = max(extreme, hi[j]) if side == 1 else min(
                    extreme, lo[j])
                tstop = (max(tstop, extreme - mult * atrv[j]) if side == 1
                         else min(tstop, extreme + mult * atrv[j]))
                touched = (lo[j] <= tstop) if side == 1 else (hi[j] >= tstop)
                if touched:
                    fill = min(op[j], tstop) if side == 1 else max(
                        op[j], tstop)
                    exit_r, exit_i, done = side * (fill - entry) / risk, j, True
                    break
            if not done:
                j = min(i + tstop_n, n - 1)
                exit_r, exit_i = side * (cl[j] - entry) / risk, j
        out.append({"sig": i, "entry_bar": i + 1, "exit_bar": exit_i,
                    "R": exit_r - cost_r, "mae_R": mae, "mfe_R": mfe,
                    "hold": exit_i - i - 1})
        i_ptr = int(np.searchsorted(sigs, exit_i + 1))
    return out


def backtest(pool: list, families=FAMILIES, min_bars: int = MIN_BARS,
             exits: dict = None) -> pd.DataFrame:
    """Every family x side over the pool, one row per trade with the
    signal-bar context (regime, adx, rvol, squeeze length)."""
    from daily_cache import load  # noqa: E402
    exits = exits or EXITS
    rows = []
    for t in pool:
        df = load(t, "1d")
        if len(df) < min_bars:
            continue
        d = features(df)
        for fam in families:
            for side in (1, -1):
                idx = np.flatnonzero(family_signals(d, fam, side))
                if not len(idx):
                    continue
                for tr in simulate(d, idx, fam, side, exits[fam]):
                    i = tr["sig"]
                    rows.append({"ticker": t, "family": fam,
                                 "side": "long" if side == 1 else "short",
                                 "date": d.index[i].date(),
                                 "bull": bool(d["bull"].iloc[i]),
                                 "adx": float(d["adx"].iloc[i]),
                                 "rvol": float(d["rvol"].iloc[i]),
                                 "sq_days": int(d["sq_days"].shift(1).fillna(
                                     0).iloc[i]),
                                 "rsi": float(d["rsi"].iloc[i]),
                                 **{k: tr[k] for k in ("R", "mae_R", "mfe_R",
                                                       "hold")}})
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame, split=None) -> str:
    """Per family/side table, TRAIN/TEST on a date split (median trade
    date unless given), plus the bull/bear regime cut."""
    if trades.empty:
        return "no trades"
    tr = trades.copy()
    tr["date"] = pd.to_datetime(tr["date"])
    split = pd.Timestamp(split) if split else tr["date"].median()
    tr["set"] = np.where(tr["date"] <= split, "TRAIN", "TEST")

    def st(g):
        n = len(g)
        if n == 0:
            return "n=0"
        w = g.R > 0
        gross_w = g.R[w].sum()
        gross_l = -g.R[~w].sum()
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        return (f"n={n:4d} win={100 * w.mean():4.1f}% expR={g.R.mean():+.2f} "
                f"PF={min(pf, 99):4.2f} hold={g.hold.median():3.0f}b "
                f"mfe={g.mfe_R.mean():+.2f} mae={g.mae_R.mean():+.2f}")
    lines = [f"split {split.date()}: TRAIN {int((tr.set == 'TRAIN').sum())} "
             f"/ TEST {int((tr.set == 'TEST').sum())} trades, "
             f"{tr.ticker.nunique()} tickers, {tr.date.min().date()}.."
             f"{tr.date.max().date()}"]
    for (fam, side), g in tr.groupby(["family", "side"]):
        lines.append(f"{fam} {side:5s} ALL   {st(g)}")
        for s in ("TRAIN", "TEST"):
            lines.append(f"{'':9s}{s:5s} {st(g[g.set == s])}")
        for reg, gg in (("bull ", g[g.bull]), ("bear ", g[~g.bull])):
            lines.append(f"{'':9s}{reg} {st(gg)}")
    return "\n".join(lines)


# ---------- stage 1: watch flags ----------
def watch_flags(d: pd.DataFrame, i: int, rs_median: float = 0.0) -> dict:
    """Interest flags at bar i. Structural flags score 1: squeeze (>=
    SQ_WATCH_DAYS inside the bands), base (tight AND flat 20 days, near
    its high or low), pullback zone (trend + EMA21 touch + RSI band),
    spring / failed-break (an undercut or overshoot of the 20-day range
    that already reversed, i.e. the RC pattern in the last two bars).
    RS extreme and volume dry-up score 0.5 as modifiers. A plain trend
    or a plain new high scores nothing: those aren't coils."""
    r = d.iloc[i]
    flags, score = [], 0.0
    g = lambda k: float(r[k]) if pd.notna(r[k]) else float("nan")  # noqa: E731
    up = bool(r["ema21"] > r["ema50"]) and bool(r["bull"])
    dn = bool(r["ema21"] < r["ema50"]) and not bool(r["bull"])
    if g("sq_days") >= SQ_WATCH_DAYS:
        flags.append(f"squeeze{int(g('sq_days'))}")
        score += 1
    tight = g("rng20") <= BASE_MAX_RNG and bool(r["flat20"])
    if tight and g("near_hi") >= BASE_NEAR:
        flags.append("base-hi")
        score += 1
    elif tight and g("near_lo") <= 2 - BASE_NEAR:
        flags.append("base-lo")
        score += 1
    if (up and g("Low") <= g("ema21") + g("atr") and 38 <= g("rsi") <= 55):
        flags.append("pullback-up")
        score += 1
    elif (dn and g("High") >= g("ema21") - g("atr")
          and 45 <= g("rsi") <= 62):
        flags.append("pullback-dn")
        score += 1
    recent = slice(max(0, i - 1), i + 1)
    if bool(d["rc_long"].iloc[recent].fillna(False).any()):
        flags.append("spring")
        score += 1
    if bool(d["rc_short"].iloc[recent].fillna(False).any()):
        flags.append("failed-break")
        score += 1
    rs = g("ret63") - rs_median
    if rs == rs and rs >= RS_EDGE:
        flags.append("RS+")
        score += 0.5
    elif rs == rs and rs <= -RS_EDGE:
        flags.append("RS-")
        score += 0.5
    if g("vol5_20") < DRYUP_MAX:
        flags.append("dry")
        score += 0.5
    bias = "long" if up else ("short" if dn else "either")
    return {"flags": flags, "score": score, "bias": bias,
            "px": round(g("Close"), 2), "adx": round(g("adx"), 1),
            "rs": round(rs, 3) if rs == rs else None}


# ---------- options mapping ----------
def options_plan(family: str, side: str, grade: str) -> str:
    """Deterministic structure by family horizon and grade. Debit
    verticals cap the loss and survive IV crush; long-dated calls only
    for the trailing families in leaders; nothing here is 0-1 DTE."""
    kind = "call" if side == "long" else "put"
    if family == "RC":
        return f"{kind} debit spread 14-21 DTE (short strike at target)"
    if family == "SQ":
        return f"{kind} debit spread 30-45 DTE (short strike at target)"
    if family == "PB":
        return (f"{kind} debit spread 30-45 DTE"
                + ("; A-grade leaders: long call 60-90 DTE 0.65d"
                   if grade.startswith("A") else ""))
    return f"long {kind} 60-90 DTE 0.65d (trail per algo), or debit spread"


# ---------- stage 3: fills (pure) ----------
def fill_decisions(pending: list, prints: dict, today) -> tuple:
    """Book the day's fills off the open print. prints[t] = (last,
    day_open, volume). A fill happens at the open when it sits within
    CHASE_ATR of the level and not beyond the would-be stop; otherwise
    the setup is invalidated with a reason. Paused families and setups
    not due today are left alone. Returns (fills, invalid)."""
    fills, invalid = [], []
    for p in pending:
        if p.get("done") or p.get("paused"):
            continue
        if p.get("fill_date") != today.isoformat():
            continue
        q = prints.get(p["ticker"])
        if q is None:
            continue
        open_px = float(q[1])
        sgn = 1 if p["side"] == "long" else -1
        # the setup's own stop sits risk below the level: an open already
        # through it means the pattern failed overnight, no trade
        if sgn * (open_px - (float(p["level"]) - sgn * float(p["risk"]))) <= 0:
            invalid.append((p, f"open {open_px:.2f} through the setup stop"))
            continue
        if abs(open_px - float(p["level"])) > CHASE_ATR * float(p["atr"]):
            invalid.append((p, f"open {open_px:.2f} beyond 1 ATR of "
                               f"level {p['level']}"))
            continue
        stop = open_px - sgn * float(p["risk"])  # fill-relative, as backtested
        fills.append({**p, "entry": round(open_px, 2),
                      "stop": round(stop, 2),
                      "target": (round(open_px + sgn * float(p["exit_mult"])
                                       * float(p["risk"]), 2)
                                 if p["exit"] == "fixed" else "-")})
    return fills, invalid


def fnl_row(f: dict, today) -> dict:
    """signal_log paper row for a fill. notes carry the plan the tracker
    needs: exit mode, trail mult / target R, time stop, level."""
    plan = (f"trail={f['exit_mult']}" if f["exit"] == "trail"
            else f"tgt={f['exit_mult']}R")
    return {"date": today.isoformat(), "ticker": f["ticker"],
            "strategy": f"FNL-{f['family']}", "side": f["side"],
            "entry": f["entry"], "stop": f["stop"], "target": f["target"],
            "grade": f["grade"], "timeframe": "daily", "status": "ENTERED",
            "outcome_R": "", "exit_reason": "", "regime": (
                "bull" if f.get("bull") else "bear"), "news_flag": "",
            "notes": f"fnl {f['family']} lvl={f['level']} risk={f['risk']} "
                     f"{plan} time={f['time']} "
                     f"opt: {options_plan(f['family'], f['side'], f['grade'])}"}


def fnl_plan(notes) -> dict:
    """Parse a FNL row's notes back into its plan (trail mult, time stop)."""
    out = {"trail": None, "time": None}
    for tok in str(notes).split():
        if tok.startswith("trail="):
            try:
                out["trail"] = float(tok[6:])
            except ValueError:
                pass
        elif tok.startswith("time="):
            try:
                out["time"] = int(tok[5:])
            except ValueError:
                pass
    return out


def trail_stop(side: str, stop: float, extreme: float, atr_now: float,
               mult: float) -> float:
    """Chandelier: the stop only ever tightens toward the running extreme
    (highest high since entry for longs, lowest low for shorts)."""
    if side == "long":
        return max(float(stop), float(extreme) - mult * float(atr_now))
    return min(float(stop), float(extreme) + mult * float(atr_now))


# ---------- texts ----------
def texted(p: dict) -> bool:
    """Text policy: B+ and up, never a paused family."""
    return (not p.get("paused")) and p.get("grade") in TEXT_GRADES


def fmt_setup(p: dict) -> str:
    """[grade] - TICKER SIDE - family - lvl - risk [- sq N +vol] - opt:
    ... (paper-off when the family is paused). SQ lines carry the
    squeeze length and a +vol mark on a >= SQ_VOL_MARK release bar — the
    two slices that tested best."""
    extra = ""
    if p["family"] == "SQ" and p.get("sq_days") is not None:
        extra = f" - sq {p['sq_days']}d"
        if p.get("rvol") is not None and p["rvol"] >= SQ_VOL_MARK:
            extra += " +vol"
    tag = " (paused, paper off)" if p.get("paused") else ""
    return (f"[{p['grade']}] - {p['ticker']} {p['side'].upper()} - "
            f"{FAMILY_NAME[p['family']]} - lvl {p['level']} - risk "
            f"{p['risk']}{extra} - opt: "
            f"{options_plan(p['family'], p['side'], p['grade'])}{tag}")


def setups_text(pending: list, fill_date) -> str:
    """The premarket text: texted setups only (B+ and up), one per line."""
    lines = [f"FNL setups for {fill_date} (fill at open, <=1 ATR of lvl):"]
    for p in pending:
        if texted(p):
            lines.append(fmt_setup(p))
    return "\n".join(lines)


def watch_text(watch: dict, pool_n: int, per_flag: int = 10) -> str:
    """One compact line per structural flag, names sorted by score."""
    order = sorted(watch.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    groups = {"squeeze": [], "base": [], "pullback": [], "spring": [],
              "failed": []}
    for t, w in order:
        for f in w["flags"]:
            key = f.split("-")[0].rstrip("0123456789")
            if key in groups and t not in groups[key]:
                groups[key].append(t)
    lines = [f"FNL watch {len(watch)}/{pool_n}:"]
    for k, names in groups.items():
        if names:
            more = f" +{len(names) - per_flag}" if len(names) > per_flag \
                else ""
            lines.append(f"{k}: {' '.join(names[:per_flag])}{more}")
    return "\n".join(lines)


def fills_text(fills: list, invalid: list) -> str:
    lines = ["FNL fills:"]
    for f in fills:
        lines.append(f"[{f['grade']}] {f['ticker']} {f['side'].upper()} "
                     f"{f['family']} @ {f['entry']} SL {f['stop']} "
                     f"Tgt {f['target']} - paper")
    for p, why in invalid:
        lines.append(f"{p['ticker']} {p['family']} invalid: {why}")
    return "\n".join(lines)


# ---------- state ----------
def load_watch() -> dict:
    try:
        with open(FUNNEL_WATCH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_watch(state: dict):
    try:
        with open(FUNNEL_WATCH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def log_rows(rows: list, path: str = None) -> int:
    """Append fills to signal_log.csv; a ticker+strategy already open is
    skipped (one position per family per name)."""
    path = path or SIGNAL_LOG
    have = set()
    if os.path.exists(path):
        try:
            old = pd.read_csv(path)
            if len(old):
                for _, r in old[old["outcome_R"].isna()].iterrows():
                    have.add((str(r["ticker"]), str(r["strategy"])))
        except Exception:
            pass
    fresh = [r for r in rows if (r["ticker"], r["strategy"]) not in have]
    if not fresh:
        return 0
    pd.DataFrame(fresh, columns=LOG_COLS).to_csv(
        path, mode="a", header=not os.path.exists(path), index=False)
    return len(fresh)


# ---------- stage 1+2 scan (premarket) ----------
def scan(today, pool_n: int = FUNNEL_N, tickers: list = None,
         refresh: bool = True) -> dict:
    """Completed bars through yesterday -> watch pool + today's pending
    setups. `refresh` tops up the daily cache first (once a day, like the
    intraday loop; the premarket slot runs before that loop, so it must
    do its own). Writes nothing else: the caller sends/saves."""
    from daily_cache import load, sync_pool  # noqa: E402
    from market_calendar import add_trading_days  # noqa: E402
    if refresh:
        from screener import scan_pool  # noqa: E402
        try:
            sync_pool(tickers or scan_pool(), today=today)
        except Exception:
            pass  # stale cache beats no scan; the log shows the asof date
    pool = funnel_pool(pool_n, tickers=tickers)
    frames = {}
    for t in pool:
        df = load(t, "1d")
        if len(df) >= MIN_BARS:
            frames[t] = features(df)
    rs_med = float(np.nanmedian([f["ret63"].iloc[-1] for f in frames.values()])) \
        if frames else 0.0
    old = load_watch().get("watch", {})
    watch, pending = {}, []
    asof = None
    for t, d in frames.items():
        i = len(d) - 1
        asof = max(asof or d.index[i].date(), d.index[i].date())
        w = watch_flags(d, i, rs_med)
        if w["score"] >= WATCH_MIN:
            w["since"] = old.get(t, {}).get("since", today.isoformat())
            w["asof"] = d.index[i].date().isoformat()
            watch[t] = w
        for fam in FAMILIES:
            for side in (1, -1):
                if bool(family_signals(d, fam, side)[i]):
                    p = setup_at(d, i, fam, side)
                    p["ticker"] = t
                    p["fill_date"] = add_trading_days(
                        d.index[i].date(), 1).isoformat()
                    pending.append(p)
    pending.sort(key=lambda p: (p["paused"], p["grade"], p["ticker"]))
    return {"asof": asof.isoformat() if asof else None,
            "scanned": today.isoformat(), "pool_n": len(pool),
            "pool": pool, "watch": watch, "pending": pending,
            "rs_median": round(rs_med, 4)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", action="append", default=[])
    ap.add_argument("--scan", action="store_true",
                    help="premarket path: watch pool + today's setups")
    ap.add_argument("--watch", action="store_true",
                    help="print the current watch file")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--family", action="append", default=[],
                    help="restrict --backtest to families (PB BO SQ RC)")
    ap.add_argument("--pool-n", type=int, default=FUNNEL_N)
    ap.add_argument("--split", default=None,
                    help="TRAIN/TEST split date (default median trade)")
    ap.add_argument("--save", default=None,
                    help="--backtest: write the trade table to this csv")
    ap.add_argument("--channels", default="dry")
    ap.add_argument("--no-refresh", action="store_true",
                    help="--scan: skip the daily-cache top-up (offline)")
    a = ap.parse_args()
    today = dt.date.today()
    if a.backtest:
        pool = [x.upper() for x in a.ticker] or funnel_pool(a.pool_n)
        fams = tuple(f.upper() for f in a.family) or FAMILIES
        tr = backtest(pool, fams)
        print(summarize(tr, a.split), flush=True)
        if a.save and len(tr):
            tr.to_csv(a.save, index=False)
            print(f"wrote {len(tr)} trades -> {a.save}", flush=True)
        return 0
    if a.watch:
        st = load_watch()
        print(json.dumps({k: v for k, v in st.items() if k != "pool"},
                         indent=1, default=str)[:6000], flush=True)
        return 0
    if a.scan:
        tickers = [x.upper() for x in a.ticker] or None
        st = scan(today, a.pool_n, tickers=tickers, refresh=not a.no_refresh)
        print(f"FNL scan asof {st['asof']}: pool {st['pool_n']}, watch "
              f"{len(st['watch'])}, setups {len(st['pending'])} "
              f"({sum(1 for p in st['pending'] if texted(p))} texted)",
              flush=True)
        for p in st["pending"]:
            print(f"  {fmt_setup(p)}", flush=True)
        for t, w in sorted(st["watch"].items(),
                           key=lambda kv: (-kv[1]["score"], kv[0])):
            print(f"  {t:6s} {w['bias']:6s} {w['score']:.1f} "
                  f"{' '.join(w['flags'])}", flush=True)
        if a.channels != "dry":
            save_watch(st)
        # execution layer: today's texted setups of the configured families
        # become ENTRY intents for the executor session (dry runs count only)
        try:
            from execution import entry_intents_from_scan, load_config  # noqa: E402
            from execution import hv as _hv, write_intents  # noqa: E402
            from daily_cache import load as _load  # noqa: E402
            ecache = {}
            try:
                from earnings import load_cache  # noqa: E402
                ecache = load_cache()
            except Exception:
                pass

            def hv_fn(t):
                c = _load(t, "1d")["Close"]
                return _hv(c, 20), _hv(c, 60)
            ents = entry_intents_from_scan(st, today, load_config(), hv_fn,
                                           ecache)
            if a.channels != "dry" and ents:
                write_intents(ents, [], today)
            print(f"  exec: {len(ents)} ENTRY intent(s)"
                  f"{' written' if a.channels != 'dry' else ' (dry, not written)'}: "
                  f"{' '.join(i['ticker'] for i in ents)}", flush=True)
        except Exception as e:  # the texts never wait on the exec layer
            print(f"  exec intents skipped: {type(e).__name__}: {e}", flush=True)
        chans = [c.strip() for c in a.channels.split(",")]
        from notify import alert, load_config  # noqa: E402
        cfg = load_config()
        bodies = []
        live = [p for p in st["pending"] if texted(p) and p["side"] == "long"]
        if live:
            bodies.append((setups_text(live, live[0]["fill_date"]), "all"))
        if st["watch"]:
            bodies.append((watch_text(st["watch"], st["pool_n"]), "owner"))
        for body, aud in bodies:
            print(body, flush=True)
            for r in alert(body, chans, cfg, title="FNL", audience=aud):
                print("  ", r, flush=True)
        return 0
    if not a.ticker:
        ap.error("give --ticker, --scan, --watch or --backtest")
    from daily_cache import load  # noqa: E402
    for t in [x.upper() for x in a.ticker]:
        df = load(t, "1d")
        if len(df) < MIN_BARS:
            print(f"{t}: {len(df)} bars cached (< {MIN_BARS})")
            continue
        d = features(df)
        i = len(d) - 1
        w = watch_flags(d, i)
        print(f"{t} asof {d.index[i].date()}: px {w['px']} bias {w['bias']} "
              f"score {w['score']} flags {w['flags']} adx {w['adx']}")
        for fam in FAMILIES:
            for side in (1, -1):
                m = family_signals(d, fam, side)
                last = np.flatnonzero(m)
                if len(last):
                    j = int(last[-1])
                    tag = "TODAY" if j == i else f"last {d.index[j].date()}"
                    print(f"  {fam} {'long' if side == 1 else 'short':5s} "
                          f"{tag}: {setup_at(d, j, fam, side)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
