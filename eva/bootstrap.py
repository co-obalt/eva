"""EvaApp — the composition root. Everything wired per doc 02 §5 (single writer,
single host process). Interfaces import this; they never build their own core.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .avatar.bus import AvatarBus
from .avatar.presence import Presence
from .config import Settings, get_settings
from .core.checkpoint import CheckpointStore
from .core.events import EventBus
from .core.orchestrator import Orchestrator, RunResult
from .core.risk import RiskGate
from .core.task import TaskStatus
from .memory.distiller import SkillDistiller
from .memory.store import MemoryStore
from .models.actor import Actor
from .models.planner import LlmPlanner
from .models.providers import ProviderUnavailable, build_default_pool
from .models.router import ModelRouter
from .persona.mood import Persona
from .perception.engine import PerceptionEngine
from .runtime.budget import ResourceMonitor
from .runtime.scheduler import Scheduler
from .runtime.watchers import WatcherService
from .storage import Database
from .tasks.manager import TaskManager
from .tools import build_registry
from .voice.tts import TTS


class EvaApp:
    def __init__(self, settings: Settings | None = None, *, confirm=None,
                 voice: bool = False, avatar: bool = False, speak: bool = False):
        self.settings = settings or get_settings()
        self.bus = EventBus()
        self.db = Database(self.settings.db_path)
        self.store = CheckpointStore(self.db)
        self.memory = MemoryStore(self.db, bus=self.bus)
        self.monitor = ResourceMonitor(self.settings, self.bus)
        self.persona = Persona(self.memory)
        self.bus.subscribe(self.persona.on_event)

        # providers + router (optional: degraded when unreachable — doc 08 §5)
        self.pool = None
        self.router: ModelRouter | None = None
        try:
            self.pool = build_default_pool(self.settings)
            self.router = ModelRouter(self.pool, store=self.store, bus=self.bus,
                                      settings=self.settings)
        except ProviderUnavailable:
            self.pool = None

        # perception + tools
        self.perception = PerceptionEngine(self.settings, self.bus, memory=self.memory)
        if self.router is not None:
            self.perception.vision.router = self.router

        # autonomy
        self.scheduler = Scheduler(self.db, bus=self.bus, on_task=self._scheduled_task)
        self.watchers = WatcherService(self.db, bus=self.bus)
        self.registry = build_registry(self.settings, self.perception, self.scheduler,
                                       memory=self.memory)

        # planning (frozen step list) + acting (M1: one action per cycle)
        self.planner = None
        self.actor = None
        if self.router is not None:
            self.planner = LlmPlanner(self.router, self.registry, bus=self.bus,
                                      settings=self.settings)
            self.actor = Actor(self.router, self.registry, memory=self.memory, bus=self.bus,
                               settings=self.settings,
                               menu_size=getattr(self.settings, "actor_menu_size", 14))

        # orchestrator
        self.gate = RiskGate(allowed_roots=self.settings.allowed_roots)
        self.orchestrator = Orchestrator(
            registry=self.registry, perception=self.perception, bus=self.bus,
            store=self.store, gate=self.gate, confirm=confirm, planner=self.planner,
            actor=self.actor, memory=self.memory, distiller=SkillDistiller(self.memory, self.bus),
            settings=self.settings, max_steps=self.settings.max_task_steps)

        # task manager
        self.manager = TaskManager(self.settings, self.memory, composer=self.planner,
                                   bus=self.bus, registry=self.registry)

        # optional presence
        self.avatar = AvatarBus(self.settings, event_bus=self.bus) if avatar else None
        # live "what am I doing" file — the seam for separate viewer processes
        self.presence = Presence(self.settings, bus=self.bus)
        self.voice = TTS(self.settings, avatar=self.avatar, bus=self.bus) if (voice or speak) else None
        self._speak = speak
        if speak:
            self.bus.subscribe(self._speak_events)

        self._service_tasks: list[asyncio.Task] = []
        self.last_result: RunResult | None = None
        self.pending_ask: dict[str, Any] | None = None   # task parked on agent.ask_user

    # ------------------------------------------------------------------ providers
    def providers_ok(self) -> bool:
        return bool(self.pool and self.pool.available())

    def mode(self) -> str:
        return self.monitor.check(self.providers_ok())

    # ------------------------------------------------------------------ run
    async def handle(self, text: str, source: str = "cli") -> tuple[RunResult | None, str | None]:
        """Single entry point for CLI / voice / schedule (doc 09 §1)."""
        self.mode()
        manager_out = await self.manager.handle(text, source)
        task, reply = manager_out
        if task is None:
            return None, reply
        if self.router is not None:
            self.router.bind_task(task)
        try:
            result = await self.orchestrator.run(task)
        finally:
            if self.router is not None:
                self.router.unbind()
        self.last_result = result
        self.pending_ask = self._pending_after(result)
        return result, None

    # ------------------------------------------------------------------ questions
    def _pending_after(self, result: RunResult | None) -> dict[str, Any] | None:
        """A task parked on agent.ask_user remembers its question, so the user's next
        line becomes the answer instead of a brand-new task."""
        if result is None:
            return None
        task = result.task
        for step in reversed(task.plan):
            if step.tool != "agent.ask_user":
                continue
            question = str(step.args.get("question") or "")
            answers = task.context.get("answers") or {}
            if question and question not in answers:
                return {"task_id": task.id, "question": question,
                        "options": [o for o in str(step.args.get("options") or "").split("|") if o]}
            return None
        return None

    async def answer_pending(self, text: str) -> tuple[RunResult | None, str | None]:
        """Record the answer as the ui.user_answer observation, then resume the task."""
        pending = self.pending_ask
        if pending is None:
            return None, None
        question = str(pending["question"])
        task = self.store.load_task(str(pending["task_id"]))
        if task is None:
            self.pending_ask = None
            return None, "That task is no longer in the store, so nothing was resumed."
        answers = dict(task.context.get("answers") or {})
        answers[question] = text
        task.context["answers"] = answers
        history = list(task.context.get("actor_history") or [])
        history.append({"step": "-", "tool": "agent.ask_user",
                        "action": f"ask_user({question[:80]!r})", "status": "ANSWERED",
                        "observed": f"user answered: {text[:200]}", "reason": question[:200]})
        task.context["actor_history"] = history[-40:]
        self.store.save_task(task)
        self.perception.note("ui.user_answer", text, "L0", args={"question": question})
        self.pending_ask = None
        result = await self.resume_task(task.id)
        return result, None

    async def resume_task(self, task_id: str) -> RunResult:
        if self.router is not None:
            task = self.store.load_task(task_id)
            if task is not None:
                self.router.bind_task(task)
        try:
            result = await self.orchestrator.resume(task_id)
        finally:
            if self.router is not None:
                self.router.unbind()
        self.last_result = result
        self.pending_ask = self._pending_after(result)
        return result

    async def _scheduled_task(self, text: str, source: str) -> None:
        try:
            await self.handle(text, source=source)
        except Exception:
            pass

    # ------------------------------------------------------------------ services
    async def start_services(self) -> list[asyncio.Task]:
        self._service_tasks = [
            asyncio.create_task(self.scheduler.run_forever(), name="scheduler"),
            asyncio.create_task(self.watchers.run_forever(), name="watchers"),
        ]
        return self._service_tasks

    async def close(self) -> None:
        self.scheduler.stop()
        self.watchers.stop()
        for t in self._service_tasks:
            t.cancel()
        if self.presence is not None:
            self.presence.close()
        await self.perception.close()
        try:
            self.db.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ voice
    def _speak_events(self, event) -> None:
        if self.voice is None:
            return
        text = None
        if event.type == "TASK_FINISHED" and event.payload.get("status") == "success":
            text = event.payload.get("message") or "Task finished and verified."
        elif event.type == "TASK_FAILED":
            text = "The task failed. I stopped instead of guessing."
        elif event.type == "TASK_WAITING_USER":
            text = event.payload.get("ask")
        elif event.type == "REMINDER_FIRED":
            text = f"Reminder: {event.payload.get('text')}"
        if not text:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.voice.say(str(text)[:300]))
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ reports
    def perf(self) -> dict[str, Any]:
        out = self.monitor.report()
        out.update({
            "providers": self.pool.status() if self.pool else [],
            "router": self.router.status() if self.router else None,
            "tools": len(self.registry.names()),
            "skills": len(self.memory.list_skills()),
            "rules": len(self.memory.list_rules()),
            "actor": bool(self.actor),
            "jobs": len(self.scheduler.list_jobs()),
            "avatar": self.avatar.status() if self.avatar else None,
            "vision": self.perception.state_view(),
        })
        return out

    def outputs_of(self, result: RunResult | None) -> list[dict]:
        if result is None:
            return []
        return list(result.task.context.get("tool_outputs") or [])
