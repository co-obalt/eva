"""Event bus (doc 09 §3). Async hosts call sync emit — dispatch is cheap and
subscriber errors never propagate to the core (P12: UI subscribers are safe)."""
from __future__ import annotations

import itertools
import time
from collections import deque
from typing import Any, Callable

from pydantic import BaseModel, Field


# -- catalogue (doc 09 §3) ---------------------------------------------------
TASK_CREATED = "TASK_CREATED"
TASK_PLAN_READY = "TASK_PLAN_READY"
TASK_STARTED = "TASK_STARTED"
TASK_STEP_START = "TASK_STEP_START"
TASK_STEP_RESULT = "TASK_STEP_RESULT"
TASK_STEP_SKIPPED = "TASK_STEP_SKIPPED"
TASK_REPLAN = "TASK_REPLAN"
ACTOR_ACTION = "ACTOR_ACTION"          # narration: the actor's one-line reason
TASK_ESCALATED = "TASK_ESCALATED"      # fast lane gave up -> general actor loop
RULE_ENFORCED = "RULE_ENFORCED"        # a remembered user rule rejected an action
TASK_WAITING_USER = "TASK_WAITING_USER"
TASK_FINISHED = "TASK_FINISHED"
TASK_FAILED = "TASK_FAILED"
TASK_CANCELLED = "TASK_CANCELLED"
TOOL_START = "TOOL_START"
TOOL_RESULT = "TOOL_RESULT"
OBSERVATION = "OBSERVATION"
STATE_CHANGED = "STATE_CHANGED"
RISK_GATE = "RISK_GATE"
RISK_ANSWER = "RISK_ANSWER"
POLICY_REJECTED = "POLICY_REJECTED"
DECLINED = "DECLINED"
EPISODE_WRITTEN = "EPISODE_WRITTEN"
SKILL_DISTILLED = "SKILL_DISTILLED"
SKILL_QUARANTINED = "SKILL_QUARANTINED"
MODE_CHANGED = "MODE_CHANGED"
MODEL_ROUTED = "MODEL_ROUTED"
BUDGET_WARN = "BUDGET_WARN"
REMINDER_FIRED = "REMINDER_FIRED"
WATCHER_TRIGGERED = "WATCHER_TRIGGERED"
IDEMPOTENT_SKIP = "IDEMPOTENT_SKIP"
AVATAR_STATE = "AVATAR_STATE"


class Event(BaseModel):
    id: int = 0
    ts: float = Field(default_factory=time.time)
    type: str
    task_id: str | None = None
    step: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


Subscriber = Callable[[Event], None]


class EventBus:
    """Sync fan-out with bounded replay log and per-subscriber drop counters."""

    def __init__(self, max_history: int = 4000):
        self._subs: list[tuple[Subscriber, int]] = []  # (fn, dropped)
        self.history: deque[Event] = deque(maxlen=max_history)
        self._ids = itertools.count(1)
        self.on_disk_log: Callable[[Event], None] | None = None

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append((fn, 0))

    def emit(self, type_: str, task_id: str | None = None, step: int | None = None,
             **payload: Any) -> Event:
        ev = Event(id=next(self._ids), type=type_, task_id=task_id, step=step, payload=payload)
        self.history.append(ev)
        for i, (fn, dropped) in enumerate(self._subs):
            try:
                fn(ev)
            except Exception:
                self._subs[i] = (fn, dropped + 1)
        if self.on_disk_log is not None:
            try:
                self.on_disk_log(ev)
            except Exception:
                pass
        return ev

    def dropped_counts(self) -> dict[str, int]:
        return {getattr(fn, "__name__", repr(fn)): d for fn, d in self._subs if d}

    def recent(self, limit: int = 50) -> list[Event]:
        return list(self.history)[-limit:]
