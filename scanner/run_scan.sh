#!/bin/bash
# Trading-system runner for launchd. Modes: nightly | premarket | saturday | bootstrap
# Logs: scanner/logs/<mode>-<date>.log. Alerts go out over iMessage.
PROJ=/Users/vismaypatel/trading-indicators
cd "$PROJ" || exit 1
source "$PROJ/.venv/bin/activate"
mkdir -p "$PROJ/scanner/logs"
STAMP=$(date +%F)
MODE=${1:-nightly}

case "$MODE" in
  nightly)
    python scanner/scan.py --pool top50 --channels imessage \
      >> "$PROJ/scanner/logs/nightly-$STAMP.log" 2>&1
    ;;
  premarket)
    python scanner/scan.py --pool top50 --mode premarket --channels imessage \
      >> "$PROJ/scanner/logs/premarket-$STAMP.log" 2>&1
    ;;
  saturday)
    OUT=$(python review/saturday.py 2>&1)
    echo "$OUT" >> "$PROJ/scanner/logs/saturday-$STAMP.log" 2>&1
    HEADLINE=$(echo "$OUT" | grep -E "Closed|No closed|Demot|DEMOTE|Regime|REBALANCE" | head -6 | tr '\n' '; ')
    python scanner/notify.py --channel imessage \
      --message "Saturday review: ${HEADLINE:-done, see log}" >> "$PROJ/scanner/logs/saturday-$STAMP.log" 2>&1
    ;;
  bootstrap)
    python scanner/notify.py --channel imessage \
      --message "Trading system online after login ($(date '+%F %H:%M')). Jobs: nightly 2:05pm, premarket 6:05am, Sat review 10:15am." \
      >> "$PROJ/scanner/logs/bootstrap-$STAMP.log" 2>&1
    ;;
esac
