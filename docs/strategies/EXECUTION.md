# Options execution strategy — FNL-SQ long, single-leg, through the Robinhood MCP

Status 2026-10-11: specified and wired in **shadow mode**. Nothing places a
real order until `data/exec/config.json` has `"armed": true`, which only the
user sets (§12). Synthesized from a three-lens design panel (risk-first,
execution-quality-first, autonomy-first) scored by two judges, over four
verified studies (`research/_cache/study_{exits,entries,iv}.py`,
`earnings_study.py`). Every number below is either cited to a study or
marked **[guess]** — guesses are tuned by the shadow journal, never by hand.

User directives this honours: single-leg options are the structure; all
option data and all orders go through the Robinhood MCP (no greeks derived
from yfinance or Black-Scholes in the live path); the strategy is a
procedure a Claude session runs every 15 minutes; subscriber texts carry the
stock-level plan and stay unchanged.

## 0. The decisions, in one screen

| question | answer | evidence |
|---|---|---|
| which setups | FNL-SQ long, grade B+, only | SQ long 49% win, +0.19R, PF 1.41, both halves; PB/BO planned loss is 7–8% of premium (uninsurable gap tail), shorts ≤ 0 |
| entry timing | the stock's next-open rule; option order 06:33–06:50 PT | every resting pullback limit lost on TEST (−0.05..−0.30R/signal); gap-up fills are not a chase penalty (+0.21 vs +0.20) |
| exits | stop at the stock stop, +2R target, 20-bar time stop; never trailed, never breakeven, no partials | every give-back variant lost on both halves (scale-out −0.16R/trade under executable fills) |
| expiry | nearest third-Friday monthly ≥ 49 DTE | 1.4 × 20 bars + 21 days cushion; weekly OI ≈ 9% of the monthly's |
| strike | MCP delta 0.55–0.75, target 0.65, most liquid in band | IV study band; floor 0.65 when IV is rich or a report sits inside the hold |
| structure | one long call; vertical only as an optional manual line | user directive; the MCP rejects multi-leg |
| governing stop | the stock stop, detected every 15 min by the session AND the loop | exits study; a premium-% stop fires on IV, not on the setup |
| resting stop | day-only `stop_market` sell-to-close, re-placed each session morning and after every fill (user decision 2026-10-11) | §7 — nothing rests overnight; the scheduled session covers the day |
| sizing | planned-loss budget per trade; premium caps; tiers by equity | §6; at $1,000 the layer is shadow-only (1% of signals fit) |
| expected option win rate | **~42%**, not the stock's 49% | 43% of time-stop exits sit below the theta breakeven (+0.3R) |

## 1. What runs where

| actor | runs | does | never does |
|---|---|---|---|
| premarket scan (`algos/funnel.py --scan`, 06:05 PT, launchd) | daily | writes ENTRY intents for today's texted SQ-long setups into `data/exec/intents.json` with the stock plan, HV20/HV60, earnings sessions | calls the MCP |
| 15-min loop (`ops/intraday.py`, 06:30–13:00 PT) | every run | books the paper fill (stage 1e), writes EXIT intents (stop / target / time / dte) off its print, alarms the owner when the session heartbeat is stale | calls the MCP, places orders |
| executor session (Claude Code + `robinhood-trading` MCP, `/fnl-exec`) | every 15 min 06:33–13:00 PT | reconciles, exits, re-rests stops, enters inside the window, writes `ledger.json` / `trades.csv` / `heartbeat.json` | writes the paper log, texts subscribers |
| user | once / rarely | sets `config.json` (account_number, armed), clears `halt`, allow-lists review alert codes, enters any manual vertical in the app | — |

Today the session is a user-opened Claude Code session running
`/loop 15m /fnl-exec` started at 06:33 PT (§11). The protocol does not
depend on how the session is started.

## 2. Expiry

