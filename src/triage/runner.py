"""Run one read-only triage session with the Claude Agent SDK.

Separate from ClaudeSDKManager on purpose: triage never touches Kevin's chat
sessions, runs its own system prompt and tool policy, and loads no settings files
(``~/.claude/settings.json`` auto-allows commands that would bypass the policy).
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
)

from ..config.settings import Settings
from .policy import DISALLOWED_TOOLS, HOME, SETTINGS, make_can_use_tool
from .prompt import SYSTEM_APPEND
from .usage import usage_tracker

logger = structlog.get_logger()

_SECRET_ENV = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|KEY|DSN|CREDENTIAL|WEBHOOK", re.I
)
_KEEP_ENV = ("ANTHROPIC_", "CLAUDE_")  # the CLI's own auth, if ever set via env


@dataclass
class TriageResult:
    text: str
    verdict: str
    cost_usd: float
    session_id: Optional[str]
    is_error: bool = False


def scrubbed_env() -> Dict[str, str]:
    """Blank out secret-looking variables the bot inherited from its .env.

    The SDK merges this over os.environ for the CLI subprocess, so triage's
    tools never see the bot's tokens even if a command slipped through.
    """
    return {
        k: ""
        for k in os.environ
        if _SECRET_ENV.search(k) and not k.startswith(_KEEP_ENV)
    }


def parse_verdict(text: str) -> str:
    first = text.strip().splitlines()[0].lower() if text.strip() else ""
    if "transient" in first:
        return "transient"
    if "fix available" in first:
        return "fix_available"
    if "needs you" in first:
        return "needs_you"
    return "unknown"


def build_options(config: Settings, cwd: Path = HOME) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=config.claude_model or None,
        effort=config.triage_effort,
        cwd=str(cwd),
        setting_sources=[],
        settings=SETTINGS,
        can_use_tool=make_can_use_tool(cwd),
        disallowed_tools=list(DISALLOWED_TOOLS),
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": SYSTEM_APPEND,
        },
        max_turns=config.triage_max_turns,
        max_budget_usd=config.triage_max_cost_per_run,
        env=scrubbed_env(),
    )


async def run_triage(prompt: str, config: Settings) -> TriageResult:
    """Run a triage session to completion (or timeout). Raises on SDK failure."""
    os.environ.pop("CLAUDECODE", None)  # not a nested session
    texts: List[str] = []
    result: Optional[ResultMessage] = None

    async def _run() -> None:
        nonlocal result
        async with ClaudeSDKClient(build_options(config)) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, RateLimitEvent):
                    usage_tracker.record(message.rate_limit_info)
                elif isinstance(message, AssistantMessage):
                    texts.extend(
                        b.text for b in message.content if isinstance(b, TextBlock)
                    )
                elif isinstance(message, ResultMessage):
                    result = message

    await asyncio.wait_for(_run(), timeout=config.triage_timeout_seconds)
    text = ((result.result if result else None) or (texts[-1] if texts else "")).strip()
    return TriageResult(
        text=text,
        verdict=parse_verdict(text),
        cost_usd=float(getattr(result, "total_cost_usd", 0) or 0),
        session_id=getattr(result, "session_id", None),
        is_error=bool(getattr(result, "is_error", False)) or not text,
    )
