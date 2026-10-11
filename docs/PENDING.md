# Pending / follow-ups (as of Sun 2026-10-11)

## Shipped 2026-10-11 (options execution layer, shadow)
- **`docs/strategies/EXECUTION.md`** — the strategy for taking funnel
  trades as single-leg options via the Robinhood MCP, synthesized from a
  three-design panel + two judges over four verified studies (SQ exit
  sweep, entry mechanics, earnings veto, IV/chain liquidity). Key calls:
  SQ exits stay 2R/20 fixed; next-open entries (every pullback limit lost
  on TEST); no earnings veto; stock stop governs; resting GTC `stop_limit`;
  option win rate ≈ 42%; shadow-only below $2,500.
- **`algos/execution.py`** + 13 tests; `.claude/skills/fnl-exec/SKILL.md`
  (the 15-minute executor runbook); premarket ENTRY intents, loop EXIT
  intents + heartbeat alarm; Saturday option-book block; `data/exec/`.
- **`algos/earnings.py`** (+9 tests, 200-name cache) from the study.
- **Calendar fixes**: Good Friday was wrong every year; `pt_now()` was a
  fixed UTC-7 (would shift the session an hour after 2026-11-01). FNL
  paper time stop aligned to the backtest's bar-20 close.

## To verify with one contract (S1, after shadow)
- Whether a take-profit limit can co-rest next to the day `stop_market`;
  the stop's trigger reference (mark/last/bid); whether `get_accounts`
  exposes a day-trade counter.
- Review-alert allowlist starts empty: collect codes in shadow.
- The scheduled headless session (`com.trade.fnlexec`, 14 slots/day):
  watch `logs/fnlexec-<date>.log` for usage-limit refusals; a missed
  morning slot leaves open positions without a day stop until the next run.

## User decisions taken 2026-10-11 (in EXECUTION.md)
- Resting stop = day-only `stop_market`, re-placed every morning (nothing
  rests overnight). Bootstrap sizing min(5% E, $250) stays until the user
  asks for flat. `regime.bull_only` on. `earnings.entry_min_sessions` 0.
- `data/exec/config.json` (gitignored, local only): agentic account and
  `notify_to` set by the user; `armed` false.

## Shipped 2026-10-10 (swing funnel)
- **`algos/funnel.py`** — pool (top 200 by $vol) → watch flags → four
  setup families (PB/BO verbatim from algo.json, SQ squeeze release,
  RC undercut reclaim) → next-open fills → trail/target/time
  management. Spec + evidence: `docs/strategies/FUNNEL.md`.
- **Backtest** (`--backtest`, 5,299 trades, TRAIN/TEST): SQ long B+
  (49% win, +0.19R, PF 1.41, both halves); RC paused (C); BO long
  negative in this window (kept B+, Saturday arbitrates).
- **Wiring**: premarket slot runs the scan (refreshes the daily cache
  itself); intraday stage 1e books fills (`FNL fills:` text) and the
  tracker manages `FNL-*` rows (`FNL paper:` text); Saturday baselines.
- First live premarket run: Mon 2026-10-12 06:05 PT.

## To verify by hand (2026-10-10)
- **Watch-pool size**: 62/200 on the first dry scan (score ≥ 1.5). If
  the text feels busy, raise `WATCH_MIN` to 2.0 (25 names).
- **SQ exits**: +2R / 20 bars were set a priori; sweep 1.5R / 2.5R /
  trail with TRAIN locked before changing.
- **Earnings veto**: not implemented for the funnel (SPECS.md asks for
  one); needs an offline calendar source.

## Shipped 2026-10-09 (weekly-box refinement)
- **Tiers + grades on the Friday text** (`algos/weekly_box.py`): BUY
  zone (upper 40% of the box, EMA10 rising) graded A/B+/B (box ≤ 1.3
  weekly ATRs, inside-week volume drying), `*` for a rising MACD
  histogram; watch and EMA20-only sections kept as watch lists.
  Evidence: no-lookahead replay of 1,418 paged alerts
  (`research/_cache/wbox_replay.csv`, gitignored).
