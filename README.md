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
cd ~/trading-indicators
source .venv/bin/activate
python universe/fetch_universe.py --out universe/universe_live.csv
```

Fresh setup elsewhere: Python 3.12 lives at `~/.python-dist` (standalone build,
no sudo needed); `.venv` was created from it with `pandas yfinance lxml`.
Live snapshot `universe/universe_live.csv` is dated 2026-09-04 (50 tickers, no META).

Then backtest (next step): `python backtest/run.py --universe universe/universe_live.csv --years 2`.

## RPS reversal algo (live + backtested)

RPS = R3 trendline-break + R10 volume-oscillator divergence (tournament pair),
plus a two-step intraday filter: 15m-RSI washout (sub-30 for longs, 70+ for
shorts, last ~session), then 30m-RSI turning the reversal way (else 1H RSI).
Entries fill at the trendline cross (never the chase-close), stops sit
0.5–2.0xATR from entry, breaks older than 2xATR past the line are skipped as
stale. Measured 60d over 13 QQQ/SPY top-10 tickers: 9–3 decided (75%,
+0.50R expectancy per signal).

Live (on by default in the nightly scan; same code as the backtest):

```
python scanner/scan.py --pool top50 --channels dry   # dry run, prints only
python scanner/scan.py --no-rps                      # OLD signals only
```

RPS rows land in `scanner/signal_log.csv` (strategy RPS, grade B) and flow
through the premarket gate + intraday ENTERED/STOPPED tracking untouched.

Backtest it (tool stays open alongside live):

```
python scanner/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS --score    # v2 two-step, next-open fills, tag [RPS]
python scanner/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS1 --score   # v1 legacy: bare pair agreement, tag [R3+R10]
RPS_WASH_LO=40 RPS_WASH_HI=60 python scanner/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS --score  # threshold sweep
```

## Gate before live

1. Refresh universe (template file is NOT tradeable data).
2. 2-year walk-forward backtest, positive test-period expectancy net of costs.
3. 2–4 weeks paper alerts with pre-market gate enforced.

## Agents: start / stop (no tokens needed)

The 8 background agents (analyze, bootstrap, inbox, intraday, nightly,
premarket, saturday, screen) run as macOS LaunchAgents and reload at login.
Control them from a Terminal with:

```
cd ~/trading-indicators
./scanner/agents.sh status    # check what's loaded
./scanner/agents.sh stop      # stop everything
./scanner/agents.sh start     # start everything
./scanner/agents.sh restart   # stop + start
```

While stopped, interval scans and inbox polling pause; inbound SUBSCRIBE
texts queue and are processed on `start`. The handler only wraps
`launchctl load/unload` — it never touches credentials or GitHub.
