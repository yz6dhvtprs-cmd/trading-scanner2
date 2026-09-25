# Pending / follow-ups (as of Fri 2026-09-25)

## To build
- **Friday weekly signal**: consolidation above weekly EMA20/EMA10, min 4
  candles, runs Fri 12:00 PT (1h before close); Friday-off fallback to
  prev close / prev half-day close. Detector (`scanner/consolidation.py`)
  is ready; the EMA20/10 gate + scheduler slot are not.
- **Premarket pool**: `scan.py` premarket still scans 50 tickers while
  intraday runs the 517-name spy+qqq union. Decide whether to switch it.
- **Hourly refresh cadence**: 1h cache has no scheduled refresh — it
  fills on demand (consolidation CLI, live-touch 1m tape is separate and
  uncached). Add a slot if anything starts reading hourly bars on a loop.

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
