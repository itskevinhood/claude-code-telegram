"""Alert triage worker: drains ~/bin/bolt-alert's queue, one alert at a time.

bolt-alert posts the alert to Telegram, then drops the event into
``triage_queue_dir``. This worker claims each file, applies the gates below, runs a
read-only triage session, and replies to the alert's Telegram message.

Gates, in order: info / heal=never (skip) → older than max_age (stale) → same
fingerprint triaged within cooldown (duplicate) → daily budget (paused) → plan
usage (paused). Low free RAM defers the whole queue (3.7 GB, OOM-averse box).
Nothing here may raise out of the loop: a failed triage must never take Bolt down.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import structlog
from telegram import Bot, LinkPreviewOptions, ReplyParameters
from telegram.error import TelegramError

from ..config.settings import Settings
from .prompt import build_prompt, recent_job_history
from .runner import TriageResult, run_triage
from .store import TriageStore
from .usage import usage_tracker

logger = structlog.get_logger()

TELEGRAM_LIMIT = 4000
NOTICE_INTERVAL_SECONDS = 3600

Runner = Callable[[str, Settings], Awaitable[TriageResult]]


def available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 1 << 30  # unknown: don't block


def _age_hours(ts: Optional[str]) -> float:
    try:
        sent = datetime.fromisoformat(ts or "")
    except ValueError:
        return 0.0
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return (datetime.now(UTC) - sent).total_seconds() / 3600


class TriageWorker:
    def __init__(
        self,
        config: Settings,
        bot: Bot,
        store: Optional[TriageStore] = None,
        runner: Runner = run_triage,
        poll_seconds: float = 5.0,
        memory_probe: Callable[[], int] = available_mb,
    ) -> None:
        self.config = config
        self.bot = bot
        self.store = store or TriageStore(config.triage_db_path)
        self.runner = runner
        self.poll_seconds = poll_seconds
        self.memory_probe = memory_probe
        self.queue = Path(config.triage_queue_dir)
        self.processing = self.queue / "processing"
        self._task: Optional[asyncio.Task[None]] = None
        self._last_notice: Dict[str, float] = {}
        self._low_memory_logged = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.processing.mkdir(parents=True, exist_ok=True)
        for f in self.processing.glob("*.json"):  # claimed but unfinished at last stop
            f.rename(self.queue / f.name)
        await self.store.init()
        self._task = asyncio.create_task(self._loop(), name="alert-triage")
        logger.info("Alert triage worker started", queue=str(self.queue))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let triage take Bolt down
                logger.exception("Alert triage tick failed")
            await asyncio.sleep(self.poll_seconds)

    # ── one step ───────────────────────────────────────────────────────────

    async def tick(self) -> bool:
        """Claim and handle the oldest queued alert. Returns True if one was handled."""
        files = sorted(self.queue.glob("*.json"))
        if not files:
            return False
        if self.memory_probe() < self.config.triage_min_available_mb:
            if not self._low_memory_logged:
                logger.warning(
                    "Alert triage waiting for free memory", queued=len(files)
                )
                self._low_memory_logged = True
            return False
        self._low_memory_logged = False
        claimed = self.processing / files[0].name
        try:
            files[0].rename(claimed)
        except FileNotFoundError:
            return False
        try:
            try:
                event = json.loads(claimed.read_text())
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Dropping unreadable triage event",
                    file=claimed.name,
                    error=str(exc),
                )
                return True
            await self.handle(event)
            return True
        finally:
            claimed.unlink(missing_ok=True)

    async def handle(self, event: Dict[str, Any]) -> str:
        """Gate, triage and reply for one event. Returns the final status."""
        cfg = self.config
        chat_id = event.get("telegram_chat_id") or (
            cfg.allowed_users[0] if cfg.allowed_users else None
        )
        event["telegram_chat_id"] = chat_id
        msg_id = event.get("telegram_message_id")

        if event.get("severity") == "info" or event.get("heal") == "never":
            await self.store.add(event, "skipped")
            return "skipped"
        if _age_hours(event.get("ts")) > cfg.triage_max_age_hours:
            await self.store.add(event, "stale")
            return "stale"

        previous = await self.store.recent_triage(
            event.get("fingerprint") or "", cfg.triage_cooldown_hours
        )
        if previous:
            await self.store.add(event, "duplicate")
            when = (previous.get("received_at") or "")[11:16]
            await self._reply(
                chat_id,
                msg_id,
                f"↩️ Same issue as the alert triaged at {when} UTC; see that thread. Not re-triaged.",
            )
            return "duplicate"

        spent = await self.store.cost_today()
        if spent >= cfg.triage_daily_budget_usd:
            await self.store.add(event, "paused")
            await self._notice(
                "budget",
                chat_id,
                msg_id,
                f"⏸ Not triaged: today's ${cfg.triage_daily_budget_usd:.2f} triage budget is used up"
                " (resets 00:00 UTC). Further alerts today won't get this note.",
            )
            return "paused"
        reason = usage_tracker.pause_reason(cfg.triage_pause_utilization)
        if reason:
            await self.store.add(event, "paused")
            await self._notice(
                "usage",
                chat_id,
                msg_id,
                f"⏸ Not triaged: {reason}. Triage resumes when usage drops.",
            )
            return "paused"

        row = await self.store.add(event, "triaging")
        history = recent_job_history(event.get("job") or "", exclude_message_id=msg_id)
        started = time.monotonic()
        try:
            result = await self.runner(build_prompt(event, history), cfg)
        except asyncio.TimeoutError:
            await self._fail(
                row,
                chat_id,
                msg_id,
                f"timed out after {cfg.triage_timeout_seconds // 60} min",
            )
            return "error"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Alert triage run failed", job=event.get("job"))
            await self._fail(row, chat_id, msg_id, type(exc).__name__)
            return "error"

        text = result.text or "(triage returned no text)"
        body = f"🩺 Triage\n{text}"
        if len(body) > TELEGRAM_LIMIT:
            body = body[: TELEGRAM_LIMIT - 20] + "\n…(truncated)"
        triage_msg = await self._reply(chat_id, msg_id, body)
        status = "error" if result.is_error else "done"
        await self.store.update(
            row,
            status=status,
            verdict=result.verdict,
            cost_usd=result.cost_usd,
            session_id=result.session_id,
            triage_message_id=triage_msg,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        logger.info(
            "Alert triaged",
            job=event.get("job"),
            verdict=result.verdict,
            cost_usd=round(result.cost_usd, 4),
            seconds=round(time.monotonic() - started),
        )
        return status

    # ── helpers ────────────────────────────────────────────────────────────

    async def _fail(self, row: int, chat_id: Any, msg_id: Any, why: str) -> None:
        await self.store.update(
            row,
            status="error",
            error=why,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        await self._reply(
            chat_id, msg_id, f"⚠️ Triage failed ({why}). The alert above still stands."
        )

    async def _notice(self, kind: str, chat_id: Any, msg_id: Any, text: str) -> None:
        now = time.monotonic()
        if (
            now - self._last_notice.get(kind, -NOTICE_INTERVAL_SECONDS)
            >= NOTICE_INTERVAL_SECONDS
        ):
            self._last_notice[kind] = now
            await self._reply(chat_id, msg_id, text)

    async def _reply(self, chat_id: Any, msg_id: Any, text: str) -> Optional[int]:
        if not chat_id:
            logger.warning("Alert triage has no chat to reply to")
            return None
        try:
            sent = await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_parameters=(
                    ReplyParameters(message_id=msg_id, allow_sending_without_reply=True)
                    if msg_id
                    else None
                ),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return int(sent.message_id)
        except TelegramError as exc:
            logger.warning("Alert triage reply failed", error=str(exc))
            return None
