# Indicators: what we use, what to add next

Rule: one new filter at a time — every filter cuts sample size, and our
bottleneck is already n (median test trades per cell = 0). Each candidate
below is validated on TRAIN with TEST locked, per review/LOOP.md.

## In use today (backtest/run.py)

- EMA 9/21/50 stack — trend direction + pullback zone. Limitation: flat/whippy
  in chop; the retest chop filter (max 2 crosses/10 bars) only partly fixes it.
- SMA 200 (daily close) — bull/bear regime gate. Confirmed working as intended:
  it correctly holds longs to the bull side; shorts starve in this regime.
- RSI 14 zones (38–55 pullback, 50–67 breakout) — stretch gate so we don't buy
  extended moves. Zones are inherited, not fitted — tune on TRAIN if touched.
- ATR 14 — stop distances, chase guard, cost-to-risk math. Load-bearing and sound.
- RVOL vs 50d average (≥2.0 breakout) — too strict on megacap dailies (7 fires
  in 18mo across 50 names). First candidate for relaxation or intraday move.
- Prior 20-day high/low — breakout level. Fine on intraday; sparse on daily.

## Add next, in this order (mapped to our evidence)

1. ADX(14) trend-strength filter (e.g. ADX > 20, rising) on pullback/retest.
   Why: public trend systems pair EMA-stack entries with an ADX gate to skip
   weak-trend chop — exactly where our retest expectancy bleeds to zero.
   Expect fewer trades but cleaner ones; watch n.
2. ATR-trailing exits (chandelier: trail 2.5–3×ATR after +1R; partial ½ at
   ~1.5R, stop to breakeven). Why: our target sweep shows expectancy rising
   monotonically with target — fixed targets behead runners. This is the exit
   DESIGN step queued in backtest/NOTES.md.
3. MAE/MFE recording per trade (diagnostic, not a filter). Losers' average MFE
   tells whether a trailing stop or earlier exit would have saved them.
4. VWAP bias + EMA slope + higher-timeframe RSI — for the intraday extension
   (15m entries under 1h/daily trend). Standard intraday stack.
5. Later candidates, honestly labeled: MACD/Stochastic cross (lagging —
   use as confirmation, never the trigger), Bollinger+Keltner squeeze
   (breakout tripwire compatible with our no-sideways rule), Supertrend
   (ATR-trailing in indicator form), OBV/volume-MA (confirmation only).

## Sources (inspected this run)

- [ADX + EMA + RVOL filter stacks in public strategies](https://www.tradingview.com/scripts/averagedirectionalindex/page-5/?script_access=all)
- [WMA trend + RSI + ADX + fixed TP/SL with trailing](https://www.tradingview.com/scripts/averagedirectionalindex/?script_type=strategies&sort=recent)
- [EMA21 pullback entries with 3×ATR trailing stop](https://github.com/timbrinded/degen-ai/blob/HEAD/docs/strategies/slowrider-trend.md)
- [Fixed vs trailing vs partial exit modes](https://github.com/amirimani/trend)
- [Trailing after +1R at 1.5–2×ATR plus time-stop](https://github.com/quochuy201/trading-system/blob/HEAD/skills/backtest/SKILL.md)
- [Fixed-target/time-stop mismatch as universe-volatility symptom](https://github.com/pavan-sai-grandhi/everything-finance/blob/HEAD/skills/backtest/references/reference.md)
