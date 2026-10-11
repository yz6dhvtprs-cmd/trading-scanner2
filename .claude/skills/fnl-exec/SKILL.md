---
name: fnl-exec
description: One run of the options executor for the swing funnel (docs/strategies/EXECUTION.md) against the Robinhood MCP — reconcile, exits, resting stops, entries inside 06:33–06:50 PT, persist, text. Invoke every 15 minutes during the session (`/loop 15m /fnl-exec`). Shadow mode until data/exec/config.json has armed=true.
---

# /fnl-exec — one executor run (idempotent, every 15 minutes, 06:33–13:00 PT)

Read `docs/strategies/EXECUTION.md` once per session. State lives in `data/exec/`
(`config.json` user-owned, `intents.json` loop-owned, `ledger.json` / `heartbeat.json` /
`trades.csv` session-owned, `halt` user-owned). Helpers: `algos/execution.py`
(`norm_quote`, `pick_expiry`, `pick_contract`, `price_at_stock`, `planned_loss`,
`size`, `ladder_prices`, `stop_order`, `restrike_needed`, `day_trades`, `ref_id`,
`trade_row`, `append_trade`, `atomic_write_json`). Run Python helpers with
`.venv/bin/python`. Never write `intents.json` or `data/signal_log.csv`, and never
edit anything outside `data/exec/` — not the repo, not memory files; what you learn
goes into `ledger.json` notes and the run summary.

Mode: `armed` false or equity < $2,500 → **shadow**: no `place_option_order`, no
`cancel_option_order`; everything else runs; synthetic fills at the ASK, synthetic
exits at the BID. Say the mode in the first line of every run's output.

## S0 preflight
1. `get_accounts` → the configured `account_number` exists, `agentic_allowed=true`,
   `option_level` ∈ {2, 3}. `get_portfolio(account_number)` → equity E =
   `total_value` (the whole account, stock included), options buying power =
   `buying_power.buying_power`.
2. `data/exec/halt` absent; `intents.json.session_date` == today; cache asof
   (intents' `signal_date`) == previous session.
3. Any failure → degraded mode (S1, S2 only), heartbeat `account_ok=false`, text.

## S1 reconcile (before any new order)
1. `get_option_orders(account_number, created_at_gte=<5 sessions back>)` (follow
   `cursor`) and `get_option_positions(account_number, nonzero=true)`.
2. Update every ledger order's state (filled → price/qty/time; cancelled/rejected/
   failed/voided → terminal). Agentic orders not in the ledger → `adopted` + alarm.
   Positions not in the ledger → `adopted`, never managed. Ledger positions missing
   at the broker → `closed_external`.
3. Day-trade count = `execution.day_trades(fills, today)` over ALL fills (also
   `get_equity_orders`), every `placed_agent`.
4. Invariant: every open position has exactly one live stop order with its qty;
   cancel extras (live mode).

## S2 exits (always run, even degraded)
For each open position: `get_equity_quotes([ticker])`. Reasons, in order: `last ≤
stock_stop` → stop; `last ≥ stock_target` → target; `trading_gap(fill_date, today) ==
time_bars − 1` and clock ≥ 12:30 PT → time; DTE < 21 → dte. Also take EXIT intents from
`intents.json` for the same position (same ids; dedupe). Per position once:
1. cancel the resting stop → poll `get_option_orders(order_id)` to terminal; filled
   meanwhile → record, done.
2. `get_option_quotes([option_id])` → ladder from `execution.ladder_prices(bid, ask,
   tick, "sell")`: `review_option_order` once, then `place_option_order` per step
   (`type="limit"`, `time_in_force="gfd"`, `position_effect="close"`), 30 s each,
   cancel → poll terminal → new `ref_id` per step. Stop/time/dte exits end in
   `type="market"` if still unfilled; a target exit stays pending and re-ladders next run.
3. Record the exit in the ledger; `execution.trade_row(...)` + `append_trade` with the
   paper row's stock R when known; `consecutive_stops` update.

