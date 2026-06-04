#!/usr/bin/env bash
# Install / uninstall the daily PayPay snapshot launchd agent (macOS).
#
#   bin/snapshot-cron.sh install     # schedule daily 07:30 (local time) snapshot
#   bin/snapshot-cron.sh uninstall   # remove the schedule
#   bin/snapshot-cron.sh status      # show whether it's loaded + the log tail
#
# The agent runs bin/snapshot-daily.sh (READ-ONLY snapshot; weekly diff on Monday).
# Run this from the COPY you want to track — e.g. the installed skill so that
# `npx skills update` keeps the wrapper current.
set -euo pipefail

LABEL="com.paypay-securities.snapshot"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SKILL_DIR/bin/snapshot-daily.sh"
LOG="$HOME/.paypay-sec/snapshot-cron.log"
HOUR="${PAYPAY_SNAPSHOT_HOUR:-7}"
MINUTE="${PAYPAY_SNAPSHOT_MINUTE:-30}"

case "${1:-}" in
  install)
    chmod +x "$WRAPPER"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.paypay-sec"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>${WRAPPER}</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load -w "$PLIST"
    echo "installed: ${LABEL} → daily ${HOUR}:$(printf '%02d' "$MINUTE") (local time)"
    echo "  wrapper: $WRAPPER"
    echo "  log    : $LOG"
    echo "  (override time: PAYPAY_SNAPSHOT_HOUR / PAYPAY_SNAPSHOT_MINUTE before install)"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled: ${LABEL}"
    ;;
  status)
    if launchctl list | grep -q "$LABEL"; then
      echo "loaded: ${LABEL}"
    else
      echo "not loaded (run: bin/snapshot-cron.sh install)"
    fi
    [ -f "$PLIST" ] && echo "plist : $PLIST" || echo "plist : (none)"
    [ -f "$LOG" ] && { echo "--- log tail ---"; tail -n 8 "$LOG"; } || echo "log   : (none yet)"
    ;;
  *)
    echo "usage: $(basename "$0") install | uninstall | status" >&2
    exit 2
    ;;
esac
