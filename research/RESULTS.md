# Research results — prediction track (rewritten 2026-09-11)

**Headline: the swing predictor never had an edge. v1–v4 are all significantly
WORSE than forecasters containing zero information, and the earlier "v2 is
current best" conclusion is retracted.** The cause was not a bug in the
predictor. It was that no null model was ever computed, so an unremarkable
number was read as a modest edge.

Everything below is reproducible offline from `research/_cache/`
(136 tickers x 15y daily, built by `research/cache_data.py`).

## 1. What was actually wrong

The old scoreboard reported `hit_rate` with nothing beside it. Scored against
its own rule, the following forecasters — which look at nothing — do better:

AAPL, 15y, 682 walk-forward points, the harness's own point selection and
its own HIT rule (right direction AND level within 1%):

| forecaster | hits | rate | vs v2 | McNemar p |
|---|---|---|---|---|
| NULL close ± 0.75×ATR14, direction = alternation | 134 | 19.7% | **+9.7pp** | 0.000004 |
| NULL close ± 1.0×ATR14, direction = alternation | 134 | 19.7% | **+9.7pp** | 0.000004 |
| NULL close × 1.02, direction = always up | 131 | 19.2% | **+9.2pp** | 0.000009 |
| NULL close ± 1.0×ATR14, direction = coin flip | 109 | 16.0% | +6.0pp | 0.0013 |
| v2 ("current best") | 68 | 10.0% | — | — |
| v1 | 37 | 5.4% | | |
| v3 | 32 | 4.7% | | |
| v4 | 29 | 4.3% | | |

Verdict printed by the harness itself now: *v1, v2, v3 are significantly worse
than 4 of 6 zero-information baselines; v4 is worse than 5 of 6.*

Two further diagnostics explain why, and both were confirmed independently:

- **The level model is worse than doing nothing.** v2's median level error is
  3.82%; simply emitting today's close gives 2.85%.
- **The direction model is worse than a constant.** v2's direction accuracy is
  53.0% against a majority-class rate of 53.3%. v3's celebrated "62%" is the
  alternation base rate, not skill — a rule that just alternates after every
  swing scores 56.5% on a 12,661-point panel with no information at all.

The 1% band is also mis-scaled: the median distance from the as-of close to the
next fractal is about 2.9%, so a 1% band mostly measures whether the move
happened to be small, i.e. realized volatility, not forecast quality.

**Verified clean:** there is no lookahead anywhere in the prediction path. The
k-bar fractal confirmation lag is honoured, indicators are causal, and the
`known_at` discipline holds. That part of the old work was done correctly. The
problem was statistical, not causal.

**Also retracted:** the "SPY 10-day range = 28.4% hits" headline. It is the
99.8th percentile of a 500-combo in-sample search, and a constant band with no
search at all matches or beats it. It is a volatility estimate, not a forecast.

## 2. What replaced it

The old target — "the exact price of the next k=3 fractal, within 1%" — is not
the question the rest of this repo asks. `grading.md`, `backtest/NOTES.md` and
`review/LOOP.md` all grade on **expectancy in R**, and explicitly say win rate
never promotes a grade. The research track had drifted onto an accuracy metric
that does not convert to P&L, which is exactly what the month simulations then
discovered the hard way.

New stack, all scored on expectancy:

| file | role |
|---|---|
| `cache_data.py` | one-time local OHLCV cache; makes every run reproducible offline |
| `panel.py` | causal features (31) + triple-barrier labels; entry at next open, stop wins intrabar ties |
| `models.py` | ridge logistic regression and gradient-boosted stumps, both pure numpy (no sklearn on this box) |
| `evaluate.py` | purged/embargoed walk-forward, date-block bootstrap, circular-shift permutation test, `grading.md` rubric |
| `run_v5.py` | absolute (per-bar) study |
| `cross_section.py` | beta-neutral ranking study |
| `sanity_check.py` | proves the harness can detect an edge that is really there |
| `baselines.py` | the nulls, now wired into the old harness so it can never again print a bare hit rate |

The question is now: *given the features at the close of bar t, should a trade
be opened at the next open, and what is its expected R?* Barriers are
stop = 1.5×ATR14, target = 2R, time limit 20 bars.

## 3. AAPL result (single ticker, 15y, purged walk-forward)

3,553 bars, 2,547 out-of-sample rows, 6 folds, 2016-06 → 2026-08.

