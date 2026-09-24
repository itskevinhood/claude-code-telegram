#!/bin/bash
# Manual (Kevin-approved): install the claude-agent-sdk version pinned in poetry.lock
# into the venv the live service runs, then restart Bolt. Safe to run from inside a
# Bolt session: the restart runs in a detached systemd unit that survives the bot
# going down, and it reports back to Telegram when the bot is up again.
#
# Rollback: git revert the bump commit, then run this again.
set -euo pipefail

REPO=/home/ubuntu/claude-code-telegram
LIVE_VENV=/home/ubuntu/.cache/pypoetry/virtualenvs/claude-code-telegram-ny9yruGr-py3.11
SERVICE=claude-telegram-bot

BOLT_ENV="/home/ubuntu/.config/bolt/telegram.env"
[ -r "$BOLT_ENV" ] && { set -a; . "$BOLT_ENV"; set +a; }
notify() {
  curl -fsS -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:-}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID:-}" --data-urlencode "text=$1" >/dev/null || true
}
live_version() { "$LIVE_VENV/bin/python" -c 'import importlib.metadata as m;print(m.version("claude-agent-sdk"))'; }

if [ "${1:-}" = "--restart" ]; then
  # Second stage, inside the detached unit.
  sudo systemctl restart "$SERVICE"
  sleep 20
  if systemctl is-active -q "$SERVICE"; then
    notify "✅ Bolt restarted on claude-agent-sdk $(live_version). Send /new first — a resumed session keeps the model it started on — then ask 'what model are you?'."
  else
    notify "❌ Bolt failed to come back after the SDK deploy. Check: journalctl -u $SERVICE -n 50"
  fi
  exit 0
fi

cd "$REPO"
TARGET="$("$LIVE_VENV/bin/python" -c 'import tomllib;print(next(p["version"] for p in tomllib.load(open("poetry.lock","rb"))["package"] if p["name"]=="claude-agent-sdk"))')"
echo "Installing claude-agent-sdk $TARGET into the live venv (was $(live_version))"
"$LIVE_VENV/bin/pip" install -q "claude-agent-sdk==$TARGET"
[ "$(live_version)" = "$TARGET" ] || { echo "install did not take"; exit 1; }

sudo systemd-run -q --uid=ubuntu --unit="bolt-sdk-restart-$(date +%s)" --on-active=5 \
  "$REPO/tools/deploy-sdk.sh" --restart
echo "Installed $TARGET. Bolt restarts in ~5s and will report back on Telegram."
