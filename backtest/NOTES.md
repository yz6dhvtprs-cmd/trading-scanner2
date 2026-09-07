# Validation findings — 2026-09-04 run (2y daily, 50 tickers, 300 cells)

Command: `python backtest/run.py --universe universe/universe_live.csv`
Full rows: `backtest/results.csv`. Pooled view: `/tmp/pool.py` (scratch, re-runnable).

## Headline: gate held — nothing is tradable yet

- 297/300 cells have <8 test trades; median test_n = 0. Per-ticker daily
  grading is statistically meaningless. Grade pooled strategy×side instead.
- Zero A-range grades on test. No live capital on any variant.

## Pooled expectancy (n-weighted, net of 2.5bps/side)

TRAIN (18mo): pullback-short +0.20 (n=12, thin); retest-long +0.04 (n=355);
pullback-long -0.03 (n=32); retest-short -0.16; breakout-short -0.17 (n=7);
breakout-long -0.61 (n=7).
TEST (6mo): pullback-long +0.20 (n=17, thin); retest-long +0.00 (n=189);
breakout-long -0.04 (n=3); pullback-short -0.30; retest-short -0.48.

## Diagnoses

1. Breakout as coded is nearly extinct on megacap dailies (RVOL>=2 + 20d high
   fired 7× in 18mo across 50 names) and negative. Breakouts live intraday;
   either relax (RVOL 1.5, 55d level) on TRAIN only, or move breakout to 15m/1h.
2. Retest-long is the sole high-sample variant and sits at ~zero edge with a
   fixed 2R target. Exit design (1.5R target, partials + trail per SPECS.md)
   is the next lever — tune on TRAIN, confirm on untouched TEST.
3. Shorts are broadly negative: 2024–2026 was a megacap bull regime. Shorts
   need regime-conditional grading (below SMA200 / high-RVOL selloffs) or
   longer history spanning a bear phase. Do not conclude "shorts never work".
4. Fixed 2R-everything understates SPECS.md (swing-high targets, partials).
   Next harness version should simulate spec exits, not fixed-R exits.

## Target sweep 2026-09-05 (`--target-r`, files results_R{1.5,2.0,2.5,3.0}.csv)

- Exits are programmable: `--target-r` (default 2.0 = 1:2), `--time-stop` (default 20).
- 1:2 verdict: NO gradable setup. Best pooled test at 2.0 is pullback-long
  +0.20R on n=17 (thin, and train was -0.03R = noise, not edge).
  Retest-long at 2.0 is +0.00R on n=189.
- Coherent pattern: long expectancy rises monotonically with target in BOTH
  train and test (retest-long test: -0.10 -> +0.10 from 1.5R to 3R;
  pullback-long train -0.07 -> +0.19). Winners run past 2R on average.
- Closest to edge: retest-long 3R, train +0.08 (n=335) / test +0.10 (n=183)
  — consistent sign, still below the B+ 0.15 bar.
- Next lever is exit DESIGN (partials + trail per SPECS.md), not larger
  fixed targets.

## Finalized algo v1.0 (2026-09-04, S&P 500 pool, 5y daily, 72+60 combos)

- Scope change: pool widened top-50 -> full S&P 500 ex-META per user direction.
  (Longer history 5y + pooled stats fixed the n=0 problem: 499 frames cached.)
- PRIMARY (grade A): breakout long, ADX>=20, RVOL>=2, chandelier trail 3.0xATR.
  train n=124 +0.72R PF 2.0 | test n=50 +0.59R PF 1.8, win rate ~30%.
  Robust across ADX 20-22 and trail 2.5-3.5; ADX<20 fails TEST (gate is real);
  ADX>=25/28 thins out and degrades.
- SECONDARY (grade B+): pullback long, ADX>=20, trail 2.5xATR.
  train n=271 +0.28R | test n=105 +0.25R.
- PAUSED (grade C): all shorts (bull-regime starvation), retest variants (~0 edge).
- Spec: algo.json. Monday rehearsal (scanner/rehearse.py): 1 triggered (CRWD
  secondary, paper), 6 building (GILD GPC HOOD MOS ULTA VLO).
- Standing warning: test n=50 for primary is good, not huge; Saturday loop
  demotes on negative live last-20 per LOOP.md.

## Confirm round (2026-09-07): MTF / RSI-range / MACD / hidden-div, additive

Precedence per research: weekly bias > daily structure > strength (ADX/RSI) >
trigger; EMA-stack agreement across TFs as the trend-structure read.
- Base signals KEPT as-is. MTF-weekly: neutral on breakout, +0.01-0.02R on
  pullback -> watchlist/score input, not a gate. RSI-range: no-op (current
  zones already sit inside Cardwell bull range) -> documented, not added.
- MACD: REJECTED as gate — train says harmful on pullback (-0.37R) while test
  says helpful (+0.57R); violent disagreement = overfit risk.
- Hidden divergence: pullback +0.28->+0.41R train AND +0.25->+0.37R test
  (n=42/17, thin but agreeing) -> informational boost flag, revisit with data.
- Stacking all confirms: n collapses to ~0. Never stack; flow, don't filter.
- Two-tier flow live in scan.py: Stage-0 9-MA alignment watchlist (logged to
  watchlist.csv, iMessage digest) -> Stage-1 base signals with confirm flags
  (M/R/D/X) + live D/1h/15m stack votes (live-only; 15m too short to backtest).

## Standing rules (from grading.md, confirmed)

- <8 test trades -> B (thin), never promoted by win rate.
- Any parameter change re-runs from TRAIN; TEST stays locked until confirmation.
