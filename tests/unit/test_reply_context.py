"""Tests for _with_reply_context: replying to a message gives Claude that message."""

from datetime import UTC, datetime
from types import SimpleNamespace

from src.bot.orchestrator import REPLY_CONTEXT_MAX_CHARS, _with_reply_context


def _msg(reply_to=None):
    return SimpleNamespace(reply_to_message=reply_to)


def _replied(text=None, caption=None, name="Bolt"):
    return SimpleNamespace(
        text=text,
        caption=caption,
        from_user=SimpleNamespace(first_name=name),
        date=datetime(2026, 9, 24, 19, 0, tzinfo=UTC),
    )


def test_no_reply_returns_text_unchanged():
    assert _with_reply_context(_msg(), "hi") == "hi"


def test_reply_prefixes_quoted_alert():
    out = _with_reply_context(_msg(_replied("❌ backup failed: 401")), "what's wrong?")
    assert out.startswith(
        "[Replying to a message from Bolt, sent 2026-09-24T19:00:00+00:00:]"
    )
    assert "<replied_message>\n❌ backup failed: 401\n</replied_message>" in out
    assert out.endswith("\n\nwhat's wrong?")


def test_caption_used_when_no_text():
    out = _with_reply_context(_msg(_replied(caption="chart of errors")), "explain")
    assert "chart of errors" in out


def test_empty_replied_message_is_ignored():
    assert _with_reply_context(_msg(_replied()), "hi") == "hi"


def test_long_replied_message_is_truncated():
    out = _with_reply_context(
        _msg(_replied("x" * (REPLY_CONTEXT_MAX_CHARS + 50))), "hi"
    )
    assert "x" * REPLY_CONTEXT_MAX_CHARS + "\n[…truncated]" in out
    assert "x" * (REPLY_CONTEXT_MAX_CHARS + 1) not in out
