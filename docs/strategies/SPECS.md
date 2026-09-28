# Strategy specs v1 — directional only, no sideways trades

Common: ATR(14) stops. Risk unit R = entry − stop. Reject setup if realistic
target < rubric minimum for its grade. Shorts mirror longs.

## 1. Trend pullback (first pullback)

- Trend: EMA9 > EMA21 > EMA50 (daily + traded timeframe agree).
- Pullback: price touches EMA9/21 zone, RSI(14) 38–55, no close below EMA50.
- Trigger: bullish engulfing / hammer on traded timeframe, close back above EMA9.
- Stop: below trigger-candle low or 1.5× ATR, whichever is wider.
- Target: recent swing high; scale 1/2 at 2R, trail rest on 21EMA/ATR.
- Timeframes: 15m/1h intraday, daily swing.
- Skip: RSI < 30 (falling knife), earnings within 3 sessions.

## 2. Breakout (20-day high/low + volume)

- Level: highest high of prior 20 sessions (lowest low for shorts).
- Confirm: close through level + relative volume ≥ 2× 50-day average + RSI 50–67.
- Enter: break candle close or next-open within 0.25× ATR of level (no chasing beyond).
- Stop: back inside the range (other side of level) or 2× ATR.
- Target: measured move (range height) projected; scale 1/2 at 2R.
- Skip: break on < 1.5× RVOL, or into earnings within 3 sessions.

## 3. EMA-21 break-and-retest

- Setup: price crosses EMA21 with conviction (body > 0.5× ATR through the line),
  then retests EMA21 and holds on a closing basis.
- MTF: higher timeframe trend agrees (hourly for 15m entries, daily for 1h entries).
- Trigger: rejection wick / inside-bar break in original direction at the retest.
- Stop: beyond retest extreme or 1.5× ATR. Target: prior swing / 2R minimum geometry.
- Skip: chop (3+ EMA21 crosses in 10 bars), flat EMA21 slope.
