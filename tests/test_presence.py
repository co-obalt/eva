"""M1 surface — the local presence file: how separate viewer processes (pet,
dashboard) see what EVA is doing without coupling to the app."""
from __future__ import annotations

import json
import time

from eva.avatar.presence import Presence, read_presence, presence_path
from eva.config import Settings
from eva.core.events import EventBus


def _settings(tmp_path) -> Settings:
    return Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path)


def test_presence_reports_live_state_from_app_events(tmp_path):
    settings = _settings(tmp_path)
    bus = EventBus()
    p = Presence(settings, bus=bus)

    # the file exists the moment the app is up, before any event
    first = read_presence(presence_path(settings))
    assert first["state"] == "IDLE" and first["running"] is True
    assert first["stale"] is False and first["pid"] > 0

    bus.emit("TASK_STARTED", task_id="t1", goal="make a folder")
    bus.emit("ACTOR_ACTION", task_id="t1", kind="tool",
             action="files.mkdir({})", reason="first folder")
    bus.emit("TASK_STEP_RESULT", task_id="t1", step=1, status="VERIFIED",
             effect="files.exists")

    live = read_presence(presence_path(settings))
    assert live["state"] == "SUCCESS"
    assert live["headline"] == "files.exists"          # the verified effect
    assert live["mood"].startswith("#") and live["stale"] is False

    p.close()
    closed = read_presence(presence_path(settings))
    assert closed["running"] is False and closed["stale"] is True
    # the last thing EVA did survives the shutdown, for viewers to show
    assert closed["headline"] == "files.exists"


def test_presence_stale_and_missing_files_are_honest(tmp_path):
    path = presence_path(_settings(tmp_path))
    # nothing has ever run
    absent = read_presence(path)
    assert absent["state"] == "NONE" and absent["stale"] is True
    # a process wrote once and then went quiet
    path.write_text(json.dumps({"state": "WORKING", "running": True,
                                "mood": "#3fbf8f", "headline": "busy",
                                "updated_at": time.time() - 999,
                                "pid": 42, "detail": ""}), encoding="utf-8")
    quiet = read_presence(path)
    assert quiet["stale"] is True and quiet["headline"] == "busy"
    # a closed file (running False) is stale even if freshly written
    path.write_text(json.dumps({"state": "IDLE", "running": False, "mood": "#101828",
                                "headline": "ready", "updated_at": time.time(),
                                "pid": 42, "detail": ""}), encoding="utf-8")
    assert read_presence(path)["stale"] is True


def test_presence_never_churns_identical_state(tmp_path):
    settings = _settings(tmp_path)
    bus = EventBus()
    Presence(settings, bus=bus)
    path = presence_path(settings)
    before = path.stat().st_mtime_ns
    for _ in range(5):
        bus.emit("OBSERVATION", task_id="t1", path="files.exists", level="L1")
    assert path.stat().st_mtime_ns == before     # irrelevant events change nothing
