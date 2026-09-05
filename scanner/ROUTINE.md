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

`[A] AAPL LONG pullback 15m | entry 242.10 stop 240.90 tgt 246.90 (4.0R) | CONFIRMED premarket | opt: call debit 240/250 45DTE max-loss $X`

## Paper gate

Log every alert with outcome in R. Size up only after 2–4 weeks with live
expectancy ≥ 0 and pre-market gate enforced throughout.
