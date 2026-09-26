# Pending / follow-ups (as of Sat 2026-09-26)

## Shipped since last update
- **Friday weekly anchor-box signal** (`scanner/weekly_box.py`): anchor
  week + inside weeks, EMA10-first text, MACD>0, no-refire, Friday 12:00
  PT plist; weeklies derived from 2y dailies; scans the 749 union.
- **Monday screener** (`scanner/screener.py`): cap 5B–1T + RVOL20 1–10 +
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
