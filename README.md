# Trading Indicators System v1 (paper mode — do not trade off template data)

Approved plan implementation. Status: scaffold + rules complete, backtest validation still required before live capital.

## Layout

- `universe/` — top-50 methodology, fetch script, `universe_v1.csv` (TEMPLATE — refresh before any use)
- `strategies/` — 3 directional strategy specs (pullback, breakout, EMA-21 retest)
- `skill.md` — runtime scoring checklist (the saved skill from plan step 4.b)
- `algos/` — signal + indicator logic (EMA, weekly box, screener, RPS, cache)
- `ops/` — orchestration launchd runs (scans, inbox, run_scan.sh, agents.sh)
- `data/` — caches, pools, state, paper-trade log (`signal_log.csv`)
- `logs/` — all run logs; `docs/` — all markdown (reports under `docs/reports/`)
`data/screener_pool.csv` — screener adds; `data/scan_pool.csv` — full 749 union w/ src column (see Monday screener)
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
python ops/scan.py --pool top50 --channels dry   # dry run, prints only
python ops/scan.py --no-rps                      # OLD signals only
```

RPS rows land in `data/signal_log.csv` (strategy RPS, grade B) and flow
through the premarket gate + intraday ENTERED/STOPPED tracking untouched.

Backtest it (tool stays open alongside live):

```
python algos/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS --score    # v2 two-step, next-open fills, tag [RPS] (RPS2 = same)
python algos/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS1 --score   # v1 legacy: bare pair agreement, tag [R3+R10]
python algos/backtest_analyzer.py --ticker AAPL --days 20 --algo OLD --score    # mainline algo.json strategies (not reversals)
RPS_WASH_LO=40 RPS_WASH_HI=60 python algos/backtest_analyzer.py --ticker AAPL --days 60 --algo RPS --score  # threshold sweep
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
(`data/ema_state.json`), over the 749-name master+screener union. Both batch and
live run a staged funnel — 60d RSI prefilter on the full pool, then the 1y
fetch plus full indicators on survivors only (same signals, ~3x faster).
`--simulate` replays the exact live rules over
history — forming-bar drop, 5-trading-day parks, 3-session dedupe — and
prints what would have paged vs muted with reasons. Both tables carry
`pslack`/`rslack` weakest-gate margins (ATRs / RSI points): near-zero rows
are margin calls that any data wobble can flip:

