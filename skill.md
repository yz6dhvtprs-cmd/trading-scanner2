# Chart-analysis skill v1 — runtime scoring checklist (plan step 4.b)

Validated defaults live in algo.json (v1.0: breakout-long ADX>=20 trail-3.0
primary; pullback-long ADX>=20 trail-2.5 secondary; shorts paused). The steps
below apply those defaults; Saturday overrides (grade_overrides.csv) win.

Run this identically for every ticker × timeframe. Completed bars only.

## 1. Load context

- Universe row (live snapshot, never the TEMPLATE file), sector peer tickers.
- Bars: traded timeframe + one higher timeframe. Long/short bias from higher TF.

## 2. Trend screen

- EMA9/21/50 stack? Higher-TF agrees? VWAP bias (intraday) same side?
- No: stop, score 0, log "no trend".

## 3. Pattern match (strategies/SPECS.md)

- Which of pullback / breakout / EMA-retest matches exactly? Name it.
- Trigger candle present? If building but untriggered → status BUILDING + "trigger if" level.

## 4. Confirmation gates (all required for A-range)

- RVOL ≥ 1.5 (≥ 2.0 for breakouts), RSI zone per spec, ATR stop fits account risk.
- Realistic target ≥ grade minimum R. Sideways/range shape → reject outright.

## 5. Cross-check (plan step 4.a, runtime form)

- Same setup present on 2+ sector peers? Higher-TF structure consistent?
- Earnings/event within 3 sessions → veto swing entries.

## 6. Grade + plan output

- Grade per grading.md. Emit: ticker, direction, strategy, entry, stop, target,
  R-multiple, grade, timeframe, status (NEW/CONFIRMED/DOWNGRADED/INVALIDATED),
  options mapping: A+ → long call/put or debit vertical; A/B+ → debit vertical
  only (same expiry, width per max-loss limit); B → paper only.

## 7. History note

- 2-year validation lives in backtest results, not in this checklist. This skill
  SCORES live bars; it never invents a historical win rate.