- **WBOX paper rows** in `signal_log.csv` for BUY names (stop box low,
  target +1 box height, 8-week time stop) so Saturday grades them;
  replay baselines in `ops/saturday.py`; a C (paused) override stops the
  booking without hiding names.
- **Breakout watch** (`data/wbox_watch.json`, 4 weeks, all tiers) in the
  intraday loop: first print over the box top on paced RVOL ≥ 1.5 and
  ≤ 3% above it → one text + a WBOX-BO paper row (stop box mid).
- **Replay convention fix**: `--replay`/`--on` score the eval week as
  the forming week (live mirror); 10/02 fidelity 15/17 vs 7/17 before.
- **intraday.py crash fix**: free-text columns read as object dtype
  (`read_log`); the stop branch raised TypeError every run 10/07–10/09.

## To verify by hand (2026-10-09)
- **Seeded refire boxes**: `weekly_box_state.json` entries stamped
  2026-09-11/18 came from the old `--on --seed` convention (3-week
  pattern). They only suppress overlapping boxes; leave them unless a
  name you expect stays silent.
- **Saturday review** has had no plist since 5bbe24b: run
  `./ops/run_scan.sh saturday` by hand (or reload a plist) or WBOX rows
  never get graded.
- **Inside-week volume**: the replay averaged three full weeks against
  the anchor; live has two completed ones. Re-check the A-grade lift
  once 20+ WBOX rows have closed.

## Shipped since last update (2026-09-26)
- **Friday weekly anchor-box signal** (`algos/weekly_box.py`): anchor
  week + inside weeks, EMA10-first text, MACD>0, no-refire, Friday 12:00
  PT plist; weeklies derived from 2y dailies; scans the 749 union.
- **Monday screener** (`algos/screener.py`): cap 5B–1T + RVOL20 1–10 +
  common-equity, weekly add/remove rotation; 232 adds, union 749;
  spy+qqq master untouched.

## To build
- **Load the two plists**: `com.trade.screener` (Mon 06:00 PT) and the
  Friday weekly-box one are created but need a manual `launchctl load`
  before either schedule fires.
- **Premarket pool**: `scan.py` premarket still scans 50 tickers while
  intraday + Friday box run the 749 union. Decide whether to switch it.
- **Hourly refresh cadence**: 1h cache has no scheduled refresh — it
  fills on demand (consolidation CLI, live-touch 1m tape is separate and
  uncached). Add a slot if anything starts reading hourly bars on a loop.

## Open questions
- **ADRs**: currently excluded via the equity/name filter. Keep or allow?
- **RPS widening**: RPS still runs the old pool; decide whether it joins
  the 749 union like EMA + weekly box.
- **Text volume**: batched format is one text/run; watch subscriber
  tolerance as live-touch + 232 extra names add afternoon pages.

## To verify by hand
- **Consolidation knobs**: check CLI boxes against real charts
  (`--ticker X --days 10`); tune `--width/--slope/--resid/--b-pct`.
- **SMS exact-STOP fix**: confirm a message merely containing "stop" no
  longer unsubscribes; only the exact string does.
- **Text volume**: batched format is one text/run; watch subscriber
  tolerance as live-touch adds afternoon pages.

## Known divergences (by design)
- **Backtest vs live-touch**: `live_sim` replays daily bars only, so
  intraday touch pages never appear in `--simulate` output. If touch
  timing starts driving P&L, the replay needs 1h bars.
- **Live prints lag ~15 min** (Yahoo). The 1-ATR touch band absorbs it;
  not for HFT.

## Considered and rejected
- **Incremental EMA/RSI state** (store yesterday's EMA, update per bar):
  mathematically trivial (EMA is O(1) by construction) but adds a second
  source of truth that rots on splits/revisions. Recompute-from-local
  (~10s pool-wide) stays until a slot needs <2s.
- **Single-file cache (parquet/HDF5)**: per-ticker CSVs won on
  debuggability; 44MB hourly is fine. Revisit past ~500MB.