```
python algos/simple_scan.py --ticker NVDA --days 60
python algos/simple_scan.py --pool spy+qqq --days 60
python algos/simple_scan.py --pool spy50 --days 90 --target 0.20 --rsi-target 40
python algos/simple_scan.py --pool spy50 --days 60 --simulate
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
`data/{daily_cache,weekly_cache,hourly_cache}/` (gitignored) and
refreshed incrementally — the 15-min loop pays network once a day, and
the batch tools reuse the chunked downloader with serial retry. Check
health, backfill, or top up per timeframe:

```
python algos/daily_cache.py --tf 1d --status
python algos/daily_cache.py --tf 1w --bootstrap
python algos/daily_cache.py --tf 1h --refresh
python algos/daily_cache.py --tf 1h --bootstrap
```

EMA/RSI/ATR are recomputed from local bars (stateless; splits and
revisions can't corrupt it). Incremental indicator state was considered
and rejected — see PENDING.md.

## Consolidation detector (reusable, any ticker/timeframe)

A-range gate (box width in ATRs) + E-drift veto (slope/residual), with
B close-clustering reported as a confirm flag for the alert line.
Returns boundaries + run length. Manual chart-check harness:

```
python algos/consolidation.py --ticker NVDA --days 10
python algos/consolidation.py --ticker AAPL --ticker MSFT --days 20 --tf 1d,1w
python algos/consolidation.py --ticker NVDA --days 5 --tf 1h --n 4 --width 1.0 --slope 0.10 --resid 0.50 --b-pct 0.5
```

## Weekly anchor-box signal (Fridays 12:00 PT)

Anchor week sets the box; the next weeks must sit entirely inside it;
week n+3 scores live at noon (holiday fallback: last daily close).
Alert needs price above weekly EMA20 + MACD bull above 0; above-EMA10
names lead the text, EMA20-only names follow, no repeats. Same-box
refires suppress (one alert per grind, state in
`weekly_box_state.json`). Window span configurable (`--weeks 8`).

```
python algos/weekly_box.py --ticker DELL
python algos/weekly_box.py --on 2026-09-18 --on 2026-09-11 --no-refire
python algos/weekly_box.py --replay 200 --no-refire --ema10-only
python algos/weekly_box.py --replay 200 --weeks 8 --no-refire
python algos/weekly_box.py --scan --no-refire --channels dry   # Friday live path, dry
```

The Friday live path scans the master+screener union via
`screener.scan_pool()` (weeklies are derived from 2y dailies, so thin
Friday tapes like PFG's can't print a wrong box low).

## Monday screener (06:00 PT)

SPY+QQQ (517) is master and is never touched here. A separate adds list
is maintained from S&P 500 + Nasdaq-100 + Nasdaq-screener names ≥ $5B
(the screener's own $5B floor stands in for Russell-1000 breadth —
iShares blocks scripted R1000 pulls). Gates, calibrated to land
400–500 qualifiers: market cap 5B–1000B, RVOL20 (Friday volume /
20-day mean) in 1–10, common equity only (name filter + quoteType;
preferreds/notes/warrants/ETFs never page). Each Monday the rotation
adds new passers and drops tracked names that fail the week
(`--status` shows the add/remove); spy+qqq membership never enters the
equation. Live pool: 517 master + 232 adds = 749 scanned by the
intraday EMA stage and the Friday weekly-box pass.

```
python algos/screener.py --calibrate  # RVOL window table (report)
python algos/screener.py --run        # weekly add/remove update
python algos/screener.py --status     # pool counts (no network)
```

Files: `data/screener_pool.csv` (the 232 adds),
`data/screener_state.json` (add-week stamps + overlap),
`data/scan_pool.csv` (full 749 snapshot with master/screener src
column — what to open to see every name), plus a dated candidate dump
under `universe/`. Scheduled via `run_scan.sh screener` on the Monday
06:00 PT plist (load once with `launchctl load`
`~/Library/LaunchAgents/com.trade.screener.plist`).

## Tests (all offline)

```
python algos/test_weekly_box.py      # 16: box, gates, fallback, refire, text shape, union
python algos/test_consolidation.py   # 8: gates, veto, B divergence, run grouping, A-only
python algos/test_daily_cache.py     # 13: rotation, forming bars, resync, timeframes, tz, derived weeklies
python algos/test_screener.py        # 5: rvol, cap band, symbols, name filter, rotation
python algos/test_simple_scan.py     # 24: gates, pages, replay
python algos/test_intraday.py        # 12: batch text, live-touch branches
python algos/test_market_calendar.py # 7
python algos/test_inbox.py           # inbox replay
```

## Gate before live

1. Refresh universe (template file is NOT tradeable data).
2. 2-year walk-forward backtest, positive test-period expectancy net of costs.
3. 2–4 weeks paper alerts with pre-market gate enforced.

## Agents: start / stop (no tokens needed)

The background agents (analyze, bootstrap, inbox, intraday, nightly,
premarket, screen, plus screener and weekly) run as macOS LaunchAgents
and reload at login. (The Saturday review text was removed — no
Saturday agent.)
Control them from a Terminal with:

```
cd ~/trading-indicators
./ops/agents.sh status    # check what's loaded
./ops/agents.sh stop      # stop everything
./ops/agents.sh start     # start everything
./ops/agents.sh restart   # stop + start
```

While stopped, interval scans and inbox polling pause; inbound SUBSCRIBE
texts queue and are processed on `start`. The handler only wraps
`launchctl load/unload` — it never touches credentials or GitHub.
