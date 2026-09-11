# Research results: swing prediction + range loop (AAPL-first, 2026-09-11)

## 1. Swing predictor scoreboard (AAPL, 2y daily, 74 walk-forward points)

HIT = right direction AND level within 1%. Fit = first 37, holdout = last 37.
Every version sees only the prior 50 bars + confirmed swings (zero lookahead).

- v1 (trend 1.272 ext; range R1/S1): 2/74, med err 7.3%
- v2 (v1 + RSI-21 regime: strong 1.618 ext / weak 0.786 retrace): **9/74
  (11% fit / 14% holdout), med err 5.1% — current best**
- v3 (alternation + 50% retrace): 4/74, med err 4.7%, dir 62%
- v4 (alternation + 38.2% retrace): 3/74, med err 5.4%

### v2 hits (asof -> truth)

| asof | pred | truth | err | branch |
|---|---|---|---|---|
| 2025-05-29 | dn 197.18 (1d) | low 195.83 in 1d | 0.69% | range |
| 2025-06-24 | dn 196.67 (1d) | low 198.30 in 4d | 0.82% | range |
| 2025-07-14 | up 213.27 (2d) | high 214.74 in 5d | 0.68% | weak-up |
| 2025-10-24 | up 277.42 (4d) | high 276.30 in 5d | 0.41% | strong-up |
| 2025-12-23 | up 276.39 (1d) | high 274.68 in 1d | 0.62% | range |
| 2026-02-19 | up 274.77 (3d) | high 275.62 in 5d | 0.31% | weak-up |
| 2026-03-13 | dn 247.80 (1d) | low 245.56 in 5d | 0.91% | strong-down |
| 2026-07-28 | up 344.36 (1d) | high 344.27 in 1d | 0.03% | strong-up |
| 2026-08-11 | dn 303.22 (1d) | low 300.57 in 1d | 0.88% | weak-down |

v1 hits: 2025-10-15 up 262.42 -> high 264.31 (0.72%); 2026-03-12 dn
247.14 -> low 245.56 (0.64%). v3/v4 hits in `AAPL/swingtest_v3/v4.csv`.
Full tables: `research/AAPL/swingtest_v1-v4.csv`.

## 2. 10-day range loop (1500+ combos, AAPL/QQQ/SPY)

Target: next-10-day high AND low each within 1%.

- SPY close +/-1.5xATR20 (no trend): **28.4% hits**, med err 0.75%/1.21%
- SPY + rsi50 asymmetry: 27.2% hits, direction 61.7%
- QQQ best: 13.6%. AAPL best: 6.2% (macd, hi20+1.0ATR / close-1.5ATR).
- Full grid: `research/loop_results.csv`.

$25k month-sim vs SPY (select Jun+Jul, holdout Aug 2026, costs in): wave 1
"best" was buy-hold luck (dir 46%); wave 2 fade +7.4% sel -> Aug -2.2%
Worst; wave 3 regime-adaptive +5.8% sel -> Aug -3.0% Worst; wave 4
long/flat timers all negative on selection. No timing edge survived.
Details: `wave2.csv`, `wave3.csv`, `loop_best.json`, SWING_ALGO.md §6.

## 3. Stop-loss methodology per system

- `signals.py` (long/short/reversal/rejection): structure + 0.25xATR14
  buffer. Long: min(bar low, swing low) - buffer. Short mirrors above.
  Reversal: under support - buffer. Rejection: above resistance + buffer.
  Targets = opposite swing; RR printed per setup.
- Live analyzer (`scanner/analyze.py`): base Long uses validated
  `risk_of()` (breakout: back inside the level, clamped 0.2-3xATR);
  qualifiers use level +/-0.5% (beyond invalidation).
- Backtest engine (`backtest/combos.py`): fixed -1R / +2-3R + 20-bar time
  stop; chandelier trail 2.5-3.0xATR + 40-bar cap; 2.5bps/side.
- Month capital sims: NO stop-loss. Daily rebalance on edge sign only;
  exits = signal flips. Wiring signals.py stops into the sim loop is the
  open follow-up.

## 4. Repro

```bash
python research/fetch_stock.py --ticker XXX
python research/fetch_context.py --ticker XXX
python research/backtest_swings.py --ticker XXX --version v2
python research/range_loop.py --budget-min 12
```
