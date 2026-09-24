"""Read-only tool policy for alert triage sessions.

Triage runs Claude on alert text that can contain outside content (email subjects,
API errors, log lines), so its tools are locked down in three layers:

1. ``SETTINGS``: deny rules for secret paths plus ``ask`` rules that send *every*
   Bash/Read/Glob/Grep call to ``can_use_tool``. Without the ``ask`` rules the CLI
   auto-approves commands it considers read-only, and recursive grep or globs then
   read denied files (verified 2026-09-24).
2. ``can_use_tool``: deny by default. Bash must pass ``check_bash`` (read-only
   allowlist, no recursion, globs, redirects or variables); Read must not touch a
   secret path.
3. ``DISALLOWED_TOOLS``: write, network and sub-agent tools are removed outright.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

HOME = Path.home()

DISALLOWED_TOOLS = [
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "Agent",
    "WebFetch",
    "WebSearch",
    "Grep",  # recursive by default; bash grep on named files is checked instead
]

# Secret locations (CLAUDE.md / SECRETS.md). Checked on resolved paths.
_SECRET_NAMES = {
    ".env",
    "auth-profiles.json",
    "tokens.json",
    ".credentials.json",
    ".claude.json",
}
_SECRET_SUFFIXES = (".pem", ".key")
_SECRET_DIRS = [
    HOME / ".config" / "bolt",
    HOME / ".config" / "anthropic",
    HOME / ".ssh",
    Path("/etc/whoop-mcp"),
    Path("/var/lib/whoop-mcp"),
    Path("/proc"),
]

SETTINGS = json.dumps(
    {
        "permissions": {
            "deny": [
                "Read(**/.env)",
                "Read(**/auth-profiles.json)",
                "Read(**/tokens.json)",
                "Read(**/*.pem)",
                "Read(~/.claude/.credentials.json)",
                "Read(~/.claude.json)",
                "Read(~/.config/bolt/**)",
                "Read(~/.config/anthropic/**)",
                "Read(~/.ssh/**)",
                "Read(//etc/whoop-mcp/**)",
                "Read(//var/lib/whoop-mcp/**)",
                "Read(//proc/**)",
                "Bash(env:*)",
                "Bash(printenv:*)",
                "Bash(sudo:*)",
            ],
            "ask": ["Bash", "Read", "Glob", "Grep"],
        }
    }
)


def is_secret_path(raw: str, cwd: Path) -> bool:
    """True if ``raw`` (as written in a command or tool call) points at a secret."""
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = cwd / p
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        return True
    if resolved.name in _SECRET_NAMES or resolved.name.endswith(_SECRET_SUFFIXES):
        return True
    return any(resolved == d or d in resolved.parents for d in _SECRET_DIRS)


# ── Bash ─────────────────────────────────────────────────────────────────────

# Commands that only read, with any flags that would make them write or hang.
_READ_ONLY: Dict[str, set] = {
    "cat": set(),
    "head": set(),
    "tail": {"-f", "-F", "--follow", "--retry"},
    "grep": {
        "-r",
        "-R",
        "--recursive",
        "--dereference-recursive",
        "-d",
        "--directories",
    },
    "egrep": {"-r", "-R", "--recursive"},
    "zgrep": {"-r", "-R"},
    "zcat": set(),
    "ls": set(),
    "wc": set(),
    "stat": set(),
    "file": set(),
    "du": set(),
    "df": set(),
    "free": set(),
    "uptime": set(),
    "date": set(),
    "id": set(),
    "whoami": set(),
    "hostname": set(),
    "which": set(),
    "nproc": set(),
    "sort": {"-o", "--output"},
    "uniq": set(),
    "cut": set(),
    "tr": set(),
    "basename": set(),
    "dirname": set(),
    "realpath": set(),
    "readlink": set(),
    "diff": set(),
    "echo": set(),
    "pgrep": set(),
    "ss": {"-K", "--kill"},
    "cd": set(),
    "true": set(),
    "journalctl": {
        "-f",
        "--follow",
        "--rotate",
        "--flush",
        "--sync",
        "--relinquish-var",
        "--smart-relinquish-var",
        "--setup-keys",
        "--update-catalog",
    },
    "find": {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    },
}
_SYSTEMCTL_READ = {
    "status",
    "is-active",
    "is-failed",
    "is-enabled",
    "show",
    "cat",
    "list-units",
    "list-timers",
    "list-unit-files",
    "list-dependencies",
}
_GIT_READ = {
    "log",
    "status",
    "diff",
    "show",
    "rev-parse",
    "describe",
    "shortlog",
    "ls-files",
    "blame",
}
_GIT_BRANCH_FLAGS = {
    "-a",
    "-r",
    "-v",
    "-vv",
    "--all",
    "--list",
    "--show-current",
    "--remotes",
}
_CURL_FLAGS = {
    "-s",
    "-S",
    "-sS",
    "-f",
    "-fsS",
    "-sf",
    "-I",
    "-i",
    "--silent",
    "--show-error",
    "--fail",
    "--head",
}
_CURL_VALUE_FLAGS = {"-m", "--max-time", "-w", "--write-out"}
_NO_FILE_ARGS = {"uniq", "tr"}  # only as filters in a pipe (uniq IN OUT writes OUT)
_FORBIDDEN_CHARS = (">", "<", "$", "`", "\n", "\\", "{")
_GLOB_CHARS = set("*?[")
_SEPARATORS = {"|", "||", "&&", ";"}


def _segments(command: str) -> Optional[List[List[str]]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segs: List[List[str]] = [[]]
    for tok in tokens:
        if tok in _SEPARATORS:
            segs.append([])
        elif set(tok) <= set("|&;"):
            return None  # "&" backgrounding or odd operators
        else:
            segs[-1].append(tok)
    return [s for s in segs if s] or None


def _unquoted_glob(command: str) -> bool:
    """True if a glob character appears outside quotes (the shell would expand it)."""
    quote = None
    for ch in command:
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _GLOB_CHARS:
            return True
    return False


def _check_segment(seg: List[str], cwd: Path) -> Optional[str]:
    """Return a reason string if the segment is not allowed, else None."""
    cmd, args = os.path.basename(seg[0]), seg[1:]
    for a in args:
        value = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        if value and not value.startswith("-") and is_secret_path(value, cwd):
            return f"'{value}' is a secret path"
    positional = [a for a in args if not a.startswith("-")]
    if cmd == "systemctl":
        sub = positional[0] if positional else None
        return None if sub in _SYSTEMCTL_READ else f"systemctl {sub} is not read-only"
    if cmd == "git":
        rest = list(args)
        while rest[:1] == ["-C"] and len(rest) > 1:
            rest = rest[2:]
        if any(
            a == "-c" or a.startswith("--output") or a == "--ext-diff" for a in rest
        ):
            return "git config/output flags are not allowed"
        sub, sub_args = (rest[0], rest[1:]) if rest else (None, [])
        if sub == "branch":
            return (
                None
                if all(a in _GIT_BRANCH_FLAGS for a in sub_args)
                else "only listing branches"
            )
        if sub == "remote":
            listing = not sub_args or sub_args in (["-v"], ["--verbose"])
            lookup = sub_args[:1] in (["show"], ["get-url"]) and not any(
                a.startswith("-") for a in sub_args[1:]
            )
            return None if listing or lookup else "only listing remotes"
        return None if sub in _GIT_READ else "only read-only git subcommands"
    if cmd == "ps":
        # BSD-style "e" (ps e, ps auxe, ps eww) prints each process's environment,
        # which for Bolt includes its .env secrets. "-e" (all processes) is fine.
        if any(not a.startswith("-") and "e" in a for a in args):
            return "ps with BSD 'e' (environment) is not allowed"
        return None
    if cmd == "crontab":
        return None if args == ["-l"] else "only 'crontab -l'"
    if cmd == "curl":
        value_idx = {i + 1 for i, a in enumerate(args) if a in _CURL_VALUE_FLAGS}
        for i, a in enumerate(args):
            if (
                i not in value_idx
                and a.startswith("-")
                and a not in _CURL_FLAGS | _CURL_VALUE_FLAGS
            ):
                return f"curl {a} is not allowed"
        targets = [
            a
            for i, a in enumerate(args)
            if i not in value_idx and not a.startswith("-")
        ]
        local = all(
            re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", u)
            for u in targets
        )
        return None if targets and local else "curl: GET to 127.0.0.1/localhost only"
    if cmd not in _READ_ONLY:
        return f"'{cmd}' is not on the read-only allowlist"
    if cmd in _NO_FILE_ARGS and len(positional) > (2 if cmd == "tr" else 0):
        return f"{cmd} only as a filter in a pipe"
    if cmd == "file" and any(a in ("-C", "--compile") for a in args):
        return "file -C is not allowed"
    bad = _READ_ONLY[cmd]
    for a in args:
        flag = a.split("=", 1)[0]
        if flag in bad or (cmd == "journalctl" and flag.startswith("--vacuum")):
            return f"{cmd} {flag} is not allowed"
        if cmd in ("grep", "egrep", "zgrep") and re.match(r"^-[a-zA-Z]*[rR]", a):
            return "recursive grep is not allowed; grep named files instead"
    return None


def check_bash(command: str, cwd: Path) -> Tuple[bool, str]:
    """Decide whether a triage Bash command is allowed. Returns (allowed, reason)."""
    if any(c in command for c in _FORBIDDEN_CHARS):
        return False, "redirects, variables, substitutions and escapes are not allowed"
    if _unquoted_glob(command):
        return False, "unquoted glob patterns are not allowed; name files explicitly"
    segs = _segments(command)
    if not segs:
        return False, "could not parse the command"
    here = cwd
    for seg in segs:
        reason = _check_segment(seg, here)
        if reason:
            return False, reason
        if os.path.basename(seg[0]) == "cd":  # later segments resolve from the new dir
            target = Path(os.path.expanduser(seg[1])) if len(seg) > 1 else HOME
            here = (target if target.is_absolute() else here / target).resolve()
    return True, "ok"


def make_can_use_tool(cwd: Path) -> Any:
    """Deny-by-default permission callback for triage sessions."""

    async def can_use_tool(
        tool_name: str, tool_input: Dict[str, Any], context: Any
    ) -> Any:
        if tool_name == "Bash":
            ok, reason = check_bash(tool_input.get("command", ""), cwd)
            if ok:
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"Triage is read-only: {reason}.")
        if tool_name == "Read":
            path = tool_input.get("file_path", "")
            if path and not is_secret_path(path, cwd):
                return PermissionResultAllow()
            return PermissionResultDeny(message="Triage may not read secret files.")
        if tool_name in ("Glob", "TodoWrite"):
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"Triage is read-only: {tool_name} is not available."
        )

    return can_use_tool
