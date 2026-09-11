# Can a move-size forecast improve the graded directional algos?

**Date:** 2026-09-11 · **Method:** 20 agents, 5 independent strands, every
positive result adversarially re-derived from scratch. 15 findings tested,
7 survived, 8 refuted. All numbers below reproduced by at least two
independent implementations against `backtest/combos.py`.

**Answer: no, and the reason is mechanical rather than empirical.** The size
forecast is real and strong. It cannot help these algos because the quantity it
forecasts is already in the denominator of R.

---

## 1. The hypothesis

Daily OHLCV forecasts the SIZE of a coming move but not its DIRECTION. So take
a directional signal only when a large move is forecast: a trailing exit needs
range to monetise, so expectancy should rise.

The mechanism is sound. It is not what happens.

## 2. The size forecast is real, and it is just ATR

Out of sample, 322,487 rows, 134 tickers, 8 purged and embargoed folds,
2016-06 to 2026-08. Target is the largest absolute displacement over the next
20 bars.

| model | AUC, top third | Spearman |
|---|---|---|
| 31-feature boosted stumps | 0.7695 | 0.5709 |
| single feature, ATR/close | 0.7693 | 0.5701 |
| **raw ATR/close, no model at all** | **0.7698** | **0.5861** |

The 31-feature model adds nothing over one feature, and one feature adds
nothing over the raw ratio. Raw ATR has the best rank correlation of the three.
Whenever this document says "the size forecast", read "ATR".

**And most of that skill is not timing.** Correlating each ticker's mean
forecast with its mean realised move gives rho 0.9876. Within a ticker it is
0.3373. A per-ticker circular-shift null, which preserves every ticker's average
level and destroys only the time axis, already scores AUC 0.6520 against the
observed 0.7695. So roughly 56% of the above-chance skill is the static fact
that Tesla moves more than Coca-Cola. Only 44% is knowing when.

## 3. Why it cannot help: R is denominated in ATR

This is the finding.

`combos.risk_of()` sets the risk unit from ATR, clamped between 0.2 and 3.0
ATR, median 0.572 ATR. R is the outcome divided by that unit.

The size forecast is 94% rank-redundant with the ATR that defines the unit.
Per-fold Spearman between the forecast and ATR runs 0.958 to 0.993.

So forecasting a bigger move simultaneously forecasts a proportionally bigger
risk unit. The numerator and the denominator move together and the effect
cancels. Measured on the reversal signal set, absorption is 116% to 124%: the
high-forecast bucket actually has 30% **less** trailing room per unit of risk,
4.64 range-per-ATR against 5.62, because its risk unit is wider (1.94 against
1.78 ATR).

You cannot gate an ATR-normalised strategy on an ATR forecast. There is nothing
left after the normalisation.

## 4. The mechanism is real, but only with perfect foresight

Give the pullback strategy an oracle that knows the realised forward move size,
in ATR units, and bucket its trades by it:

| forecast tercile | expectancy | MFE | MAE |
|---|---|---|---|
| low | −0.602R | 2.207 | −1.605 |
| mid | +0.513R | 3.809 | −1.661 |
| high | +1.258R | 4.710 | −1.823 |

Spread +1.860R, monotone, permutation p = 0.0000 over all admissible shifts.
Upside grows far faster than downside, exactly as the hypothesis predicts. The
trailing exit does convert range into R asymmetrically.

So the idea is correct. What kills it is that the useful component is range
**expansion relative to current ATR**, and there is no forecasting skill in that
component. The forecastable part of volatility is its level, and the level is
already priced into the risk unit.

## 5. Power: the test could not have found a moderate effect anyway

Standard error of the high-minus-low tercile difference is 0.567R. The minimum
effect detectable at 80% power is 1.588R. Nothing short of the oracle's +1.86R
would show up. Read every null in this document as "not detected at this sample
size", not as "proven zero".

## 6. The bigger problem the test exposed

The strands could not evaluate a filter without first establishing what it
filters, and the baselines did not hold up.

