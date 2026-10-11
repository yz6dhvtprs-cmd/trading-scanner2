# The trades, in plain words

What kind of technical trades this system looks for, why, and how they get
taken. Details and evidence live in `FUNNEL.md` and `EXECUTION.md`.

## The idea in one paragraph

We don't predict. We wait for a stock to **coil** (go quiet in a tight range,
volatility shrinking) inside an **uptrend**, then buy when it **breaks out of
the coil in the trend's direction**. Every trade has a stop decided *before*
entry, a target of **2× the risk**, and a time limit. We win about half the
time, but winners are twice the size of losers, so it adds up.

## Words used below

- **EMA (exponential moving average)** — the average price of the last N days,
  weighted to recent days. *Above the EMA* = buyers in control; *below* =
  sellers. EMA21 ≈ one month, EMA50 ≈ two-and-a-half months.
- **SMA200** — 200-day average. Price above it = long-term uptrend ("bull
  regime"). Below = downtrend.
- **RSI** — 0–100 gauge of how hard a stock has been pushed. Above 70 =
  stretched up, below 30 = washed out, 40–55 = a healthy breather in a trend.
- **ATR** — the stock's normal daily range in dollars. Used to size stops so a
  noisy stock gets a wider stop.
- **R** — the risk on one trade (entry minus stop). "+2R" = made twice what
  we risked.

## Step 1 — the pool (~200 names)

The most-traded big stocks (top 200 by dollar volume, price ≥ $15). Options on
them are liquid, so we can actually get in and out.

## Step 2 — the watch list ("something is coiling")

A stock goes on watch when one of these shows up:

| flag | plain meaning |
|---|---|
| **squeeze** | the price swings have gotten unusually small for 5+ days (Bollinger Bands inside Keltner Channels) — energy is building |
| **tight base** | 20 days in a range under 8% wide, sitting near its high (or low) |
| **pullback zone** | in an uptrend (EMA21 above EMA50, price above SMA200), price has dipped back to the EMA21 with RSI 38–55 — a rest, not a breakdown |
| **spring / failed break** | it dipped below its 20-day low and snapped right back (or poked above the high and failed) |
| bonus | strong vs other stocks over 3 months; volume drying up |

## Step 3 — the setups we actually trade

| setup | what has to happen | stop | exit | status |
|---|---|---|---|---|
| **SQ — squeeze release** (the main one) | squeeze lasted ≥ 6 days, then the first day price closes *outside* the tight range, **upward**, with price **above EMA21**, momentum positive | the bottom of the squeeze range | +2R, or after 20 trading days | **traded** — 49% win, +0.19R per trade, held up in both test periods |
| **PB — trend pullback** | EMA9 > EMA21 > EMA50, above SMA200, trend strength (ADX ≥ 20), price dips to EMA9/21, **RSI 38–55**, then closes back up | that day's low | trailing stop 2.5×ATR, max 40 days | texted; options not auto-traded (stop too tight for options) |
| **BO — 20-day breakout** | closes above its 20-day high on **2× normal volume**, RSI 50–67, above SMA200 | just under the breakout level | trailing stop 3×ATR | texted; weak recently |
| **RC — undercut & reclaim** | broke the 20-day low then closed back above it within 3 days | the low it made | +2R in 15 days | paused (too frequent, too little edge) |

Only **long (buy) setups in a bull regime (above SMA200)** are taken with real
options. Shorts are paper-only — they haven't made money in this market.

Other alerts the system also sends (separate from the funnel):
- **Weekly box** — a stock spends 3 weeks inside the range of one week,
  above its weekly EMA20 with MACD positive. Best when price sits in the top of
  the box, the box is tight, and volume is drying up. Buy near the top, or on
  the breakout above it.
- **EMA50 pullback scan** — in a weekly uptrend (weekly RSI > 55), price
  touches the daily EMA50 with daily RSI 40–45.

## Step 4 — how the trade is taken

- **Entry:** at the next morning's open, only if it didn't gap more than 1 ATR
  away. We tested waiting for a cheaper pullback — it lost money (we missed
  the best moves).
- **Stop:** fixed at entry. Never moved to breakeven, never trailed, no taking
  half off early — all of those were tested and did worse.
- **Exit:** at +2R, at the stop, or after 20 trading days, whichever first.

## Step 5 — the option we buy

- **One call option** (single leg), about **2 months out** (monthly expiry ≥ 49
  days) so time decay doesn't eat the trade.
- **Strike:** delta ~0.65 (a bit in-the-money — moves ~65¢ per $1 in the
  stock), picked from Robinhood's live quotes; the most-traded strike wins.
- **Skip** if the option is illiquid (bid/ask too wide) or unusually expensive
  versus the stock's normal volatility.
- **The stock price decides the exit**, not the option price. A day-only
  stop order sits at the broker as backup, re-placed every morning.
- **Expect ~42% winners on the options** (a bit below the stock's 49%, because
  slow winners lose to time decay).

## Sizing

Risk a small fixed slice per trade (5% up to $250 while the account is small,
shrinking to 2% as it grows). Below $2,500 it only **paper-trades** to build a
record. More capital later = same rules, more contracts.
