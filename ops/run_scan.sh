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
esac
