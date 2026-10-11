#!/bin/bash
# Trading-system runner for launchd. Modes: nightly | premarket | saturday | bootstrap | weekly
# Logs: logs/<mode>-<date>.log. Alerts go out over iMessage.
PROJ=/Users/vismaypatel/trading-indicators
cd "$PROJ" || exit 1
source "$PROJ/.venv/bin/activate"
mkdir -p "$PROJ/logs"
STAMP=$(date +%F)
MODE=${1:-nightly}

case "$MODE" in
  nightly|premarket)
    if ! python algos/market_calendar.py > /dev/null 2>&1; then
      echo "$(date +%F) $MODE skipped: market closed" \
        >> "$PROJ/logs/$MODE-$STAMP.log" 2>&1
      exit 0
    fi
    ;;
esac
case "$MODE" in
  nightly)
    python ops/scan.py --pool etf --intraday --channels imessage \
      >> "$PROJ/logs/nightly-$STAMP.log" 2>&1
    ;;
  premarket)
    python ops/scan.py --pool top50 --mode premarket --channels imessage \
      >> "$PROJ/logs/premarket-$STAMP.log" 2>&1
    # swing funnel: watch pool + today's setups off yesterday's completed
    # bars (fills book at the open in the intraday loop)
    python algos/funnel.py --scan --channels imessage \
      >> "$PROJ/logs/funnel-$STAMP.log" 2>&1
    ;;
  saturday)
    OUT=$(python ops/saturday.py 2>&1)
    echo "$OUT" >> "$PROJ/logs/saturday-$STAMP.log" 2>&1
    HEADLINE=$(echo "$OUT" | grep -E "Closed|No closed|Demot|DEMOTE|Regime|REBALANCE" | head -6 | tr '\n' '; ')
    python ops/notify.py --channel imessage \
      --message "Saturday review: ${HEADLINE:-done, see log}" >> "$PROJ/logs/saturday-$STAMP.log" 2>&1
    ;;
  bootstrap)
    # no subscriber text on login anymore: local log line only.
    echo "online after login ($(date '+%F %H:%M')), no alerts sent" \
      >> "$PROJ/logs/bootstrap-$STAMP.log" 2>&1
    ;;
  intraday)
    # session only: regular-hours tape, no weekend/evening re-evaluation
    if ! python algos/market_calendar.py > /dev/null 2>&1; then
      echo "$(date '+%F %T') intraday skipped: market closed" \
        >> "$PROJ/logs/intraday-$STAMP.log" 2>&1
      exit 0
    fi
    HM=$((10#$(date +%H%M)))
    if [ "$HM" -lt 630 ] || [ "$HM" -gt 1300 ]; then
      echo "$(date '+%F %T') intraday skipped: outside 06:30-13:00 PT" \
        >> "$PROJ/logs/intraday-$STAMP.log" 2>&1
      exit 0
    fi
    python ops/intraday.py --channels imessage \
      >> "$PROJ/logs/intraday-$STAMP.log" 2>&1
    ;;
  screen)
    python ops/screen.py --channels imessage \
      >> "$PROJ/logs/screen-$STAMP.log" 2>&1
    ;;
  analyze)
    python algos/analyze.py --channels imessage \
      >> "$PROJ/logs/analyze-$STAMP.log" 2>&1
    ;;
  inbox)
    python ops/inbox.py --channels imessage \
      >> "$PROJ/logs/inbox-$STAMP.log" 2>&1
    ;;
  weekly)
    # Friday 12:00 PT anchor-box consolidation scan (one batched text).
    # Friday-off fallback (prev close) lives inside weekly_box.py.
    if ! python algos/market_calendar.py > /dev/null 2>&1; then
      echo "$(date '+%F %T') weekly skipped: market closed" \
        >> "$PROJ/logs/weekly-$STAMP.log" 2>&1
      exit 0
    fi
    python algos/weekly_box.py --scan --no-refire --channels imessage \
      >> "$PROJ/logs/weekly-$STAMP.log" 2>&1
    ;;
  screener)
    # Monday 06:00 PT universe refresh (cap + RVOL gates, add/remove).
    # No texts: updates screener_pool.csv for the scan union only.
    python algos/screener.py --run \
      >> "$PROJ/logs/screener-$STAMP.log" 2>&1
    ;;
  fnlexec)
    # Options executor (docs/strategies/EXECUTION.md): one headless Claude
    # Code run of /fnl-exec against the Robinhood MCP. Scheduled 06:33 and
    # 06:48 PT (entry window) then every 30 min to 12:48 PT on market days.
    # Shadow mode until data/exec/config.json has armed=true. "force" as $2
    # skips the day/time guards for a manual test run.
    LOG="$PROJ/logs/fnlexec-$STAMP.log"
    if [ "${2:-}" != "force" ]; then
      if ! python algos/market_calendar.py > /dev/null 2>&1; then
        echo "$(date '+%F %T') fnlexec skipped: market closed" >> "$LOG" 2>&1
        exit 0
      fi
      HM=$((10#$(date +%H%M)))
      if [ "$HM" -lt 630 ] || [ "$HM" -gt 1300 ]; then
        echo "$(date '+%F %T') fnlexec skipped: outside 06:30-13:00 PT" >> "$LOG" 2>&1
        exit 0
      fi
    fi
    LOCK="$PROJ/logs/.fnlexec.lock"
    if [ -d "$LOCK" ]; then
      if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +20 2>/dev/null)" ]; then
        rmdir "$LOCK" 2>/dev/null   # a run older than 20 min is dead
      else
        echo "$(date '+%F %T') fnlexec skipped: previous run still active" >> "$LOG" 2>&1
        exit 0
      fi
    fi
    mkdir "$LOCK" 2>/dev/null || exit 0
    trap 'rmdir "$LOCK" 2>/dev/null' EXIT
    echo "===== $(date '+%F %T') fnlexec run =====" >> "$LOG" 2>&1
    # explicit allowlist: the Robinhood MCP, repo reads, file edits ONLY
    # under data/exec/ (Edit(path) rules cover every file-editing tool;
    # verified 2026-10-11: inside allowed, outside denied), the venv python
    # (helpers + notify); no git, no network tools; 60 turns max; 15-minute
    # kill timer so a hung run never blocks the next slot
    perl -e 'alarm 900; exec @ARGV' /Users/vismaypatel/.local/bin/claude -p \
      "Run exactly one executor cycle now: read .claude/skills/fnl-exec/SKILL.md first and follow it literally (S0 preflight, S1 reconcile, S2 exits, S3 resting day stops, S4 entries only inside 06:33-06:50 PT, S5 persist + text). docs/strategies/EXECUTION.md is the spec if a rule is unclear. State lives in data/exec/ and that is the only place you may write. Do one cycle, then stop; end with one line: FNL-EXEC HH:MM mode=<shadow|live> <summary>." \
      --allowedTools "mcp__robinhood-trading" "Read" "Glob" "Grep" "Edit(data/exec/**)" \
        "Bash(.venv/bin/python:*)" "Bash(date:*)" "Bash(ls:*)" "Bash(cat:*)" "Bash(mkdir:*)" \
      --max-turns 60 --output-format text >> "$LOG" 2>&1
    echo "===== $(date '+%F %T') fnlexec done (exit $?) =====" >> "$LOG" 2>&1
    ;;
esac
