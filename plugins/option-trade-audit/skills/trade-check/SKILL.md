---
name: trade-check
description: Check one candidate option trade before it is taken. Runs a deterministic gate (policy vetoes plus five scored gates, each annotated with the user's own historical record for that kind of trade from trade-audit), completes the candidate from live data, adjudicates what a script cannot see, and returns TAKE / TAKE SMALLER / WAIT with a wake-up condition / NO. Use before recommending or placing any option trade.
---

# trade-check

The gate is a script; you are the judgment layer on top of it.

- **Script (`scripts/gate.py`)** does the bar maths (EMA/RSI/ATR, distance from
  EMA20, SPY regime, day-range position, earnings proximity), labels the trade
  the same way trade-audit labelled history, looks up the user's own record for
  each bucket, and applies the checks below.
- **You** fetch the live facts, read the chart and news, and make the final call.
  You may make the verdict **more** conservative than the gate's, never less.

Needs `pandas numpy yfinance`. Priors: `./trade-audit-data/trade_priors.json`
(or `TRADE_PRIORS`), written by the trade-audit skill. **Without it the history
checks are inactive: say so, and treat the result as policy checks only.**

## What the gate checks

**Policy vetoes** (risk rules, always on, configurable with `--cfg`):
no adding to a losing option position; at most 3 new positions a day
(`max_new_per_day`); stop for the day after 2 losses; no new position within
60 minutes of a loss (`revenge_min`); premium cap (`max_premium`, default
2,500); and a defined stop price on the underlying, on the correct side.

**History rules** (block only when the user's own record for that bucket is bad,
profit factor below 0.80 on at least 20 trades; otherwise printed as advisory):
0-1 DTE more than 1% out of the money; bearish with SPY above its 20 and 50 EMAs
and the name not stretched; entry on or just after an earnings report.

**Five scored gates** (3 of 5 needed): expiry, contract (delta 0.55-0.75),
trend (aligned with EMA20/50, or a pullback below EMA20 with RSI under 40 for a
long), trigger (with the day's move, in the top or bottom fifth of the day's
range), regime (VIX bucket and entry time not bad in the user's record).
Each prints the bucket's n / win rate / profit factor. A gate whose rule and
the user's history disagree is marked `CONFLICT`: adjudicate it in your answer.

## Steps
1. Gather: last, day open/high/low (`get_equity_quotes`), VIX, delta / spread /
   open interest of the actual contract (`get_option_chains` → `get_option_instruments`
   → `get_option_quotes`; use the broker's delta, never derive it), open option
   positions, and today's closed trades (new positions so far, losses, minutes
   since the last loss).
2. Run, from the skill's `scripts/` directory:
   `python3 gate.py --json '{"ticker":"AAPL","otype":"call","dte":45,"strike":250,"qty":2,"premium":1800,"delta":0.62,"stop_px":238,"live":{"last":258,"day_open":254,"day_high":258.4,"day_low":253.5,"vix":17.4,"new_today":1,"min_since_loss":180,"entry_hour":9.8,"adding_to_loser":false}}'`
   (`--as-json` for machine output). A missing field fails its gate; never guess it.
3. Adjudicate each `CONFLICT` and each failed gate in one clause: does the live
   picture change it?
4. Answer in this shape:
   ```
   TRADE: <ticker> <expiry> <strike><C/P> x<qty>  (<DTE> DTE, delta <d>, premium $<p>)
   VETO:   <rule>                      <- omit if none
   GATES:  expiry PASS | contract FAIL | trend PASS | trigger PASS | regime PASS  (4/5)
   STOP:   <underlying price that invalidates it>
   VERDICT: TAKE / TAKE SMALLER / WAIT FOR <condition> / NO
   NEAREST PASSING VERSION: <concrete alternative or "none">
   ```
   A veto means NO. For WAIT, name the price or date that makes it live. For NO,
   say what would change it, so the idea can go on a watch list instead of being
   argued again.

This skill never places orders. If the user wants a trade the gate rejects, say
which rule it breaks and leave the decision with them.
