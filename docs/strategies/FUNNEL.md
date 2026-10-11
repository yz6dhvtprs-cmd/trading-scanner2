# Swing funnel — pool → watch → setup → fill → manage

Status 2026-10-10: wired and paper-only. Premarket scan at 06:05 PT
(`run_scan.sh premarket` → `algos/funnel.py --scan`), fills and
management in the 15-min intraday loop, Saturday grades each family.
Nothing here is sized or traded by the system; texts are decision
support. Evidence and grades below; change one lever at a time and
re-run `--backtest` before trusting a change (review/LOOP.md).

## Why a funnel

Swing trades are found while they form, not in one pass. The weekly-box
study (docs/PENDING.md, 2026-10-09) showed the alert is the *start* of a
trade's life: bought inside the box, 62% stopped; waited for and bought
at the right spot, hit rates roughly double. So the pipeline is explicit:
a name shows interest → it sits on a watch pool with a bias → a defined
setup prints on completed bars → it fills at the next open within a
chase guard → it is managed to a stop, target, trail or time stop.

## Stages

**0. Pool (~200).** The scan union (spy+qqq master + screener adds) cut
to the top 200 by 20-day dollar volume, price ≥ $15. On 2026-10-09 that
floor was $521M/day (MU … EQIX) — every name is options-liquid.
Recomputed every scan from the local cache; no network.

**1. Watch flags** (completed daily bars; weight in brackets):

| flag | definition | weight |
|---|---|---|
| squeezeN | Bollinger(20,2) inside Keltner(20, 1.5×ATR20) for N ≥ 5 bars | 1 |
| base-hi / base-lo | 20-day range ≤ 8% of price AND the 20-day mean moved < 1.5% in 10 bars, close within 3% of the range high (low) | 1 |
| pullback-up / -dn | EMA21 > EMA50 above SMA200 (mirror), low within 1 ATR of EMA21, RSI 38–55 (45–62 short) | 1 |
| spring / failed-break | the undercut-reclaim (overshoot-fail) pattern printed in the last 2 bars | 1 |
| RS+ / RS− | 63-day return minus the pool median ≥ +10% / ≤ −10% | 0.5 |
| dry | 5-day volume < 0.7 × 20-day volume | 0.5 |

Score ≥ 1.5 makes the watch pool (62 of 200 on 2026-10-09; 25 at ≥ 2).
Bias = long when EMA21 > EMA50 above the 200-day, short when the mirror
holds, else either. A plain uptrend or a plain new high scores nothing
— those are not coils (the first cut of these flags admitted 168/200
for exactly that reason).