- `min_dte = ceil(1.4 × horizon_bars) + 21` calendar days: SQ (20 bars) → **49**. Pick the nearest **third-Friday monthly** with `min_dte ≤ DTE ≤ min_dte + 60`; none → no trade. Weeklies never (ATM OI median 9% of the monthly's). On the 2026-10-12 calendar: **2026-12-18** (67 DTE; 39 left at the time stop).
- A report inside `[fill, time_stop_date]` → expiry must also be ≥ report + 7 calendar days (the 49-day rule already satisfies this for every SQ hold).
- Rolling: never. DTE < 21 while open (mis-pick, holiday shift) → close at the next run.
- The session reads `get_option_chains(underlying_symbol).expiration_dates`; the loop's hint from yfinance must name the same date or the intent blocks (`expiry_mismatch`).

## 3. Strike (MCP delta only)

- `get_option_instruments(chain_symbol, expiration_dates=<expiry>, type="call", tradability="tradable")`, keep strikes in `[0.85 S, 1.05 S]`, then `get_option_quotes(instrument_ids)` (≤ 20 ids per call).
- Admissible: `delta ∈ [0.55, 0.75]` from the quote (**no quote delta → no trade**). Floor rises to **0.65** when the IV regime is rich (§4) or a report falls inside the hold (extrinsic ≈ 53% of premium at 0.65Δ vs ~100% ATM; a 5-pt IV drop costs −10% vs −14..−22%).
- Pick: highest `open_interest` in band; tie (within 20%) → tighter spread % of mid; tie → delta nearest 0.65. Budget step-down: if sizing yields 0 contracts, retry the most liquid 0.60 then 0.55 strike; still 0 → `unfunded`.
- Text guidance (unchanged texts, optional lines): PB singles only at 0.55–0.66Δ (0.70+ fails the liquidity gate at PB's risk unit); a 2R debit vertical (short strike at the stock target) may be added as "manual alternative" for SQ; never a vertical on PB/BO (capping their runners turns +0.65R into −0.46R).

## 4. IV regime (MCP IV vs realized vol)

`iv_ratio = implied_volatility(chosen strike) / max(HV20, HV60)`; HV = annualized stdev of daily log returns from the daily cache. HV20 alone is depressed inside a squeeze by construction (GILD 2.13 vs 1.81, NVDA 1.29 vs 0.89), so the 60-day floor is mandatory.

| iv_ratio | action |
|---|---|
| ≤ 1.2 **[guess]** | single call, band 0.55–0.75 |
| 1.2–1.6 **[guess]** | single call, delta ≥ 0.65 only |
| > 1.6 **[guess]** | no autonomous entry; text may carry "manual vertical only" |

Also: `^VIX` prior close > 30 → halve the risk budget (LOOP.md), > 40 → no new entries **[guess]**. Thresholds are calibrated from `fidelity_err` by regime in the shadow journal (§12).

## 5. Entry discipline

1. **Stock rule unchanged**: paper fill = today's open iff `|open − level| ≤ 1 ATR` and `open > level − risk` (`funnel.fill_decisions`). No resting stock or option limits, no pullback wait, no re-entry on a later print.
2. **Window 06:33–06:50 PT** (never before 06:33: opening option quotes are empty or wide; hard cutoff 06:50, unfilled orders cancelled by 06:52) so the option fill stays next to the backtested open print.
3. **Live re-check** on `get_equity_quotes` (quote ≤ 60 s old): `|P − level| ≤ 1 ATR`, `P > level − risk`, and `P − open ≤ 0.5 ATR` **[guess]** so live reward:risk stays near 2:1. Any failure → `chased` / `through_stop`, final for that signal.
4. **Plan pinned to the paper row**: `stock_stop = open − risk`, `stock_target = open + 2 × risk`, `time_stop_date = fill + 19 sessions`. Sizing uses the actual `P_live → stock_stop` distance.
5. **Buy ladder** (single-leg, `type=limit`, `gfd`, regular hours; `half = (ask − bid)/2`, tick from `get_option_chains.min_ticks`, buy prices rounded up): step 0 `mid` 45 s → cancel → step 1 `mid + 0.33 half` 45 s → step 2 `mid + 0.67 half` 45 s → step 3 `ask` 30 s, only if `ask ≤ 1.02 × step-0 ask` and the planned loss at that price ≤ 1.10 × budget. Each step: `cancel_option_order` → poll `get_option_orders(order_id)` to a terminal state (≤ 60 s) → new `ref_id` (attempt n+1). One ladder restart if the mid runs past the step-0 ask; never two ladders after 06:50; partial fills are kept. **[guess: step sizes and waits; the shadow journal records the fill step and slippage]**
6. **Priority** when intents compete for slots (ordering only, never a filter): `sq_days ≤ 8`, then release-bar `rvol ≥ 1.2`, then lowest spread/R, then ticker.

## 6. Sizing

- **Planned loss per contract** `PL = 100 × (P_fill − P_at_stop)` where `P_at_stop` is the option price mapped to the stock stop with the quote's own greeks (a price map, not a greek derivation): `P_at_stop = P + Δ(S_stop − S) + ½Γ(S_stop − S)² + Θ × 10` (10 calendar days ≈ the median losing hold of 6 bars; Θ per day from the quote, negative). SQ median PL ≈ 51–61% of premium.
- **Premium at risk** `PR = 100 × P_fill × qty` (gap-to-zero), 1.6–2.0 × PL.
- **Budget RB and caps** (`E` = `get_portfolio.total_value` — the whole account, stock included, since it is all the user's risk capital; `options_bp` from the same call caps the premium separately. The first shadow run read "cash + options" instead of the total; this line settles it):

| tier | equity | RB per trade | premium / position | Σ premium | Σ PL | max open |
|---|---|---|---|---|---|---|
| shadow | < $2,500 | sized as if $2,500, qty recorded, nothing placed | — | — | — | 0 live |
| bootstrap | $2,500–4,999 | min(5% E, $250) | 10% E | 10% E | 7% E | 1 |
| standard | $5,000–24,999 | 3% E | 10% E | 25% E | 10% E | 2 (3 from $10k) |
| full | $25,000–49,999 | 2% E | 5% E | 20% E | 10% E | 4 |
| scale | ≥ $50,000 | 1.5% E | 4% E | 20% E | 8% E | 6 |

- The tiers are the growth path: the user adds capital when the shadow and live
  journals earn it, and the same rules then produce more contracts and more
  names — nothing in the code changes with a deposit. Equity is re-read every
  run, so a deposit is in force from the next run.
- `qty = floor(RB / PL)`; `qty = 1` allowed when `PL ≤ 1.25 × RB` **[guess]**; then `min` with the premium caps and `floor(options_bp / (100 P))`. Ten planned losers stay inside −25% of equity in every tier.
- **What fits** (SQ-long signal table, 0.65Δ, convexity rebate 15%): PL per contract q10 $188, median $732, q90 $3,853. One contract fits **1% of signals at $1,000** (5% budget), 8% at $2,500, 22% at $5,000, 16% at $10,000 (2%), **47% at $25,000** (2%). Throughput at $5k ≈ one trade a month. The rules keep their form at every size; only qty changes.
- One position per ticker across strategies; skip a candidate whose 60-day return correlation with any open underlying > 0.75 (daily cache) **[guess]**.

## 7. Stop-loss

1. **Governing stop = the stock stop**, pinned at the fill (`open − risk`), never moved to breakeven, never trailed, no partials. No premium-% stop: a premium drawdown with the stock above its stop is IV, not the setup failing.
2. **Detection, twice, same rule**: the session checks `get_equity_quotes.last ≤ stock_stop` on every run; the loop checks its 15-min print and writes an EXIT intent with the same deterministic id. Touch → exit, no waiting for a bounce.
3. **Execution** (sell-to-close ladder, single-leg): cancel the resting stop → poll terminal (filled meanwhile → record) → `mid` 30 s → `mid − 0.33 half` 30 s → `mid − 0.67 half` 30 s → `bid` 30 s → **`type=market`** (regular hours; stop and time exits must complete inside the run).
4. **Resting day stop (user decision 2026-10-11: `stop_market`, GFD)** — protects the hours between session runs. The MCP schema makes `market`/`stop_market` GFD + regular-hours only, so the order is placed by the **first session run of every morning** (06:33) for each open position and again right after every fill: `place_option_order(type="stop_market", stop_price, legs=[sell-to-close], quantity, time_in_force="gfd")` with `stop_price = round_down_tick(0.90 × P_at_stop_now)` (the option price mapped to the stock stop with today's quote greeks; the 0.90 buffer keeps a quote blip from converting a touch into a market sell **[guess]**). Re-placed (cancel → confirm → place) when the re-mapped trigger moves > 5%. If the trigger would sit at/above the current ask (MCP rejects): stock within 0.1R of its stop → exit now; otherwise (IV crush, stock intact) no resting stop that day and the loop/session path is primary. **Nothing rests overnight**: an overnight gap is sold at the first run via the ladder (§7.6). A morning on which the scheduled session fails to start has no in-market stop until it does — the loop alarms after 20 minutes (§9), and the scheduled job (§12) is what makes that rare.
5. **Earnings decision point**: on the last run before a report inside the hold, cancel the resting premium stop (an IV crush would trigger it with the stock flat — stock right, option stopped) and rely on the stock-print stop through the event; re-map and re-place on the first run after.
6. **Gap through the stop at the open**: the first session run sells at the open price via the ladder (executable convention: SQ 2R/20 stays +0.19R with gapped stops at the open).
7. **PDT never withholds a stop**: a same-day stop is the allowed third day trade if it comes to that (2 of 72 SQ stops are same-day).

## 8. Exits

- **Target**: stock print ≥ `open + 2 × risk` → sell ladder (mid → bid, no market order; at the target the call is worth 2.3–2.6 × PL, a 15-min delay costs little). Unfilled → re-ladder next run from the new mid.
- **No resting take-profit**: one contingent sell per position (Robinhood OCO is equity-only; a second sell-to-close against reserved contracts is expected to be rejected — verify in S1; only if co-resting works does a GTC limit at the mapped target price get rested).
- **Time stop**: the session where `trading_gap(fill_date, today) == 19` (close of bar 20, funnel.simulate's convention), executed at the last run ≥ 12:30 PT; the paper tracker is aligned to the same bar. Time exits are 32% of SQ trades, mean +0.47R stock — small winners, take them, do not hold into theta.
- DTE < 21 → close. Never roll. No partials, no breakeven, no trail.
- **Fidelity accounting per close**: `option_R = pnl / (PL × qty)`, `stock_R` from the linked paper row, `fidelity_err = option_R − stock_R`, `slip_entry = fill − step-0 mid`, `slip_exit = step-0 mid − exit`. "Tracking" = `|mean fidelity_err| ≤ 0.25R` over the last 20 closes.

## 9. Executor runbook (every 15 minutes, 06:33–13:00 PT)

Idempotent: every step re-reads the broker first; nothing depends on the previous run having finished. Tool names are the MCP's.

- **S0 preflight** — `get_accounts` → the configured `account_number` must be present with `agentic_allowed=true` and `option_level` ∈ {2, 3}; `get_portfolio(account_number)` → equity E, options buying power; `data/exec/halt` absent; `intents.json` written today; cache asof = previous session. Any failure → degraded mode (S1–S2 only), heartbeat `account_ok=false`, text the owner.
- **S1 reconcile** — `get_option_orders(account_number, created_at_gte=<5 sessions back>)` (paginate) and `get_option_positions(account_number, nonzero=true)`. Update every ledger order's state (filled → price/qty/time; cancelled/rejected/failed/voided → terminal). Agentic orders at the broker not in the ledger → `adopted` + alarm. Positions not in the ledger (the user's own) → `adopted`, never managed. Ledger positions missing at the broker → `closed_external`. Recount day trades from ALL fills in the rolling 5 sessions (`get_option_orders` + `get_equity_orders`, every `placed_agent`). Invariant: each open position has exactly one live stop order with the right qty; extras cancelled.
- **S2 exits** — union of EXIT intents from the file and the session's own check (`get_equity_quotes(symbols=<open tickers>)`: `last ≤ stock_stop`, `last ≥ stock_target`; `time_stop_date` with the clock ≥ 12:30 PT; DTE < 21). Per position once: cancel resting stop → poll terminal → `review_option_order` once → sell ladder per §7/§8. Record; append `trades.csv`; update `consecutive_stops`.
- **S3 resting stops** — for each open position without a live stop at the right trigger (every morning's first run since GFD orders expired, after a fill, after an earnings re-map): `get_option_quotes([option_id])` → `P_at_stop` → `review_option_order` → `place_option_order(type="stop_market", stop_price, legs=[{option_id, side:"sell", position_effect:"close"}], quantity, time_in_force="gfd")` (no `price`).
- **S4 entries** (only 06:33–06:50 PT; in shadow mode run through `review_option_order` and record a synthetic fill at the ASK) — sort pending ENTRY intents by priority; for each: slots/caps/loss caps/PDT → `get_equity_quotes([ticker])` re-check (§5.3) → `get_option_chains(underlying_symbol)` → expiry (§2, must match the hint) → `get_option_instruments(...)` → `get_option_quotes(...)` → pick (§3) → gates (§10) → size (§6) → `review_option_order(account_number, chain_symbol, underlying_type="equity", legs=[{option_id, side:"buy", position_effect:"open"}], quantity, type="limit", price, time_in_force="gfd")` — any `order_checks` alert not in `alert_allowlist` blocks the intent and is texted verbatim → ladder (§5.5) with `ref_id = uuid5(NAMESPACE_EXEC, intent_id + ":" + attempt)` persisted to the ledger BEFORE each `place_option_order`, re-sent unchanged only on a transport error after reconciling → on fill: position record, S3 stop, `trades.csv` open row.
- **S5 persist + text** — `ledger.json` and `heartbeat.json` written atomically (tmp + `os.replace`); one batched `FNL-EXEC HH:MM` text to the owner only (fills, exits, blocks with reasons, `N open, day trades k/3, day P&L`).

Fail-safes: session never starts → ENTRY intents expire at 06:50, EXIT intents stay pending, the GTC stop is the only protection, the loop alarms after 20 min without a heartbeat; session dies mid-ladder → next S1 finds the order (filled → record; open inside the window → resume; open past it → cancel); loop dies → the session's own exit check still runs, no new entries; MCP transport error → reconcile, then the same `ref_id`; cancel rejected because filled → record the fill; user closes a position in the app → `closed_external`, nothing further; user's manual spreads → adopted, never managed, counted for PDT only.

## 10. Guards (all deterministic; the session re-applies every one on live data)

1. **Earnings** — no signal-level veto (every veto candidate was noise; SQ long with a report inside the hold was +0.39R vs +0.14R). Option level: report inside `[fill, time_stop_date]` → delta floor 0.65, expiry ≥ report + 7 d, §7.5 decision point; `sessions_to_earnings ≤ earnings_entry_min_sessions` (config, default 0 = off) skips the option entry only if the user turns it on. Dates: `data/earnings_cache.json`, cross-checked in session with `get_earnings_results`/`get_earnings_calendar`; unknown → treat as "inside the hold".
2. **Liquidity** at the chosen contract on the live quote (quote ≤ 300 s old): `open_interest ≥ 250` (≥ 500 for strikes within 2.5% of spot), `(ask − bid)/mid ≤ 10%`, **full-cross spread ≤ 0.20 × delta × stock_risk** (the binding gate: one crossing ≤ 0.2R, ≈ 0.1R at mid-ish fills, against a +0.19R edge), `bid ≥ 0.10`, `bid ≤ mark ≤ ask`. Passes 4–6 of the 12 sampled names at the ~60-DTE monthly; expect mega-caps to dominate.
3. **IV regime** §4. **Chase** §5.3.
4. **Loss caps** **[guess]**: layer P&L today (realized + marked) ≤ −1.5 RB → no entries today; 3 consecutive stops → next entry at half qty (min 1) until a winner; layer equity −15% from its 20-session high → `halt` until the user clears it.
5. **PDT (E < $25k)**: entries only when the rolling 5-session count (all fills, all agents, options + equities) ≤ 2; stops and targets are never deferred.
6. **Positions**: tier `max_open`; one per ticker; pending non-terminal orders count; correlation guard §6.
7. **Duplicates**: deterministic ids; one non-terminal order per intent and per `option_id+side`; `ref_id` persisted before every place; S1 before any new order; the loop never rewrites an id the ledger knows.
8. **Stale data**: intents written today; cache asof = previous session; stock quote ≤ 60 s for entries, option quotes ≤ 300 s; heartbeat ≤ 20 min; expiry hint must match the chain.
9. **Account/venue**: agentic account only, option level ≥ 2, single-leg, every order reviewed first, regular hours, `market` only on stop/time exits.
10. **Regime gate (user decision 2026-10-11: on)**: the option entry requires the signal bar's bull flag (close > SMA200): SQ long bull n=138 +0.23R vs bear n=27 −0.01R. The paper row and the subscriber text are unaffected (the funnel's own policy has no stock-level regime filter); only the option layer skips bear-regime signals.

## 11. Order-intent protocol (files under `data/exec/`, gitignored, one writer each)

| file | writer | content |
|---|---|---|
| `config.json` | user | every constant (§13), `account_number`, `armed`, `alert_allowlist`, `notify_to` |
| `intents.json` | premarket scan (ENTRY) + loop (EXIT), regenerated each run, append-by-id | see below |
| `ledger.json` | session | heartbeat, equity/tier, intents' outcomes, positions, orders, adopted, day trades, daily P&L |
| `trades.csv` | session (append) | one row per closed live or shadow position with the fidelity fields |
| `heartbeat.json` | session | `last_run`, `runs_today`, `account_ok`, `mode` |
| `halt` | user | presence blocks entries; exits always run |

ENTRY intent (`intent_id = "FNLX-{fill_date}-{ticker}-SQ-L"`):

```json
{"intent_id": "FNLX-20261013-PG-SQ-L", "kind": "ENTRY", "created": "2026-10-13T06:05:30-07:00",
 "window_pt": ["06:33", "06:50"], "ticker": "PG", "family": "SQ", "side": "long", "grade": "B+",
 "signal_date": "2026-10-12", "fill_date": "2026-10-13",
 "stock": {"level": 151.23, "risk": 7.27, "atr": 3.12, "time_bars": 20, "exit": "fixed", "exit_mult": 2.0,
           "sq_days": 11, "rvol": 1.1, "bull": true},
 "vol": {"hv20": 0.17, "hv60": 0.21},
 "earnings": {"next": "2026-10-24", "sessions_to": 9, "inside_hold": true},
 "expiry_hint": "2026-12-18",
 "paper_row": {"strategy": "FNL-SQ", "date": "2026-10-13"}}
```

EXIT intent (`intent_id = "{position_id}-X-{stop|target|time|dte}"`): `{"intent_id", "kind": "EXIT", "position_id", "reason", "stock_print", "print_time"}`.

Ledger position: `{position_id (= intent_id), ticker, option_id, chain_id, symbol, expiry, strike, qty, fill_price, fill_time, fill_date, delta_fill, iv_fill, gamma_fill, theta_fill, stock_at_fill, stock_open, stock_stop, stock_target, time_stop_date, planned_loss, premium, resting_stop {order_id, stop_price, limit, placed, state}, status open|closing|closed|closed_external, exit {reason, price, time, stock_print}}`.

`trades.csv` columns: `intent_id, mode, ticker, signal_date, fill_date, expiry, strike, qty, fill_premium, delta_fill, iv_fill, iv_ratio, planned_loss, stock_open, stock_stop, stock_target, exit_date, exit_reason, exit_premium, pnl_usd, option_R, stock_R, fidelity_err, slip_entry, slip_exit, day_trade`.

Idempotency: ids are deterministic and regenerable; the ledger is the dedupe store; `ref_id = uuid5(NAMESPACE_EXEC, intent_id:attempt)`; the same `ref_id` is re-sent only on a transport retry after S1; a new ladder step is a new attempt. `signal_log.csv` stays the subscriber truth; the loop appends `exec=LIVE|SHADOW id=… fill=…x… K=… exp=…` to the paper row's notes so paper and option rows join on `intent_id`.

## 12. Shadow mode, arming, promotion

- **Shadow** (`armed=false` or E < $2,500): S0–S3 read-only (no placements, no cancels); S4 runs through `review_option_order` and records a synthetic fill at the **ask**; exits at the **bid** (pessimistic, so fidelity and slippage are not flattered). Rows carry `mode=shadow`.
- **Arming**: ≥ 20 shadow round trips, `|mean fidelity_err| ≤ 0.25R`, simulated slip ≤ 0.10R per side, ≥ 10 sessions with zero reconcile errors, E ≥ $2,500 — then the user sets `armed: true`. Saturday can only disarm (last 10 live `option_R` mean < −0.3 while their paper `stock_R` mean > 0 = execution drag).
- **S1 supervised live**: one contract, `max_open=1`, every action texted; 10 trades, zero checklist violations, realized slip ≤ 0.15R/side. Also verifies: co-resting a take-profit next to the day stop; the `stop_market` trigger reference (mark vs last vs bid); whether `get_accounts` exposes a day-trade counter.
- **S2 autonomous within guards**. **S3 scheduled session (implemented 2026-10-11)**: `~/Library/LaunchAgents/com.trade.fnlexec.plist` runs `run_scan.sh fnlexec` → `claude -p` (headless, verified to reach the Robinhood MCP with the stored login) at 06:33 and 06:48 PT (the entry window) and then every 30 minutes to 12:48 PT on market days — 14 runs a day, each one `/fnl-exec` cycle with an explicit tool allowlist (the MCP server, Read/Write/Edit/Glob/Grep, `.venv/bin/python`) and a 60-turn cap, logged to `logs/fnlexec-<date>.log`. The 30-minute cadence outside the entry window is a usage-cost choice: the loop's print-based EXIT intents and the day stop cover the gaps; `/loop 15m /fnl-exec` from an interactive session is the denser alternative. The loop texts "EXEC session down" when the heartbeat is > 20 min old with open positions or pending intents. Overlapping runs are prevented by a lock directory; a run older than 20 minutes is treated as dead.

## 13. Parameters (`data/exec/config.json`)

```json
{"armed": false, "account_number": null, "notify_to": null, "alert_allowlist": [],
 "families": ["SQ"], "sides": ["long"], "min_grade": "B+",
 "entry_window_pt": ["06:33", "06:50"], "entry_chase_atr": 1.0, "entry_max_above_open_atr": 0.5,
 "expiry": {"min_dte": 49, "max_dte_over_min": 60, "monthly_only": true, "close_dte": 21},
 "delta": {"lo": 0.55, "hi": 0.75, "target": 0.65, "rich_floor": 0.65},
 "iv": {"single_max": 1.2, "rich_max": 1.6, "vix_halve": 30, "vix_stop": 40},
 "liquidity": {"oi_min": 250, "oi_min_near_spot": 500, "spread_pct_max": 10.0, "spread_r_max": 0.20, "bid_min": 0.10, "quote_max_age_s": 300},
 "sizing": {"tiers": [{"min": 0, "rb_pct": 0.0, "prem_pos": 0.0, "prem_sum": 0.0, "pl_sum": 0.0, "max_open": 0},
                      {"min": 2500, "rb_pct": 0.05, "rb_cap": 250, "prem_pos": 0.10, "prem_sum": 0.10, "pl_sum": 0.05, "max_open": 1},
                      {"min": 5000, "rb_pct": 0.03, "prem_pos": 0.10, "prem_sum": 0.25, "pl_sum": 0.10, "max_open": 2},
                      {"min": 10000, "rb_pct": 0.03, "prem_pos": 0.10, "prem_sum": 0.25, "pl_sum": 0.10, "max_open": 3},
                      {"min": 25000, "rb_pct": 0.02, "prem_pos": 0.05, "prem_sum": 0.20, "pl_sum": 0.10, "max_open": 4}],
            "qty_tolerance": 1.25, "pl_days": 10, "corr_max": 0.75},
 "stops": {"type": "stop_market", "tif": "gfd", "buffer": 0.90, "restrike_pct": 5.0, "near_stop_r": 0.1},
 "ladder": {"buy": [0.0, 0.33, 0.67, 1.0], "buy_wait_s": [45, 45, 45, 30], "ask_cap": 1.02, "budget_cap": 1.10,
            "sell": [0.0, -0.33, -0.67, -1.0], "sell_wait_s": 30, "market_after_sell_ladder": ["stop", "time", "dte"]},
 "loss_caps": {"day_rb": 1.5, "consec_stops_halve": 3, "halt_dd": 0.15},
 "pdt": {"entry_max_used": 2, "exempt_equity": 25000},
 "earnings": {"entry_min_sessions": 0, "expiry_after_report_days": 7},
 "regime": {"bull_only": true},
 "stale": {"stock_quote_s": 60, "heartbeat_s": 1200}}
```

## 14. Known limits

- No option P&L backtest exists (no historical option data); the overlay (+0.23..+0.31 PL-R on the real SQ exits, constant IV) is a model, and the IV path after a release is unmeasured. The shadow journal is the test.
- At $1,000 nothing trades live; the layer earns its record in shadow while the account grows by deposits or by the user's own trades.
- Fills are read from Yahoo's lagged prints in the loop; the session's MCP quotes are authoritative.
- The resting day stop's trigger reference (mark vs last vs bid) is broker behaviour only a live test settles; nothing rests overnight by design.
- The scheduled headless session is a Claude Code run per slot (14 a day): usage counts against the user's plan; a usage-limit refusal at a slot is logged and the next slot retries. A missed morning slot leaves open positions without a day stop until the next successful run.
- Throughput ≈ 2.5 SQ-long signals a week pool-wide before liquidity and sizing; expect one live trade a month at $5k.
