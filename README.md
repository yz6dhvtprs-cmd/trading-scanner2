# Trading Indicators System v1 (paper mode — do not trade off template data)

Approved plan implementation. Status: scaffold + rules complete, backtest validation still required before live capital.

## Layout

- `universe/` — top-50 methodology, fetch script, `universe_v1.csv` (TEMPLATE — refresh before any use)
- `strategies/` — 3 directional strategy specs (pullback, breakout, EMA-21 retest)
- `skill.md` — runtime scoring checklist (the saved skill from plan step 4.b)
- `scanner/` — nightly + pre-market routine and alert format
- `grading.md` — A+ / A / B+ / B rubric

## Run it (already set up on this machine)

```
cd /Users/vismaypatel/trading-indicators
source .venv/bin/activate
python universe/fetch_universe.py --out universe/universe_live.csv
```

Fresh setup elsewhere: Python 3.12 lives at `~/.python-dist` (standalone build,
no sudo needed); `.venv` was created from it with `pandas yfinance lxml`.
Live snapshot `universe/universe_live.csv` is dated 2026-09-04 (50 tickers, no META).

Then backtest (next step): `python backtest/run.py --universe universe/universe_live.csv --years 2`.

## Gate before live

1. Refresh universe (template file is NOT tradeable data).
2. 2-year walk-forward backtest, positive test-period expectancy net of costs.
3. 2–4 weeks paper alerts with pre-market gate enforced.

## Agents: start / stop (no tokens needed)

The 8 background agents (analyze, bootstrap, inbox, intraday, nightly,
premarket, saturday, screen) run as macOS LaunchAgents and reload at login.
Control them from a Terminal with:

```
cd /Users/vismaypatel/trading-indicators
./scanner/agents.sh status    # check what's loaded
./scanner/agents.sh stop      # stop everything
./scanner/agents.sh start     # start everything
./scanner/agents.sh restart   # stop + start
```

While stopped, interval scans and inbox polling pause; inbound SUBSCRIBE
texts queue and are processed on `start`. The handler only wraps
`launchctl load/unload` — it never touches credentials or GitHub.
