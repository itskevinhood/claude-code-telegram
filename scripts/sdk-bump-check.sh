#!/bin/bash
# Weekly (cron): test the newest claude-agent-sdk against the bot, commit the bump
# to main, and ask Kevin via Bolt to deploy it. The SDK bundles the Claude Code CLI,
# so this is also what keeps the bot's "CLI default" model current.
#
# Never touches the live venv or the service — tools/deploy-sdk.sh does that, and
# only when Kevin says so (restarting live infra is ask-first).
#
# Env: MONITOR_DRY_RUN=1  print alerts instead of sending
#      SDK_DRY_RUN=1      run the checks but revert instead of commit/push
#      SDK_TARGET=x.y.z   test this version instead of PyPI's latest
set -uo pipefail

REPO=/home/ubuntu/claude-code-telegram
LIVE_VENV=/home/ubuntu/.cache/pypoetry/virtualenvs/claude-code-telegram-ny9yruGr-py3.11
STATE="$REPO/state"
NOTIFIED="$STATE/sdk-bump-notified"   # last version Kevin was told about (ok or failed)
LOG="$STATE/sdk-bump-check.log"
export PATH="/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p "$STATE"
exec >>"$LOG" 2>&1
echo "=== $(date -Is) sdk-bump-check"

BOLT_ENV="/home/ubuntu/.config/bolt/telegram.env"
[ -r "$BOLT_ENV" ] && { set -a; . "$BOLT_ENV"; set +a; }
notify() {
  if [ "${MONITOR_DRY_RUN:-}" = "1" ]; then echo "[DRY RUN] would send: $1"; return; fi
  curl -fsS -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:-}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID:-}" --data-urlencode "text=$1" >/dev/null \
    || echo "telegram send failed"
}

cd "$REPO" || exit 1
# Cron's PATH resolves python3 to the system 3.10; pin poetry to its 3.11 env.
poetry env use -q /usr/bin/python3.11 || { notify "⚠️ sdk-bump-check: poetry env unavailable. Log: $LOG"; exit 1; }
PY="$(poetry env info -p)/bin/python"
locked() { "$PY" -c 'import tomllib;print(next(p["version"] for p in tomllib.load(open("poetry.lock","rb"))["package"] if p["name"]=="claude-agent-sdk"))'; }

LATEST="${SDK_TARGET:-$(curl -fsS -m 30 https://pypi.org/pypi/claude-agent-sdk/json \
  | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["info"]["version"])')}"
LIVE="$("$LIVE_VENV/bin/python" -c 'import importlib.metadata as m;print(m.version("claude-agent-sdk"))')"
LOCKED="$(locked)"
echo "latest=$LATEST locked=$LOCKED live=$LIVE"
[ -n "$LATEST" ] || { notify "⚠️ sdk-bump-check: couldn't read PyPI. Log: $LOG"; exit 1; }

if [ "$LATEST" = "$LIVE" ]; then echo "up to date"; exit 0; fi
if [ "$(cat "$NOTIFIED" 2>/dev/null)" = "$LATEST" ]; then echo "already notified about $LATEST"; exit 0; fi

if [ "$LATEST" = "$LOCKED" ]; then
  notify "🔄 Bolt SDK $LATEST is committed on main but not deployed (live: $LIVE). To deploy, ask Claude to run ~/claude-code-telegram/tools/deploy-sdk.sh"
  echo "$LATEST" >"$NOTIFIED"; exit 0
fi

# --- Bump path: needs a clean main so the commit contains only the bump.
if [ -n "$(git status --porcelain)" ] || [ "$(git branch --show-current)" != main ]; then
  notify "⚠️ sdk-bump-check: claude-agent-sdk $LATEST is out, but ~/claude-code-telegram isn't a clean main checkout, so I skipped testing it."
  exit 1
fi
git pull -q --ff-only || { notify "⚠️ sdk-bump-check: git pull failed. Log: $LOG"; exit 1; }

revert() { git checkout -q -- pyproject.toml poetry.lock; poetry install -q --no-root; }
fail() {
  echo "FAILED: $1"; revert
  notify "❌ claude-agent-sdk $LATEST failed the bot's checks ($1). Nothing changed; the bot stays on $LIVE. Log: $LOG"
  echo "$LATEST" >"$NOTIFIED"; exit 1
}

poetry add -q "claude-agent-sdk@^$LATEST" || fail "poetry add"
CLI="$("$(dirname "$PY")/../lib/python3.11/site-packages/claude_agent_sdk/_bundled/claude" --version 2>/dev/null | awk '{print $1}')"

# Private SDK internals the bot imports (see the 0.2.104 bump commit).
"$PY" -c '
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
import inspect; assert "_query" in inspect.getsource(ClaudeSDKClient)
ClaudeAgentOptions(effort="medium")' || fail "SDK internals changed"

TESTS="$(poetry run pytest -q -p no:cacheprovider --no-cov 2>&1 | tail -1)"
echo "$TESTS"
echo "$TESTS" | grep -q "passed" && ! echo "$TESTS" | grep -qE "failed|error" || fail "tests: $TESTS"

# Live smoke query with no model set = what the bot gets with CLAUDE_MODEL empty.
SMOKE_DIR="$(mktemp -d)"
SMOKE="$(cd "$SMOKE_DIR" && timeout 180 "$PY" - <<'EOF'
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage
async def main():
    model, ok = "?", False
    async for m in query(prompt="Reply with just: ok",
                         options=ClaudeAgentOptions(max_turns=1, setting_sources=["project"])):
        if isinstance(m, AssistantMessage): model = m.model
        if isinstance(m, ResultMessage): ok = not m.is_error
    print(model if ok else "ERROR")
anyio.run(main)
EOF
)"
rm -rf "$SMOKE_DIR"
echo "smoke: $SMOKE"
{ [ -n "$SMOKE" ] && [ "$SMOKE" != ERROR ]; } || fail "live smoke query"

if [ "${SDK_DRY_RUN:-}" = "1" ]; then
  echo "SDK_DRY_RUN: reverting instead of committing"; revert
  notify "[dry run] claude-agent-sdk $LATEST (Claude Code $CLI) passed: $TESTS; default model $SMOKE"
  exit 0
fi

git commit -q -m "chore: bump claude-agent-sdk to $LATEST

Automated by scripts/sdk-bump-check.sh: bundled Claude Code $CLI, SDK
internals present, $TESTS, live smoke query ok (default model $SMOKE)." \
  -- pyproject.toml poetry.lock || fail "git commit"
git push -q origin main || { notify "⚠️ sdk-bump-check: committed $LATEST locally but push failed. Log: $LOG"; exit 1; }

notify "✅ Bolt SDK update ready: claude-agent-sdk $LIVE → $LATEST (Claude Code $CLI). $TESTS; default model is $SMOKE. Committed $(git rev-parse --short HEAD) to main. To deploy (restarts Bolt), ask Claude to run ~/claude-code-telegram/tools/deploy-sdk.sh"
echo "$LATEST" >"$NOTIFIED"