**The grade-A breakout primary is regime-dependent.** Pooled over 15 years and
135 tickers, n=214:

| period | n | expectancy | PF | win rate |
|---|---|---|---|---|
| 2012 to 2020 | 140 | **−0.3994R** | 0.58 | 12.9% |
| 2021 to 2026 | 74 | **+1.6555R** | 3.54 | 35.1% |
| pooled | 214 | +0.3112R, CI [−0.119, +0.820] | 1.37 | 20.6% |

The pooled interval contains zero. The strategy lost money for its first nine
years and made everything in the last five.

The break survives every attack. Scanning all 12 candidate cut years, 2021 is
the argmax and the family-wise permutation p is 0.0214; the bootstrap on the
era gap gives P(gap ≤ 0) = 0.0003. It is not one name: the five largest 2021+
winners are five different tickers with one trade each, and leave-one-ticker-out
moves the figure only from +1.655 to +1.426. It is not market beta: SPY's
unconditional 20-day return is 1.25% in the first era and 1.22% in the second.

Independent corroboration already existed in the repo. `backtest/combos_10y.csv`
runs the same code path on the full 500-name pool and shows the same shape, and
`grading.md`'s own `grade()` applied to that 10-year TRAIN leg returns **B+, not
A**.

**Neither graded algo separates from a matched random-entry null** at this
sample size: p = 0.198 for the primary and p = 0.153 for the secondary. A blind
long on every bar returns +0.3295R with CI [0.228, 0.434]. It does not beat
either algo on point estimate, at +0.4218R and +0.3748R, but the intervals
overlap. The correct statement is that they are indistinguishable, not that the
blind long wins.

**The pullback secondary is fine.** A claim that it failed to reproduce was
refuted: scored on the TEST leg the way `grading.md` actually specifies, it
returns +0.3228R, PF 1.39, n=107, which grades **A**, one notch above its
published B+.

## 7. Two methodological traps worth keeping

Both manufactured apparently significant results inside this run and were caught
only by the adversarial pass.

**Pooling calibrated probabilities across walk-forward folds creates a fake
gate.** Each fold carries its own isotonic calibrator, so per-fold output means
ranged 0.425 to 0.540. A nominal 20th-percentile cut taken on the pooled
distribution deleted 63% of one fold's bars and 4% of another's. That is a
random era selector. It produced +0.481R of which 59% to 65% was pure
composition. Always rank within fold.

**With a mixed-side signal set, any covariate correlated with side fakes
significance.** The reversal set mixes a +0.20R long leg with a −0.31R short
leg. A pooled circular-shift null does not preserve side composition, so a
forecast that merely correlates with side scored p = 0.0097. Stratifying the
permutation by side moved the same result to p = 0.7295.

## 8. What to do

- **Do not gate these algos on a volatility forecast.** The normalisation
  already absorbs it. This is structural and no amount of feature engineering
  changes it.
- **The size forecast is still the right tool for sizing and stop placement,**
  which is where an ATR estimate belongs and where it is not self-cancelling.
- **Treat the breakout primary's grade A as unproven.** It rests on one regime.
  The repo's own 10-year 500-name run grades it B+, and `review/LOOP.md` already
  has the machinery to demote on live evidence. Consider whether the ADX and
  RVOL gates are selecting a condition that only paid after 2021.
- **If the size idea is pursued further, the target must be range expansion
  relative to current ATR, not move size.** That is the component with the
  +1.86R oracle payoff and currently zero measured skill. It is a harder
  forecasting problem and may not be solvable, but it is at least the right one.

## 9. Reproduce

```bash
python research/run_v5.py --model logit --tickers AAPL --folds 6
python research/sanity_check.py --tickers AAPL
python research/meta_model.py --ticker AAPL
python research/confluence.py --ticker AAPL --backtest 1h
```

Workflow run id `wf_6d6d91e9-2ec`; per-agent returns in that run's
`journal.jsonl`.
