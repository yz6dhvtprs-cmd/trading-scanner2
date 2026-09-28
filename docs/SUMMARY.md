# Trading Indicators System — summary

Directional stock + options signal system on the S&P 500 (ex-META), validated
by backtest, delivered over iMessage, self-improving every Saturday.

## Finalized algorithm v1.0 (algo.json, 2026-09-04)

- PRIMARY (A): breakout-long, ADX≥20, RVOL≥2, chandelier trail 3.0×ATR.
  Train +0.72R (n=124) → test +0.59R (n=50), win rate ~30%. Runners pay.
- SECONDARY (B+): pullback-long, ADX≥20, trail 2.5×ATR (+0.28/+0.25R).
- PAUSED: all shorts, all retest variants. No sideways strategies by design.

## Automation (this Mac, PT)

- Nightly scan Mon–Fri 2:05pm, pre-market gate Mon–Fri 6:05am,
  Saturday review 10:15am, login ping after reboots — all via iMessage.
- Agents: `~/Library/LaunchAgents/com.trade.*.plist` → `scanner/run_scan.sh`.

## Feedback loop

Every alert → `scanner/signal_log.csv` → Saturday `review/saturday.py`
compares live last-20 expectancy vs baseline, demotes failures, checks regime
(SPY/VIX) and universe age. Grade changes apply; param changes need approval.

## Key files

`algo.json` `skill.md` `strategies/SPECS.md` `grading.md` `INDICATORS.md`
`backtest/{run,combos}.py` `backtest/NOTES.md` `scanner/{scan,notify,rehearse}.py`
`scanner/ROUTINE.md` `review/LOOP.md` `ALERTS.md` `VERSIONING.md`
Universe snapshots + result CSVs are versioned; `.venv`, cache, logs are not.

## Status: PAPER ONLY

iMessage delivery verified 2026-09-04. Real capital only after 20+ closed
paper trades hold live expectancy ≥ 0 per the Saturday loop.
