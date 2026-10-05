"""Phase 7 — scheduler persistence + watcher triggers (doc 10, doc 11 S9-ish)."""
from __future__ import annotations

import asyncio
import time

from eva.core.events import EventBus
from eva.runtime.scheduler import Scheduler
from eva.runtime.watchers import WatcherService
from eva.storage import Database


def test_reminder_persists_and_fires_without_a_conversation(tmp_path):
    db = Database(tmp_path / "s.db")
    bus = EventBus()
    fired: list = []
    bus.subscribe(lambda ev: fired.append(ev) if ev.type == "REMINDER_FIRED" else None)

    s1 = Scheduler(db, bus=bus)
    s1.add_reminder(0.01, "stretch your legs")          # 0.01 min = 0.6 s

    # "restart": a brand new Scheduler instance on the same DB still sees the job
    s2 = Scheduler(db, bus=bus)
    assert len(s2.list_jobs()) == 1

    time.sleep(0.7)
    asyncio.run(s2._tick())
    assert fired and fired[0].payload["text"] == "stretch your legs"
    assert s2.list_jobs() == []                          # one-shot: disabled after firing


def test_watcher_fires_on_new_file_with_kill_switch(tmp_path):
    db = Database(tmp_path / "w.db")
    bus = EventBus()
    triggered: list = []
    bus.subscribe(lambda ev: triggered.append(ev) if ev.type == "WATCHER_TRIGGERED" else None)

    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    svc = WatcherService(db, bus=bus, tick_s=0.01)
    svc.add_rule(str(watch_dir), "*")

    asyncio.run(svc._tick())                             # baseline scan
    (watch_dir / "new_report.pdf").write_text("x")
    asyncio.run(svc._tick())
    assert triggered and triggered[0].payload["file"].endswith("new_report.pdf")

    svc.set_enabled(False)
    (watch_dir / "second.pdf").write_text("x")
    before = len(triggered)
    asyncio.run(svc._tick())
    assert len(triggered) == before                      # kill switch holds
