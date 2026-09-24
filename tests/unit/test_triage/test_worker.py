"""TriageWorker gates, replies and queue handling (fake bot + fake runner)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.config.settings import Settings
from src.triage import worker as worker_mod
from src.triage.runner import TriageResult
from src.triage.usage import usage_tracker
from src.triage.worker import TriageWorker


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(
        self, chat_id, text, reply_parameters=None, link_preview_options=None
    ):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_parameters.message_id if reply_parameters else None,
            }
        )
        return SimpleNamespace(message_id=1000 + len(self.sent))


def _settings(tmp_path, **kw):
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        allowed_users=[42],
        triage_queue_dir=tmp_path / "queue",
        triage_db_path=tmp_path / "triage.db",
        **kw,
    )


def _event(**kw):
    ev = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "job": "automations/ghl-contact-backup",
        "severity": "error",
        "summary": "Backup failed",
        "details": "error: 401",
        "log": "/home/ubuntu/automations/ghl-contact-backup/backup.log",
        "heal": "ask",
        "fingerprint": "fp1",
        "telegram_message_id": 555,
        "telegram_chat_id": 42,
    }
    ev.update(kw)
    return ev


@pytest.fixture(autouse=True)
def _reset_usage():
    usage_tracker.latest = None
    yield
    usage_tracker.latest = None


@pytest.fixture
def make_worker(tmp_path):
    async def _make(runner=None, memory=10_000, **settings_kw):
        calls = []

        async def fake_runner(prompt, cfg):
            calls.append(prompt)
            return TriageResult(
                text="🔧 Fix available\nLikely cause: token expired",
                verdict="fix_available",
                cost_usd=0.25,
                session_id="sess-1",
            )

        cfg = _settings(tmp_path, **settings_kw)
        bot = FakeBot()
        w = TriageWorker(
            cfg, bot, runner=runner or fake_runner, memory_probe=lambda: memory
        )
        await w.start()
        await w.stop()  # drive tick()/handle() directly in tests
        return w, bot, calls

    return _make


async def test_triages_and_replies_in_thread(make_worker):
    w, bot, calls = await make_worker()
    assert await w.handle(_event()) == "done"
    assert len(calls) == 1 and "<alert>" in calls[0]
    assert bot.sent[0]["reply_to"] == 555 and bot.sent[0]["text"].startswith(
        "🩺 Triage\n🔧 Fix available"
    )
    row = await w.store.get(1)
    assert row["status"] == "done" and row["session_id"] == "sess-1"
    assert row["triage_message_id"] == 1001 and row["cost_usd"] == 0.25


async def test_info_and_heal_never_are_skipped(make_worker):
    w, bot, calls = await make_worker()
    assert await w.handle(_event(severity="info")) == "skipped"
    assert await w.handle(_event(heal="never")) == "skipped"
    assert not calls and not bot.sent


async def test_stale_alert_is_not_triaged(make_worker):
    w, bot, calls = await make_worker()
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat(timespec="seconds")
    assert await w.handle(_event(ts=old)) == "stale"
    assert not calls and not bot.sent


async def test_same_fingerprint_within_cooldown_is_duplicate(make_worker):
    w, bot, calls = await make_worker()
    await w.handle(_event())
    assert await w.handle(_event(telegram_message_id=556)) == "duplicate"
    assert len(calls) == 1
    assert bot.sent[-1]["reply_to"] == 556 and "Same issue" in bot.sent[-1]["text"]


async def test_daily_budget_pauses_with_one_notice(make_worker):
    w, bot, calls = await make_worker(triage_daily_budget_usd=0.2)
    await w.handle(_event())  # spends 0.25
    assert (
        await w.handle(_event(fingerprint="fp2", telegram_message_id=600)) == "paused"
    )
    assert (
        await w.handle(_event(fingerprint="fp3", telegram_message_id=601)) == "paused"
    )
    notices = [m for m in bot.sent if m["text"].startswith("⏸")]
    assert len(notices) == 1 and "budget" in notices[0]["text"]
    assert len(calls) == 1


async def test_plan_usage_pauses(make_worker):
    w, bot, calls = await make_worker()
    usage_tracker.record(
        SimpleNamespace(
            status="allowed",
            utilization=None,
            raw={
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.9, "resetsAt": 9_999_999_999}
                }
            },
        )
    )
    assert await w.handle(_event()) == "paused"
    assert not calls and "plan usage at 90%" in bot.sent[0]["text"]


async def test_runner_failure_replies_and_records_error(make_worker):
    async def boom(prompt, cfg):
        raise RuntimeError("cli died")

    w, bot, _ = await make_worker(runner=boom)
    assert await w.handle(_event()) == "error"
    assert "Triage failed (RuntimeError)" in bot.sent[0]["text"]
    assert (await w.store.get(1))["status"] == "error"


async def test_timeout_is_reported(make_worker):
    async def slow(prompt, cfg):
        raise asyncio.TimeoutError()

    w, bot, _ = await make_worker(runner=slow)
    assert await w.handle(_event()) == "error"
    assert "timed out" in bot.sent[0]["text"]


async def test_tick_claims_queue_file_and_removes_it(make_worker, tmp_path):
    w, bot, calls = await make_worker()
    q = tmp_path / "queue"
    (q / "a.json").write_text(json.dumps(_event()))
    assert await w.tick() is True
    assert not list(q.glob("*.json")) and not list((q / "processing").glob("*"))
    assert len(calls) == 1
    assert await w.tick() is False


async def test_low_memory_defers_queue(make_worker, tmp_path):
    w, bot, calls = await make_worker(memory=100)
    (tmp_path / "queue" / "a.json").write_text(json.dumps(_event()))
    assert await w.tick() is False
    assert list((tmp_path / "queue").glob("*.json")) and not calls


async def test_unreadable_event_is_dropped(make_worker, tmp_path):
    w, bot, calls = await make_worker()
    (tmp_path / "queue" / "bad.json").write_text("{not json")
    assert await w.tick() is True
    assert not list((tmp_path / "queue").glob("*.json")) and not calls


async def test_start_requeues_unfinished_claims(tmp_path):
    cfg = _settings(tmp_path)
    proc = tmp_path / "queue" / "processing"
    proc.mkdir(parents=True)
    (proc / "x.json").write_text("{}")
    w = TriageWorker(cfg, FakeBot())
    await w.start()
    await w.stop()
    assert (tmp_path / "queue" / "x.json").exists()


async def test_available_mb_reads_meminfo():
    assert worker_mod.available_mb() > 0
