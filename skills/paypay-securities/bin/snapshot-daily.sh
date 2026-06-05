#!/usr/bin/env bash
# Daily PayPay snapshot (READ-ONLY) — saves an account snapshot so `paypay diff`
# can show changes over time. Run by a launchd agent ~07:30 JST on trading days.
# On Monday it also appends a weekly `diff --days 7`.
#
# It uses the cached session + cookie (no password, no trades — the trading gate
# stays off here). Output is appended to ~/.paypay-sec/snapshot-cron.log.
#
# Install/uninstall the schedule with: bin/snapshot-cron.sh install | uninstall
set -uo pipefail

# launchd runs with a bare environment — make uv & friends findable.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
unset VIRTUAL_ENV

# Resolve the skill dir from this script's location (works for repo or install).
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HOME/.paypay-sec/snapshot-cron.log"
mkdir -p "$HOME/.paypay-sec"

# Skip weekends (US market closed Sat/Sun; nothing new to snapshot). 6=Sat 7=Sun.
dow="$(date +%u)"
if [ "$dow" = "6" ] || [ "$dow" = "7" ]; then
  exit 0
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M %Z') daily snapshot ==="
  # default account
  uv run --project "$SKILL_DIR" paypay snapshot save 2>&1
  # each named profile ~/.paypay-sec/<name>.env (the *.env glob skips the dotfile .env)
  for envf in "$HOME"/.paypay-sec/*.env; do
    [ -e "$envf" ] || continue
    name="$(basename "$envf" .env)"
    echo "--- snapshot -a $name ---"
    uv run --project "$SKILL_DIR" paypay snapshot save -a "$name" 2>&1
  done
  # rebuild the household 每日涨跌日历 from the accumulated snapshots
  echo "--- rebuild P&L calendar ---"
  uv run --project "$SKILL_DIR" paypay calendar --out "$HOME/.paypay-sec/pnl-calendar.html" 2>&1
  if [ "$dow" = "1" ]; then
    echo "--- weekly diff (--days 7) ---"
    uv run --project "$SKILL_DIR" paypay diff --days 7 2>&1
  fi
  echo ""
} >> "$LOG" 2>&1