**2. Setup families** (signal on a completed bar; fill at the next open
within 1 ATR of the level, never through the setup's own stop):

| family | definition | risk unit | exit | horizon | grade |
|---|---|---|---|---|---|
| PB trend pullback | algo.json secondary, `signal_mask("pullback")` verbatim, ADX ≥ 20 | per `risk_of` | chandelier 2.5 ATR, 40 bars | 2–8 weeks | B+ |
| BO 20-day breakout | algo.json primary, `signal_mask("breakout")` verbatim, ADX ≥ 20, RVOL ≥ 2 | per `risk_of` | chandelier 3.0 ATR, 40 bars | 2–8 weeks | B+ (see evidence) |
| SQ squeeze release | ≥ 6 bars inside the bands, first close outside, TTM momentum and EMA21 on that side, close in that direction | close − far side of the squeeze range, 0.2–3 ATR | +2R target, 20 bars | 1–4 weeks | long B+, short B |
| RC undercut reclaim | close back above a 20-day low undercut ≤ 3 bars ago (mirror for the high) | close − undercut extreme, 0.2–3 ATR | +2R target, 15 bars | days | C (paused) |

**3. Fill.** The premarket text lists today's B+ setups; the first
intraday run books the open as the paper fill or invalidates it
(gap beyond 1 ATR of the level, or through the setup stop). B+ and up
are texted, B books silently, C never books. One open position per
family per name.

**4. Manage.** The tracker marks FNL rows off the daily print: stop
(−1R), target (+R), chandelier trail ratcheted from the daily cache
(a trail exit books the locked-in R), time stop in trading days. One
batched `FNL paper:` text per run. Saturday grades `FNL-*` rows
against the baselines in `ops/saturday.py`.

## Evidence (2026-10-10 backtest)

`python algos/funnel.py --backtest --save research/_cache/funnel_trades.csv`
— top-200 pool, signals 2025-07-16..2026-10-06, next-open fills,
2.5 bps per side, TRAIN/TEST split at the median trade date (2026-02-19).

| family/side | n | win | expR | PF | TRAIN | TEST | verdict |
|---|---|---|---|---|---|---|---|
| SQ long | 165 | 49% | +0.19 | 1.41 | +0.18 | +0.22 | B+, wired |
| SQ short | 159 | 43% | +0.00 | 1.00 | −0.06 | +0.04 | B, paper only |
| PB long | 49 | 20% | +0.65 | 1.78 | +0.10 | +1.68 | B+ (algo.json), wired |
| BO long | 24 | 21% | −0.37 | 0.52 | −0.54 | −0.10 | B+ kept per algo.json's 5y A; this window says no |
| RC long | 2002 | 39% | +0.09 | 1.15 | +0.13 | +0.05 | C: no slice better, 30+/week |
| PB/BO/RC short | — | — | ≤ 0 | — | — | — | C |

SQ long slices: early release (6–8 bars in the squeeze) 54% / +0.40R
(TEST +0.60, n=20); release bar RVOL ≥ 1.2 52% / +0.25R (TEST +0.43,
n=20); bull regime + RVOL 53% / +0.29R. These are small; the text shows
`sq Nd` and `+vol` rather than filtering on them. By quarter the edge is
uneven (2026 Q1–Q2 ≈ +0.58R, 2025 H2 and 2026 Q3 ≈ 0): expect streaks.

Pool-wide signal rate: SQ ~2.5/week each side, PB 0.8, BO 0.4, RC
30–45 (why it is paused). Expect about four texted setups a week.

## Options mapping

| family | structure | why |
|---|---|---|
| RC | debit vertical 14–21 DTE, short strike at target | days-long hold, defined risk |
| SQ | debit vertical 30–45 DTE, short strike at target | 1–4 week hold; the spread survives the IV drop after the release |
| PB | debit vertical 30–45 DTE; A-grade leaders: long call 60–90 DTE 0.65Δ | pullbacks in leaders are the position-trade candidates |
| BO | long call 60–90 DTE 0.65Δ (trail per algo) or debit vertical | trailing exits need time |

Not mapped here: 0–1 DTE. Daily-bar setups have multi-day horizons;
0DTE belongs to intraday index/mega-cap setups (the RPS track), and
even there the account-growth goal is better served by defined-risk
structures. IV rank is not in the mapping yet — check it by hand (or
through the Robinhood seam in `algos/rh_mcp.py` once it carries option
data): prefer spreads when IV rank > 50, long options when < 30.

## What it sends

- Premarket (06:05 PT): `FNL setups for <date>` (B+ and up, one line
  each: grade, side, family, level, risk, `sq Nd +vol` for SQ, options
  structure) and `FNL watch N/200:` (top names per flag).
- Intraday: `FNL fills:` (fills + invalidations), `FNL paper:` (stops,
  targets, trail exits, time stops, +2R notes).

## Files

`algos/funnel.py` (pool, features, families, backtest, scan, fill and
trail logic), `algos/test_funnel.py` (12 offline tests),
`data/funnel_watch.json` (gitignored state: watch, pending, pool),
`logs/funnel-<date>.log`, `research/_cache/funnel_trades.csv`
(gitignored backtest table).

```
python algos/funnel.py --ticker NVDA                 # flags + last setups
python algos/funnel.py --scan --channels dry         # premarket path, dry
python algos/funnel.py --scan --channels dry --no-refresh
python algos/funnel.py --backtest --family SQ --split 2026-02-19
python algos/funnel.py --watch                       # the state file
```

## Known limits / next

- No earnings veto yet (SPECS.md asks for one). Needs a calendar source
  in the offline loop; the Robinhood MCP has one in-session.
- Pool is "liquid today": mild survivorship in the backtest.
- The 06:45 fill reads Yahoo's lagged open; the chase guard absorbs it.
- Shorts are starved in this regime (algo.json's note stands).
- SQ_MIN_DAYS=6 and the +2R/20-bar exit were set a priori, not tuned;
  a target sweep (1.5R/2.5R/trail) is the obvious next study, TRAIN/TEST
  locked.