| rule | trades | win rate | expectancy R | 95% CI | grade |
|---|---|---|---|---|---|
| take every bar (pure exposure) | 2,547 | 50.3% | **+0.403** | [+0.347, +0.460] | A |
| shuffled probabilities (null) | 794 | 50.8% | +0.424 | [+0.323, +0.518] | A |
| repo primary: breakout ADX≥20 RVOL≥2 | 17 | 52.9% | +0.388 | [−0.218, +1.020] | A (thin) |
| logistic model, p ≥ q70 | 794 | 55.4% | +0.562 | [+0.466, +0.661] | A+ |
| boosted stumps, p ≥ q95 | 237 | 54.0% | +0.554 | [+0.365, +0.741] | A+ |

That table looks like a win. It is not, for two reasons.

**First, the baseline is beta.** Buying AAPL on any random bar and managing it
with these barriers returns +0.40R, which grades A on the repo's own rubric
before any model exists. AAPL rose roughly 16x over the sample. Any long-only
result on this ticker inherits that.

**Second, the lift does not survive a valid test.** The model's trades are a
subset of the always-trade population, so comparing their confidence intervals
is not a significance test. Holding the selection size fixed and destroying
only the alignment between score and outcome (circular-shift permutation,
2,000 draws, which preserves the autocorrelation that overlapping 20-bar labels
create):

| model | best rule | observed R | same-size null R | null 95th pct | p | Bonferroni p |
|---|---|---|---|---|---|---|
| logistic | q70, 794 trades | +0.562 | +0.399 | +0.604 | 0.093 | 0.463 |
| boosted stumps | q95, 237 trades | +0.554 | +0.397 | +0.748 | 0.216 | 1.000 |

Neither is significant. Selection skill is **not** demonstrated on AAPL.

Discrimination agrees: AUC 0.527 (logistic) and 0.500 (stumps).

## 4. The harness is not the problem — `sanity_check.py`

"No edge" and "broken code" produce the same AUC. These four checks separate
them, on AAPL:

| check | result | reading |
|---|---|---|
| train vs test AUC | 0.65 / 0.69 train, 0.52 / 0.52 test | the learner fits; the signal does not generalise |
| planted signal, strength 0.02 → 0.25 | test AUC 0.519 → 0.619 | the pipeline recovers a real edge, and scales with it |
| shuffled labels | test AUC 0.459 | no leakage |
| label arithmetic | stop 51.4%, target 37.1%, timeout 11.5% | barriers self-consistent |

So the negative result is a finding about the data, not about the code.

## 5. What is genuinely reusable

- **`close ± 1.0×ATR14` as a level estimator.** It beat every hand-built
  predictor here by 6–10 points. Use it for stop and target placement, which is
  what it is actually good for. Do not call it a forecast.
- **The causal data plumbing.** `fractal_swings`, the `known_at` discipline and
  the indicator maps verified clean under adversarial review.
- **The expectancy rubric.** It was right all along; the research track just
  stopped using it.

## 6. Honest ceiling

From daily OHLCV alone on one megacap name, independently estimated during the
audit: direction AUC around 0.62 at 4+ bars against a 57.6% majority base rate,
and level R² around 0.02 in ATR units. A 1%-band point forecast of the next
swing is not reachable, and chasing it further is not a good use of time.

The productive directions from here, in order: (a) intraday data, where the
repo's own 60d study already found structure the daily bars cannot see;
(b) cross-sectional ranking over a wide universe rather than one ticker; and
(c) treating volatility, not direction, as the forecastable quantity — the ATR
band result says that is where the predictability actually lives.

## 8. Follow-up: does a size forecast improve the graded directional algos?

Tested 2026-09-11 across five strands with adversarial verification. Short
answer: no, because R is denominated in ATR and the size forecast is 94%
rank-redundant with that ATR, so the effect cancels. The mechanism is real
under perfect foresight (+1.86R oracle spread) but the forecastable component
is volatility LEVEL, which the risk unit already absorbs. The run also found
the grade-A breakout primary is regime-dependent: -0.40R over 2012-2020 versus
+1.66R over 2021-2026. Full writeup: `research/SIZE_PLUS_DIRECTION.md`.

## 7. Repro

```bash
python research/cache_data.py                                        # once
python research/backtest_swings.py --ticker AAPL --version v2 --period 15y
python research/run_v5.py --model logit --tickers AAPL --folds 6
python research/sanity_check.py --tickers AAPL
python research/cross_section.py --top-k 10 --universe universe/universe_sp500.csv
```
