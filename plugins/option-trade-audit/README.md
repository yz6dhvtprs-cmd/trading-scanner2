# option-trade-audit

Two Claude Code skills that work together:

- **trade-audit** rebuilds your option trades from your broker, joins each entry to
  its technical context, and finds which setups and habits made or lost money.
- **trade-check** scores a new trade against that record (and a few hard risk
  rules) before you take it, and returns TAKE / TAKE SMALLER / WAIT / NO.

Nothing about anyone's trades ships in this repo. Your order history, P&L and
priors are written to `./trade-audit-data/` on your machine and are git-ignored.

## Install

```
/plugin marketplace add yz6dhvtprs-cmd/trading-scanner2
/plugin install option-trade-audit@trading-scanner2
pip install -r plugins/option-trade-audit/requirements.txt   # pandas numpy yfinance
```

Built for Robinhood's MCP (`get_option_orders`, `get_realized_pnl`, quotes, option
chains). Any broker MCP with equivalent tools works; only the collection step in
`skills/trade-audit/SKILL.md` is broker-specific.

## Use

1. "Audit my option trades from the last three years." Claude pages your orders,
   runs the analysis (about a minute), checks it against the broker's realized
   P&L and tells you where you win and lose.
2. "Check this trade: AAPL 45-day call, delta 0.62, stop 238." Claude gathers live
   data, runs the gate and gives a verdict with every rule it breaks.

Run the scripts by hand if you prefer: see the two `SKILL.md` files.

## How the gate stays honest

History rules only block when *your* record for that kind of trade is bad
(profit factor under 0.80 on at least 20 trades); otherwise they are printed as
advisory. Risk rules (no averaging down, daily cap, revenge timer, premium cap,
stop required) are policy, always on, and configurable with `--cfg`. An AI session
reviewing the gate output may lower its verdict, never raise it. The thresholds
move when you re-run the audit; no code changes.

## Tests

```
python3 tests/test_gate.py      # 13 offline checks
```

Not financial advice. Past results do not predict future ones.
