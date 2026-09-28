# Nightly + pre-market routine v1

## Nightly (after close)

Score all 50 (live universe) on daily + 1h with skill.md. Emit candidate list:
ticker, direction, strategy, entry/stop/target, grade, status NEW or CARRY.

## Pre-market (08:30–09:25 ET) — re-affirmation gate

For each candidate, re-pull quotes + overnight news/earnings/futures, re-run skill.md:

- Gap beyond entry + 0.25× ATR buffer → INVALIDATED (no chase).
- Gap through stop → INVALIDATED.
- Earnings/event conflict, VWAP/MTF flip, RVOL regime break → DOWNGRADED or INVALIDATED.
- Still valid → CONFIRMED (alertable). New matches → NEW with "trigger if" levels.

Only CONFIRMED/NEW-with-trigger alerts fire. Everything else stays paper.

## Alert format (push: Telegram/Pushover/SMS; TradingView backup)

`[B] - NVDA SHORT - Possible upcoming Rejection - Entry 219.98 - SL 229.52 - Tgt - - opt: put debit spread` (RPS; OLD rows show strategy instead of state, e.g. `[B] - AAPL LONG - breakout (primary) - Entry 326.55 - SL 328.94 - Tgt 319.67 - opt: call debit spread`)

## Paper gate

Log every alert with outcome in R. Size up only after 2–4 weeks with live
expectancy ≥ 0 and pre-market gate enforced throughout.
