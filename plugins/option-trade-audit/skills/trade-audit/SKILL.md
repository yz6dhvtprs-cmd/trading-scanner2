---
name: trade-audit
description: Audit the user's own option trade history. Pages their orders from the broker's MCP, rebuilds every position, joins each entry to its technical context (EMA/RSI/ATR, SPY regime, VIX, time of day, earnings, sequence and sizing habits), and reports which setups and habits made or lost money. Writes a private report and a priors file that the trade-check skill uses. Use when the user asks to analyze, review or learn from their past option trades.
---

# trade-audit

Turns "how have my option trades actually gone?" into evidence: win rate and
profit factor per bucket (days to expiry, moneyness, trend, RSI, VIX, time of
day, averaging down, revenge re-entries, size, earnings), reconciled against the
broker's own realized P&L. Output stays on the user's machine.

Needs: Python 3 with `pandas numpy yfinance` (`pip install -r requirements.txt`
at the plugin root) and a broker MCP that exposes option orders
(built for Robinhood: `get_accounts`, `get_option_orders`, `get_realized_pnl`).

All files go to `./trade-audit-data/` (override: `TRADE_AUDIT_DIR`). **It holds
the user's orders and P&L. Keep it out of git, and never paste its dollar
figures into anything shared.**

`S` below is this skill's `scripts/` directory.

## 1. Collect orders (the agent step; the MCP cannot be called from Python)
1. `get_accounts`. Give each account a short label (`main`, `ira`, `roth`):
   labels become directory names, never use account numbers.
2. For each account call `get_option_orders(account_number, created_at_gte=<3 years ago>)`.
   Results are large and spill to a file; ingest each one and follow the cursor:
   `python3 $S/ingest_orders.py <label> <result_file>`
   It prints the date range, filled count and the next cursor. Pass that cursor
   to the next call. Repeat until it prints `next=NONE`. Pages overlap safely.
3. A very short last page may come back inline instead of in a file. Save it to a
   file and ingest it the same way.
4. Control totals for later: `get_realized_pnl(account, asset_classes=["option"],
   start_date=..., end_date=...)`.

## 2. Run the analysis
`bash $S/run.sh <path-to-python>` (about a minute). It runs, in order:
`reconstruct.py` (fills → FIFO lots → flat-to-flat positions; expiries with no
close are booked at zero), `fetch_px.py` (daily bars, VIX, hourly bars for the
most-traded names, from yfinance), `features.py` / `features_h1.py` (technical
and behavioural context at each entry), `earnings_ctx.py`, `analysis.py`,
`report.py` (full `ANALYSIS.md` and a dollar-free `LESSONS_TABLES.md`), and
`priors.py` (writes `trade_priors.json` for trade-check).

## 3. Validate before concluding
Compare reconstructed monthly P&L with `get_realized_pnl`. Small gaps are
fees/rounding. A large gap in one month points to exercise, assignment or a
corporate action the order stream does not show; say so rather than hiding it.
Entry price of the underlying is a proxy (hourly interpolation where hourly bars
exist, else the day's open or close); say that too.

## 4. Read and report
Read `ANALYSIS.md`. Lead with the account's overall win rate and profit factor,
then rank the biggest leaks and the buckets that paid, **by profit factor with
the trade count beside it**; ignore buckets under ~20 trades. Check that each
pattern has the same sign across accounts and years before calling it real. Look
especially at: averaging down vs adding above entry, bearish trades in an
uptrend, trades after a loss (minutes since the previous exit), the nth trade of
the day, 0-1 DTE out of the money, size buckets, hold time, earnings proximity.
Then draft rules in plain language. Selection on outcomes flatters what-if
filters, so give signs and rankings, not promised dollars.

Offer a sanitized write-up (`LESSONS_TABLES.md` plus prose with no dollar amounts
or account details) if the user wants to keep or share conclusions.

## 5. Hand off
`trade_priors.json` is what `trade-check` reads. Re-run steps 1-2 whenever
the user wants it refreshed; the gate's thresholds move with the data.
