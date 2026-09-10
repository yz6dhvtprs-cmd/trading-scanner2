#!/bin/bash
# Start/stop/status for the trade scanner agents (no tokens needed).
# Usage (from /Users/vismaypatel/trading-indicators):
#   ./scanner/agents.sh start | stop | restart | status
cd /Users/vismaypatel/trading-indicators || exit 1
PLISTS=(~/Library/LaunchAgents/com.trade.*.plist)
case "${1:-status}" in
  start)
    for p in "${PLISTS[@]}"; do
      if launchctl list "$(basename "$p" .plist)" >/dev/null 2>&1; then
        echo "already running: $(basename "$p" .plist)"
      else
        launchctl load "$p" 2>&1 && echo "started: $(basename "$p" .plist)"
      fi
    done ;;
  stop)
    for p in "${PLISTS[@]}"; do
      label="$(basename "$p" .plist)"
      if launchctl list "$label" >/dev/null 2>&1; then
        launchctl unload "$p" 2>&1 && echo "stopped: $label"
      else
        echo "not running: $label"
      fi
    done ;;
  restart) "$0" stop; sleep 2; "$0" start ;;
  status) launchctl list 2>/dev/null | grep com.trade || echo "no trade agents loaded" ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 1 ;;
esac
