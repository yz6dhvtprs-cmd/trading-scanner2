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
python scanner/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS --score    # v2 two-step, next-open fills, tag [RPS] (RPS2 = same)
python scanner/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS1 --score   # v1 legacy: bare pair agreement, tag [R3+R10]
python scanner/backtest_analyzer.py --ticker AAPL --days 20 --algo OLD --score    # mainline algo.json strategies (not reversals)
RPS_WASH_LO=40 RPS_WASH_HI=60 python scanner/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS --score  # threshold sweep
```

## Simple EMA scanner (pullback to value)

Setup: Close below EMA 8/13/21 while within 1xATR of EMA50, RSI14 from
`--rsi-target` to `--rsi-max` (default 40 to 45; `--no-rsi` disables),
plus as-of weekly RSI14 above `--wrsi-min` (default 55: daily pullback
inside a weekly uptrend; weekly value uses completed Friday weeks plus the
bar's own close as the forming week, same as a live chart). The previous
daily close and today's open must both have printed strictly above EMA50
(first touch only, no gap-unders).
Backtest per fresh signal (entry = signal-day close): days to +20%, max %,
current %, plus a `page` column (Y = live would page it, - = muted by the
3-trading-day dedupe). Live: intraday runs it every 15m over spy+qqq
with 3-trading-day alert dedupe and a 5-trading-day / recovery ignore list
(`scanner/ema_state.json`), over the full spy+qqq union. Both batch and
live run a staged funnel — 60d RSI prefilter on the full pool, then the 1y
fetch plus full indicators on survivors only (same signals, ~3x faster).
`--simulate` replays the exact live rules over
history — forming-bar drop, 5-trading-day parks, 3-session dedupe — and
prints what would have paged vs muted with reasons. Both tables carry
`pslack`/`rslack` weakest-gate margins (ATRs / RSI points): near-zero rows
are margin calls that any data wobble can flip:

```
python scanner/simple_scan.py --ticker NVDA --days 60
python scanner/simple_scan.py --pool spy+qqq --days 60
python scanner/simple_scan.py --pool spy50 --days 90 --target 0.20 --rsi-target 40
python scanner/simple_scan.py --pool spy50 --days 60 --simulate
```

Pools: sp100 (top-100 S&P weights, proxy), sp500, spy, qqq, spy50, qqq50
(weight-ranked, SlickCharts 09-2026, baked into `universe/*_w.csv`);
`--pool` also takes `+` unions like `spy+qqq` (full 517-name union, live default).

Live intraday (every 15m, 06:30–13:00 PT) runs the same gates plus a
live-touch pass: names in the daily RSI band are re-checked against the
live print, so a touch of EMA50 at 10am pages at 10:15 instead of the
next morning (daily and live pages share one 3-day dedupe clock). Each
run sends at most one batched text:

```
D_RSI 40-45, WRSI>55, EMA50
XYZ
PQR
```

## Local bar cache (1d/1w/1h, no re-downloads)

Completed bars are stored per ticker under
`scanner/{daily_cache,weekly_cache,hourly_cache}/` (gitignored) and
refreshed incrementally — the 15-min loop pays network once a day, and
the batch tools reuse the chunked downloader with serial retry. Check
health, backfill, or top up per timeframe:

```
python scanner/daily_cache.py --tf 1d --status
python scanner/daily_cache.py --tf 1w --bootstrap
python scanner/daily_cache.py --tf 1h --refresh
python scanner/daily_cache.py --tf 1h --bootstrap
```

EMA/RSI/ATR are recomputed from local bars (stateless; splits and
revisions can't corrupt it). Incremental indicator state was considered
and rejected — see PENDING.md.

## Consolidation detector (reusable, any ticker/timeframe)

A-range gate (box width in ATRs) + E-drift veto (slope/residual), with
B close-clustering reported as a confirm flag for the alert line.
Returns boundaries + run length. Manual chart-check harness:

```
python scanner/consolidation.py --ticker NVDA --days 10
python scanner/consolidation.py --ticker AAPL --ticker MSFT --days 20 --tf 1d,1w
python scanner/consolidation.py --ticker NVDA --days 5 --tf 1h --n 4 --width 1.0 --slope 0.10 --resid 0.50 --b-pct 0.5
```

## Weekly anchor-box signal (Fridays 12:00 PT)

Anchor week sets the box; the next weeks must sit entirely inside it;
week n+3 scores live at noon (holiday fallback: last daily close).
Alert needs price above weekly EMA20 + MACD bull above 0; above-EMA10
names lead the text, EMA20-only names follow, no repeats. Same-box
refires suppress (one alert per grind, state in
`weekly_box_state.json`). Window span configurable (`--weeks 8`).

```
python scanner/weekly_box.py --ticker DELL
python scanner/weekly_box.py --on 2026-09-18 --on 2026-09-11 --no-refire
python scanner/weekly_box.py --replay 200 --no-refire --ema10-only
python scanner/weekly_box.py --replay 200 --weeks 8 --no-refire
python scanner/weekly_box.py --scan --no-refire --channels dry   # Friday live path, dry
```

## Tests (all offline)

```
python scanner/test_weekly_box.py      # 14: box, gates, fallback, refire, text shape
python scanner/test_consolidation.py   # 8: gates, veto, B divergence, run grouping, A-only
python scanner/test_daily_cache.py     # 11: rotation, forming bars, resync, timeframes, tz
python scanner/test_simple_scan.py     # 24: gates, pages, replay
python scanner/test_intraday.py        # 12: batch text, live-touch branches
python scanner/test_market_calendar.py # 7
python scanner/test_inbox.py           # inbox replay
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
