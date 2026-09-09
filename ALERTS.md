# Run book + free real-time phone alerts (Mac)

## How to run (everything)

```bash
cd /Users/vismaypatel/trading-indicators
source .venv/bin/activate            # every fresh shell, always first
python scanner/scan.py               # nightly-style scan, top-50, prints only
python scanner/scan.py --pool sp500  # full pool (slower, ~minutes)
python scanner/scan.py --mode premarket   # gap-gate on triggered names only
python review/saturday.py            # weekly review (also runs Saturdays 10:15)
python backtest/combos.py --help     # research grid
```

How signals reach you: `scan.py` prints every setup, appends it to
`scanner/signal_log.csv` (Saturday review reads this), and forwards it to
`--channels` (default `dry` = print only).

## Free real-time alerts to your phone, ranked

### 1. Telegram — recommended (free, instant, no limits)

1. In Telegram, message `@BotFather` → `/newbot` → free token.
2. Message your bot anything, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your chat id.
3. `cp scanner/config.example.json scanner/config.json`, fill token + chat id.
4. Test: `python scanner/notify.py --channel telegram --message "test"`
5. Go live: `python scanner/scan.py --channels telegram`

### 2. Mac Shortcut → iMessage to yourself (free, native)

1. Shortcuts.app → new shortcut, "Receive Text input", add "Send Message"
   (to yourself) and/or "Show Notification". Name it `Trade Alert`.
2. Test: `python scanner/notify.py --channel shortcut:Trade\ Alert --message "test"`
3. Go live: `python scanner/scan.py --channels shortcut:Trade\ Alert`
4. Combine: `--channels telegram,shortcut:Trade\ Alert,mac` fans out to all.

### 3. Direct iMessage from this Mac (free, no shortcut needed)

Set `TRADE_IMSG_TO` (your own mobile number) or `imsg_to` in config.json,
then `--channels imessage`. Requires Messages.app signed in; macOS asks for
Automation permission once.

### 4. Mac banner only (free, Mac stays nearby)

`--channels mac`. Pops a notification on this Mac via osascript.

## Scheduling (free, on this Mac)

Live agents (all in `~/Library/LaunchAgents/com.trade.*.plist`, auto-start at
login): intraday 10m · screen hourly :20 · analyze 15m · premarket 6:05am ·
nightly 2:05pm · saturday 10:15am · inbox 5m · bootstrap login ping.

## Operations (run in your Terminal to see live status)

```bash
launchctl list | grep trade   # agents loaded? (PID "-" + 0 = armed, idle)
tail -5 scanner/logs/inbox-$(date +%F).log    # inbox polls + commands seen
tail -5 scanner/logs/analyze-$(date +%F).log  # analyzer output
# manual inbox check + send path (use --channels sms; dry sends nothing):
python scanner/inbox.py --channels sms
python scanner/notify.py --channel sms --to me --message "test"
```

- Keep it paper until `signal_log.csv` holds 20+ closed trades with live
  expectancy ≥ 0 — the Saturday loop enforces this, not willpower.
- Market holidays/weekends: nightly and pre-market runs self-skip
  (`scanner/market_calendar.py` — NYSE calendar); no texts on closed days.
