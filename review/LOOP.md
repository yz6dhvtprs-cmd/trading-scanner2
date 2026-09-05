# Continuous-improvement loop v1

Signals are predictions. Every prediction gets tracked, scored, and fed back.
Saturday review is the heartbeat; the rules below keep it from overfitting
to one bad week or one war headline.

## 1. Track everything (scanner/signal_log.csv)

Every alert — paper or real — logs one row at fire time, outcome filled at exit:
`date,ticker,strategy,side,entry,stop,target,grade,timeframe,status,
outcome_R,exit_reason,regime,news_flag,notes`.
- `regime`: SPY vs SMA200 (bull/bear) + VIX bucket at entry.
- `news_flag`: earnings/FOMC/CPI/geopolitical shock inside the holding window.
- No outcome filled = open trade; Saturday ignores opens.

## 2. Diagnose before adjusting (attribution order)

For each variant (strategy×side) with ≥8 closed live trades, compare live
last-20 expectancy vs backtest baseline, then attribute in this order:

1. Sample size — <8 trades: no conclusion, stays B (thin).
2. Regime mismatch — losses cluster in opposite regime (e.g. shorts in a
   bull tape)? Fix = regime-condition the variant, don't delete it.
3. Event-driven — losses flagged news/FOMC/war? Fix = widen event veto
   (the defense against unpredictable news is exposure control, not new
   parameters — never refit params to one headline).
4. Execution — slippage vs backtest assumption? Fix = cost model, size, spreads.
5. Edge decay — clean losses, no regime/event cause? Fix = demote grade,
   then re-validate params on TRAIN with TEST locked.

## 3. Adjustment levers (in increasing strength)

- Grade change (immediate, risk control): demote/promote one step, recorded
  in review/grade_overrides.csv with reason. A new grade C (paused) stops
  new alerts for that variant without deleting its history.
- Exposure change: halve size into opposite regime or VIX > 30; spreads-only.
- Parameter change (slow, guarded): only via TRAIN re-fit + locked-TEST
  confirmation, one lever at a time.
- Universe refresh: quarterly per METHOD.md; Saturday flags it when the
  snapshot is >95 days old.

## 4. Saturday review (automated, cron)

Runs `review/saturday.py`: recomputes live-vs-baseline, applies grade
demotions, checks universe age + market regime (SPY/VIX), writes
`review/REPORT-YYYY-MM-DD.md`, updates overrides. Human reads the report;
grade changes take effect immediately, param changes need explicit approval.
