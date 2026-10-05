"""Watchers (doc 10 Phase 7): new-file triggers -> LOW-risk bus events.

Rules live in SQLite; one polling loop (>=1 s cadence, doc 08 §3 forbids busy
loops). Kill switch: `set_enabled(False)` or `/watch off` in the CLI.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..storage import Database


class WatcherService:
    def __init__(self, db: Database, bus=None, tick_s: float = 2.0):
        self.db = db
        self.bus = bus
        self.tick_s = tick_s
        self.enabled = True
        self._stop = asyncio.Event()
        self._known: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ rules
    def add_rule(self, directory: str, pattern: str = "*") -> str:
        rule_id = f"watch_{uuid.uuid4().hex[:8]}"
        spec = {"directory": directory, "pattern": pattern}
        self.db.ex(
            "INSERT INTO watch_rules(id, kind, spec_json, enabled, created_at, last_fired)"
            " VALUES (?,?,?,1,?,NULL)", (rule_id, "new_file", json.dumps(spec), time.time()))
        return rule_id

    def list_rules(self) -> list[dict]:
        rows = self.db.q("SELECT * FROM watch_rules WHERE enabled=1")
        return [dict(r) for r in rows]

    def remove_rule(self, rule_id: str) -> bool:
        row = self.db.q1("SELECT id FROM watch_rules WHERE id=?", (rule_id,))
        if not row:
            return False
        self.db.ex("UPDATE watch_rules SET enabled=0 WHERE id=?", (rule_id,))
        return True

    def set_enabled(self, on: bool) -> None:
        self.enabled = on

    # ------------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        while not self._stop.is_set():
            if self.enabled:
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
        if not self.enabled:
            return
        for rule in self.list_rules():
            spec = json.loads(rule["spec_json"] or "{}")
            directory = Path(spec.get("directory", ""))
            pattern = spec.get("pattern") or "*"
            if not directory.is_dir():
                continue
            try:
                current = {str(p) for p in await asyncio.to_thread(lambda: list(directory.glob(pattern)))}
            except OSError:
                continue
            known = self._known.setdefault(rule["id"], set(current))
            new = current - known
            if new:
                self._known[rule["id"]] = known | new
                if self.bus is not None:
                    for f in sorted(new)[:5]:
                        self.bus.emit("WATCHER_TRIGGERED", rule=rule["id"],
                                      file=f, source="event")
                self.db.ex("UPDATE watch_rules SET last_fired=? WHERE id=?",
                           (time.time(), rule["id"]))
