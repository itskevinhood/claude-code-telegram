"""Latest plan-usage reading, from the SDK's rate_limit_event.

Bolt runs on Kevin's Max plan (OAuth), so there is no API bill to meter. Every
Claude run in this process (Kevin's chats and triage) emits a RateLimitEvent whose
raw payload carries per-window utilization, e.g.
``{"unifiedWindows": {"five_hour": {"utilization": 0.07, "resetsAt": ...}, ...}}``.
Triage pauses when any live window crosses the threshold, so it never eats the
quota Kevin uses interactively.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class UsageReading:
    status: str
    windows: Dict[str, Dict[str, float]] = field(default_factory=dict)
    recorded_at: float = 0.0


class UsageTracker:
    def __init__(self) -> None:
        self.latest: Optional[UsageReading] = None

    def record(self, info: Any) -> None:
        """Store a claude_agent_sdk RateLimitInfo. Never raises."""
        try:
            raw = getattr(info, "raw", None) or {}
            windows = {}
            for name, w in (raw.get("unifiedWindows") or {}).items():
                if isinstance(w, dict) and w.get("utilization") is not None:
                    windows[name] = {
                        "utilization": float(w["utilization"]),
                        "resets_at": float(w.get("resetsAt") or 0),
                    }
            if not windows and getattr(info, "utilization", None) is not None:
                windows[getattr(info, "rate_limit_type", None) or "unknown"] = {
                    "utilization": float(info.utilization),
                    "resets_at": float(getattr(info, "resets_at", 0) or 0),
                }
            self.latest = UsageReading(
                status=str(getattr(info, "status", "") or ""),
                windows=windows,
                recorded_at=time.time(),
            )
        except Exception:  # noqa: BLE001 — usage tracking must never break a run
            pass

    def pause_reason(
        self, threshold: float, now: Optional[float] = None
    ) -> Optional[str]:
        """Why triage should wait, or None. Windows that have already reset are ignored."""
        r = self.latest
        if r is None:
            return None
        now = now or time.time()
        live = {
            n: w
            for n, w in r.windows.items()
            if not w["resets_at"] or w["resets_at"] > now
        }
        if r.status in ("allowed_warning", "rejected") and live:
            return f"plan usage warning ({r.status})"
        for name, w in sorted(live.items()):
            if w["utilization"] >= threshold:
                return f"plan usage at {w['utilization']:.0%} of the {name.replace('_', '-')} window"
        return None


usage_tracker = UsageTracker()
