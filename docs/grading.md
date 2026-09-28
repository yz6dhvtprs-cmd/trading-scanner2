# Grading rubric v1 — expectancy-based, per direction

Computed on walk-forward TEST period, net of fees + slippage:

- expectancy_R = win_rate × avg_win_R − (1 − win_rate) × 1
- profit factor, max drawdown recorded alongside.

## Grades

- A+: test expectancy ≥ +0.50R, PF ≥ 1.5, multi-timeframe agreement, RVOL confirm.
  tradable with stock or single-leg options.
- A: expectancy ≥ +0.30R, PF ≥ 1.3. Tradable; options preferably spreads.
- B+: expectancy ≥ +0.15R, or strong pattern but single-timeframe only.
  Spreads only or reduced size.
- B: valid pattern, marginal/negative test expectancy. Paper only.

## Notes

- Win rate alone never promotes a grade. A 65% win rate at 1R is worse than
  40% at 2R — grade the expectancy.
- 1:5 realized R is aspirational; backtests show most winners land 1.5–2.5R.
  Do not force 5R targets — that converts winners into losers.
- Grades expire: any strategy with negative expectancy over its last 20 live
  (paper or real) trades is demoted to B automatically.