## S3 resting day stops (every morning's first run — GFD orders expired overnight —
## after any fill, after an earnings re-map)
For each open position without a live stop at the right trigger: `get_option_quotes
([option_id])` → `q = norm_quote(...)` → `execution.stop_order(q, S_now, stock_stop,
tick)` (returns `stop_price`, `type="stop_market"`, `tif="gfd"`); `restrike_needed(old,
new)` decides re-placement. Place `review_option_order` → `place_option_order(
type="stop_market", stop_price, legs=[{option_id, side:"sell", position_effect:"close"}],
quantity, time_in_force="gfd")` — no `price`. If the trigger would sit at/above the ask:
stock within 0.1R of its stop → exit now (S2), else no resting stop today
(`no_rest_iv`). Earnings decision point: last run before a report inside the hold →
cancel the resting stop, rely on the print path; first run after → re-map and re-place.
Never withhold a stop for PDT reasons. Nothing rests overnight by design.

## S4 entries (only 06:33–06:50 PT; never degraded; shadow runs through review)
Sort pending ENTRY intents (already in priority order; the premarket scan already
dropped bear-regime signals because `regime.bull_only` is on). For each:
1. Slots/caps: tier `max_open`, one per ticker, loss caps (day P&L ≤ −1.5 RB → none;
   3 consecutive stops → half qty), PDT (used ≥ 3 → none; entries only when used ≤ 2),
   `halt` absent, correlation guard.
2. `get_equity_quotes([ticker])` (≤ 60 s old): `|P − level| ≤ entry_chase_atr × ATR`,
   `P > level − risk`, `P − open ≤ 0.5 ATR` (open = the intent's paper fill if the loop
   booked it, else the quote's day open). Fail → `chased`/`through_stop`, final.
3. `get_option_chains(underlying_symbol=ticker)` → `expiration_dates`, `min_ticks`;
   `execution.pick_expiry(expiration_dates, today)` must equal `expiry_hint`, else
   `expiry_mismatch`.
4. `get_option_instruments(chain_symbol=ticker, expiration_dates=<expiry>, type="call",
   tradability="tradable")` (follow `cursor`), keep strikes in [0.85 S, 1.05 S] →
   `get_option_quotes(instrument_ids=<≤ 20 ids>)` → `norm_quote` each.
5. IV regime: `execution.iv_regime(iv at the 0.65-nearest strike, hv20, hv60)` →
   `single` (floor 0.55) / `rich` (floor 0.65) / `none` (block, record). A report inside
   the hold also floors delta at 0.65.
6. `execution.pick_contract(quotes, S, risk, cfg, delta_floor)`; none → record reason.
7. `planned_loss(q, S, stock_stop)`; `size(E, bp, PL, 100 × mid, open_prem_sum,
   open_pl_sum, n_open)`; qty 0 → try the most liquid 0.60 then 0.55 strike; still 0 →
   `unfunded` (shadow row still written with qty as if E = $2,500).
8. `review_option_order(account_number, chain_symbol, underlying_type="equity",
   legs=[{option_id, side:"buy", position_effect:"open"}], quantity, type="limit",
   price=<step-0 mid>, time_in_force="gfd")`. Record `order_checks` verbatim; any alert
   code not in `alert_allowlist` → `blocked:alert:<code>` + text.
9. Ladder `execution.ladder_prices(bid, ask, tick, "buy")` with waits 45/45/45/30 s;
   `ref_id = execution.ref_id(intent_id, attempt)` written to the ledger BEFORE each
   `place_option_order`; cancel → poll terminal (≤ 60 s) → next step; stop when the
   planned loss at the step price > 1.10 × RB or the ask > 1.02 × step-0 ask; one ladder
   restart max; nothing after 06:50; partial fills kept.
10. Fill: position record (pinned `stock_stop = open − risk`, `stock_target = open +
    2·risk`, `time_stop_date = fill + 19 sessions`, `planned_loss` from the fill price and
    greeks), S3 stop, `trades.csv` open row. Shadow: synthetic fill at the ask.

## S5 persist + text
`ledger.json` and `heartbeat.json` (`last_run`, `runs_today`, `account_ok`, `mode`)
via `execution.atomic_write_json`; append trades; one batched text to `notify_to` only:
`.venv/bin/python algos/notify.py --channel imessage --to <notify_to> --message
"FNL-EXEC HH:MM mode=… fills/exits/blocks… N open, DT k/3, day P&L"`. Subscribers get
nothing.

## Fail-safes
Session dies mid-ladder → next S1 finds the order (filled → record; open inside the
window → resume; past it → cancel, `unfilled`). MCP transport error on place → S1
reconcile, then re-send the SAME `ref_id`. Cancel rejected because filled → record the
fill. Account not agentic / level < 2 → no orders, text. User closed a position in the
app → `closed_external`. Never two non-terminal orders per intent or per option_id+side.
