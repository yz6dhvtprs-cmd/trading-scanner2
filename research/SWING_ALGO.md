# Swing-prediction algorithm (research track, AAPL-first)

Feed a new stock: `python research/fetch_stock.py --ticker XXX`
(+ `fetch_context.py`), then run the harness + scan below. No code changes
needed — everything is ticker-parameterized under `research/<TICKER>/`.

## 1. Data maps (per ticker, 15 files)

| File | Content |
|---|---|
| `daily_100.csv` | Last 100 daily OHLCV (rebuild each trading day) |
| `h1_100.csv` / `m15_100.csv` | Last 100 1h / 15m OHLCV |
| `weekly_10.csv` | Last 10 weekly OHLCV |
| `ind_1d/1h/15m.csv` | MA 8/10/21/50/100/200, EMA 8/21/34/55/89/144/200, RSI-21, MACD 12/26/9, VOL, VMA-10, RVOL-10, VWAP (session on intraday, 20d rolling on daily), ATR-14 |
| `swings_1d.csv` | Fractal (k=3) swing highs/lows with `known_at` confirmation bar — causal reads only |
| `volume_profile.json` | POC/VAH/VAL + top-5 nodes from the 15m window |
| `spy_100.csv` / `vix_100.csv` | Market context (beta, regime) |
| `news_15m.csv` | Recent headlines → nearest 15m candle + 4h forward return (free API = recent window only) |
| `earnings.json` | Earnings dates + EPS surprise trail (~4y) |
| `fundamentals_q.csv` | Quarterly revenue/gross/op/net income (10-Q/10-K headlines; IR pages are JS-walled, EDGAR links in earnings.json) |

## 2. Four separated algos (`signals.py`, `scan_all`)

- **long**: uptrend structure (HH+HL) + pullback to last-leg fib/EMA21 + RSI-21 40–68 + MACD ok + 1h above EMA55. Stop under swing low, target last swing high.
- **short**: mirror (downtrend, RSI 32–60, 1h below EMA55).
- **reversal**: hammer/doji + RSI-21 < 42 within 2.5% of support + 15m reclaimed EMA21. Long bias.
- **rejection**: shooting-star/doji + RSI-21 > 58 within 2.5% of resistance + 15m under EMA21. Short bias.

All stops/targets/ETA derive from swings + ATR; `rr` printed on every setup.

## 3. Swing predictor scoreboard (AAPL, 2y daily, 74 walk-forward points)

HIT = right direction AND level within 1%. Fit = first 37 points,
holdout = last 37. Every version sees only the prior 50 bars + confirmed
swings — zero lookahead.

| Ver | Logic | Fit | Holdout | Median err |
|---|---|---|---|---|
| v1 | Trend 1.272 ext of last leg; range → R1/S1 pivot | 3% | 3% | 7.3% |
| v2 | v1 + RSI-21 regime (strong → 1.618 ext, weak → 0.786 retrace) | 11% | 14% | 5.1% |
| v3 | Alternation + 50% retracement of last leg | 5% | 5% | 4.7% |
| v4 | Alternation + 38.2% retracement | 3% | 5% | 5.4% |

**Current best: v2.** Direction 51%, median level error ~5% — the 1%-on-every
swing bar is not cleared. Two structural findings, not tuning noise:

1. 46% of "next fractals" print within 3 bars — minor noise extremes, not
   tradeable swings. Predicting major swings against minor-fractal truth is
   a category mismatch. Next step: score against significant swings only
   (leg ≥ 2× ATR) as the primary target.
2. Predicting FROM a fresh confirmed extreme with continuation logic fights
   mean-reversion; alternation (v3) fixed direction (62%) but levels still
   miss — pullback depth varies more than any fixed fib ratio captures.

## 4. How to run (any ticker)

```bash
python research/fetch_stock.py --ticker XXX
python research/fetch_context.py --ticker XXX
python research/backtest_swings.py --ticker XXX --version v2  # scoreboard
python research/backtest_swings.py --ticker XXX --version v2 --asof YYYY-MM-DD  # one date
python -c "import sys; sys.path.insert(0,'research'); from signals import *; [print(s) for s in scan_all(load_maps('XXX'), -1)]"
```

## 5. Standing rules for this track

- No lookahead: swings by `known_at`, 50-bar windows, train/holdout split.
- Intraday (1h/15m) confirms live signals only — never backtested (history
  too short); the scored core is daily structure.
- A version earns "current best" only by beating holdout, never fit alone.
- Today's AAPL v2 read (2026-09-11, close 333.10): UP to 337.32, ETA 1d
  (strong-up 1.618 ext). No long/short/reversal/rejection setup firing.

## 6. 15-min range loop log (2026-09-11, `range_loop.py` + waves 2–4)

Target: next-10-day high AND low within 1%; then $25k month-sim vs SPY.
1500+ combos (trend x high-model x low-model), walk-forward, no lookahead.

| Wave | Idea | Best (selection) | Holdout Aug26 |
|---|---|---|---|
| 1 | ATR/Keltner/fib x trend-asymmetry | SPY ±1.5ATR 28% hits; AAPL 6%; QQQ 14% | AAPL sim +3.74 vs SPY +1.77 "best" = buy-hold luck (AAPL bh +3.77), dir 46% |
| 2 | Vol-scaled k + fade variants, sel Jun+Jul | QQQ fade +7.4% sel | −2.2% Worst (fade wins chop, dies in trends) |
| 3 | Regime-adaptive (ADX: follow/fade) | AAPL +5.8% sel | −3.0% Worst |
| 4 | Long/flat timers | all sel < 0 | ≈ buy-hold minus costs |

VERDICT: neither target met. Reusable: SPY 10d range ≈ close ±1.5×ATR20
(28% within 1%, med err 0.75%/1.21%) as a stop/target estimator; trend-mid
direction 60–62% on SPY does not convert to P&L (right small, wrong big).
