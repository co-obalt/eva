"""Scheduler (doc 10 Phase 7): reminders + interval jobs persisted in SQLite.

Reminders fire as bus events (REMINDER_FIRED) — the CLI, voice and avatar are
pure subscribers; no LLM conversation is ever held open for a reminder.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable

from ..storage import Database


class Scheduler:
    def __init__(self, db: Database, bus=None, on_task: Callable | None = None,
                 tick_s: float = 5.0):
        self.db = db
        self.bus = bus
        self.on_task = on_task          # async callable(request_text, source) for scheduled tasks
        self.tick_s = tick_s
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ jobs
    def add_reminder(self, minutes: float, text: str) -> str:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        spec = {"text": text}
        next_run = time.time() + max(0.001, minutes) * 60.0
        self.db.ex(
            "INSERT INTO scheduled_jobs(id, kind, spec_json, next_run_at, last_run, enabled, created_at)"
            " VALUES (?,?,?,?,?,1,?)",
            (job_id, "reminder", json.dumps(spec), next_run, None, time.time()))
        return job_id

    def add_cron(self, request_text: str, every_minutes: float) -> str:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        spec = {"request": request_text, "every_minutes": every_minutes}
        next_run = time.time() + max(0.5, every_minutes) * 60.0
        self.db.ex(
            "INSERT INTO scheduled_jobs(id, kind, spec_json, next_run_at, last_run, enabled, created_at)"
            " VALUES (?,?,?,?,?,1,?)",
            (job_id, "interval", json.dumps(spec), next_run, None, time.time()))
        return job_id

    def list_jobs(self) -> list[dict]:
        rows = self.db.q("SELECT * FROM scheduled_jobs WHERE enabled=1 ORDER BY next_run_at")
        return [dict(r) for r in rows]

    def cancel(self, job_id: str) -> bool:
        row = self.db.q1("SELECT id FROM scheduled_jobs WHERE id=?", (job_id,))
        if not row:
            return False
        self.db.ex("UPDATE scheduled_jobs SET enabled=0 WHERE id=?", (job_id,))
        return True

    # ------------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_s)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    async def _tick(self) -> None:
        now = time.time()
        due = self.db.q("SELECT * FROM scheduled_jobs WHERE enabled=1 AND next_run_at<=?", (now,))
        for row in due:
            job = dict(row)
            spec = json.loads(job["spec_json"] or "{}")
            if job["kind"] == "reminder":
                if self.bus is not None:
                    self.bus.emit("REMINDER_FIRED", job_id=job["id"],
                                  text=spec.get("text", "reminder"), source="schedule")
                self.db.ex("UPDATE scheduled_jobs SET enabled=0, last_run=? WHERE id=?",
                           (now, job["id"]))
            elif job["kind"] == "interval":
                if self.on_task is not None:
                    text = spec.get("request", "")
                    try:
                        await self.on_task(text, "schedule")
                    except Exception:
                        pass
                every = float(spec.get("every_minutes") or 60.0)
                self.db.ex("UPDATE scheduled_jobs SET next_run_at=?, last_run=? WHERE id=?",
                           (now + every * 60.0, now, job["id"]))
