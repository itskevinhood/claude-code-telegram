#!/bin/bash
# Cron every 5 min: is Bolt (claude-telegram-bot.service) alive? Alerts through the
# Telegram Bot API directly, so it works while the bot process is down.
#   - down on 2 consecutive checks -> alert, then a reminder hourly while down
#   - back up after an alert       -> one "recovered" message
#   - >= 3 restarts between checks -> crash-loop alert (systemd keeps restarting it)
# Never restarts the bot itself: restarting live infra is Kevin's call.
# Env: MONITOR_DRY_RUN=1 prints instead of sending.
set -uo pipefail

SERVICE=claude-telegram-bot
STATE=/home/ubuntu/claude-code-telegram/state
DOWN_COUNT="$STATE/watchdog-down-count"
LAST_ALERT="$STATE/watchdog-last-alert"      # epoch of last down alert; absent = not alerting
LAST_RESTARTS="$STATE/watchdog-restarts"
mkdir -p "$STATE"

# notify SEVERITY SUMMARY [DETAILS] [EMOJI] — via ~/bin/bolt-alert (docs/conventions/alerts.md).
# --heal never: Bolt cannot triage alerts about itself.
# MONITOR_DRY_RUN=1 is honored by bolt-alert itself.
notify() {
  /home/ubuntu/bin/bolt-alert --job claude-code-telegram --severity "$1" --summary "$2" --heal never \
    ${3:+--details "$3"} ${4:+--emoji "$4"} \
    || echo "$(date -Is) bolt-alert failed" >&2
}

now=$(date +%s)
state=$(systemctl is-active "$SERVICE" 2>/dev/null)

if [ "$state" = active ]; then
  echo 0 >"$DOWN_COUNT"
  if [ -f "$LAST_ALERT" ]; then
    rm -f "$LAST_ALERT"
    notify info "Bolt is back up" "" ✅
  fi
  restarts=$(systemctl show "$SERVICE" -p NRestarts --value)
  prev=$(cat "$LAST_RESTARTS" 2>/dev/null || echo "$restarts")
  echo "$restarts" >"$LAST_RESTARTS"
  if [ $((restarts - prev)) -ge 3 ]; then
    notify error "Bolt is crash-looping: $((restarts - prev)) restarts in the last 5 min" "Check: journalctl -u $SERVICE -n 50"
  fi
  exit 0
fi

count=$(( $(cat "$DOWN_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$count" >"$DOWN_COUNT"
[ "$count" -lt 2 ] && exit 0

last=$(cat "$LAST_ALERT" 2>/dev/null || echo 0)
if [ $((now - last)) -ge 3600 ]; then
  echo "$now" >"$LAST_ALERT"
  notify error "Bolt is down ($SERVICE: $state for ~$((count * 5)) min)" "Alerts still arrive, but nothing can answer them.
Check: journalctl -u $SERVICE -n 50
Restart: sudo systemctl restart $SERVICE"
fi
