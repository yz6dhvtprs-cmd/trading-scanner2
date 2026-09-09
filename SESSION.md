# Session log — trading-scanner build (2026-09-04 → 2026-09-08)

## What was built

- **Universe**: S&P 500 ex-META (`universe/universe_sp500.csv`, top-50 live slice kept).
- **Algo v1.0** (`algo.json`): PRIMARY breakout-long ADX≥20 RVOL≥2 trail-3.0×ATR
  (grade A; 10y: train +0.16–0.33R / test +0.52–0.64R); SECONDARY pullback-long
  ADX≥20 trail-2.5 (B+); shorts + retest PAUSED (negative even in bear tapes).
- **Indicators**: EMA stack 8/21/34/50/100/200, SMA 50/100/200, RSI zones +
  range-shift, ADX, MACD (rejected as gate), hidden divergence (boost flag),
  Fib 38.2/50/61.8 + extensions, candle patterns, weekly+1h+15m votes.
- **Two-tier flow**: Stage-0 9-MA alignment watchlist (25 max, confidence kickout,
  top-10 text) → Stage-1 signals with M/R/D/X confirms → pre-market gap gate.
- **Alerts**: change-only (approach/entered/stopped/invalidated/stand-down,
  delta watchlist/top-10). Recipients: you (iMessage) + Satya (SMS relay).
- **Inbox commands**: SUBSCRIBE / PAUSE / RESUME / UNSUBSCRIBE (5-min poll).
- **Saturday loop**: live-vs-baseline expectancy, auto-demotions, regime check.
- **Infra**: Python 3.12 + venv in project; git via dulwich (Apple git broken);
  GitHub `trading-scanner2` (private) holds v0.1–v0.2; v0.3+ local only.

## Schedules (launchd, PT, auto-start at login)

intraday 10m (6:30a–1:05p) · screen :20 hourly (6:20a–1:20p) · analyze 15m
(6:30a–1pm) · premarket 6:05a · nightly 2:05pm · saturday 10:15a · inbox 5m ·
bootstrap login ping. Holiday/weekend self-skip.

## Key decisions & corrections

- 60–70% wins at 1:5 rejected (math); grading is expectancy-based (~30% WR).
- $1k→$1M needs ~10 net doublings, not 20 (20 = $1B); 50 trades/yr math checks
  only if wins +100% with losses capped −20% — unproven, paper first.
- ADX gate is real (breakout fails TEST without it); MACD rejected (train/test
  disagree); stacking confirms kills samples — flow, don't filter.
- Target sweep: expectancy rises with target; fixed 5R rejected, trail instead.

## Standing rules

Paper only until 20+ closed paper trades hold live expectancy ≥ 0.
Grade changes immediate; param changes need explicit approval + TEST lock.
Never overwrite state on fetch failure (guard in screen.py).

## Open / pending

- Push v0.3–v0.9 to GitHub (needs fresh token; old ones deleted).
- Intraday logic backed only by 60d study + accumulating paper (least-trusted tier).
- Bear-market short patterns unbuilt; sizing engine unbuilt; options leg untested.

## Addendum — incidents, details, anything missing

- **Subscribers**: you (+1669…, iMessage) + Satya (+1906…, SMS relay).
  Per-recipient `--to` filter supported. First iMessage test landed 2026-09-04.
- **Sleep near-miss**: Mac was set to sleep after 1 min idle; fixed with
  `sudo pmset -c sleep 0` + power plugged. No agents were loaded until you ran
  the load commands from your Terminal (sandbox cannot touch launchd) —
  Tuesday 6:05am premarket was missed once because of that; caught up manually.
- **Labor Day**: market closed Mon 2026-09-07; holiday gate added
  (`market_calendar.py`) after you flagged it — nightly/premarket self-skip.
- **Data-loss guard**: a Yahoo rate-limit hiccup once returned zero rows and
  blanked the watchlist; screen.py now aborts (no state write, no text) when
  fewer than 10 tickers score.
- **Config crash**: a duplicate line once corrupted config.json and would have
  broken every run; fixed, verified, rule: edit it via code, not by hand.
- **Alpha Vantage**: key stored gitignored; free 25/day → nightly 4-call
  calibration only; throttled same-day during testing, recovers daily.
- **MCP verdict**: tradingview-mcp + maverick-mcp are servers for MCP clients;
  nothing to plug into here — underlying data/APIs used directly instead.
- **Saturday cron**: the old in-session scheduler was deleted; Mac agent owns it.
- **Alert tiers by trust**: daily signals (10y evidence) > watchlist (alignment)
  > approach texts (no edge, arming only) > intraday entries (paper-proving).
- **Commits**: v0.1 SP500+algo · docs · v0.2 scan/alerts · v0.3 holiday+two-tier ·
  v0.4 intraday · v0.5 change-only · v0.6 gap fixes · v0.7 two agents+AV ·
  v0.8 top-10+guard · v0.9 inbox commands · v0.10 analyzer evidence ·
  smart-routing/geo-fence/dedupe fixes · sub.py admin CLI · GRADES.md.
  All pushed; GitHub current.
- **Inbox hardening log**: RCS blob bodies decoded; 2h missed-command sweep
  (stale rows can PAUSE/RESUME/UNSUBSCRIBE but never SUBSCRIBE — no
  resurrection); per-sender last-text-wins; 10-min idempotency with
  state-aware replies ("Already subscribed/paused/active"); dry runs pure
  (no state writes); non-+1 senders silently ignored; re-SUBSCRIBE lifts pause.
- **Routing rule**: inbound any transport (RCS/iMessage/SMS); outbound SMS
  default with delivery-verify + iMessage fallback. Subscriber admin via
  `scanner/sub.py` (list/add/remove/pause/resume); never hand-edit config.json.
- **Grades for subscribers**: `GRADES.md` — A/A+/B+/B/C in plain language.
