"""M1 surface — the local web dashboard: read-only view over data/ (presence + store)."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from eva.config import Settings
from eva.core.checkpoint import CheckpointStore
from eva.core.task import Task, TaskStatus
from eva.avatar.presence import presence_path
from eva.memory.store import MemoryStore, Rule
from eva.storage import Database

import eva_dashboard
from eva_dashboard import build_state, make_handler


def _settings(tmp_path) -> Settings:
    return Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path)


def test_build_state_defaults_when_nothing_has_run(tmp_path):
    state = build_state(_settings(tmp_path))
    assert state["presence"]["state"] == "NONE" and state["presence"]["stale"] is True
    assert state["tasks"] == [] and state["rules"] == [] and state["activity"] == []
    assert state["totals"] == {}


def test_build_state_reflects_store_and_presence(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    store = CheckpointStore(db)
    task = Task(user_request="make a folder", goal="make a folder",
                mode="actor", planned_by="actor")
    task.status = TaskStatus.FINISHED
    task.counters.actions_run = 3
    task.counters.actor_calls = 2
    store.save_task(task)
    store.journal_add(task.id, "hash1", "files.mkdir", {"path": "/x"}, "ok", {},
                      reason="make the first folder")
    MemoryStore(db).upsert_rule(Rule(instruction="never delete while building reports",
                                     when_text="report", forbid_tools=["files.delete"]))
    presence_path(settings).write_text(json.dumps(
        {"state": "SUCCESS", "running": True, "mood": "#34c77a",
         "headline": "task finished ✓", "detail": "", "updated_at": time.time(),
         "pid": 7}), encoding="utf-8")

    state = build_state(settings)

    assert state["presence"]["state"] == "SUCCESS" and state["presence"]["stale"] is False
    assert state["tasks"][0]["goal"] == "make a folder"
    assert state["tasks"][0]["mode"] == "actor" and state["tasks"][0]["actions"] == 3
    assert state["totals"]["FINISHED"] == 1
    assert state["rules"][0]["instruction"].startswith("never delete")
    assert state["rules"][0]["forbid"] == ["files.delete"]
    assert state["activity"][0]["tool"] == "files.mkdir"
    assert state["activity"][0]["reason"] == "make the first folder"
    db.close()


def test_dashboard_serves_page_and_state_over_http(tmp_path):
    settings = _settings(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            page = resp.read().decode("utf-8")
        assert resp.status == 200 and "EVA dashboard" in page
        with urllib.request.urlopen(base + "/api/state", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert set(payload) >= {"presence", "tasks", "rules", "activity", "totals"}
        assert payload["presence"]["stale"] is True      # nothing ran yet
        try:
            urllib.request.urlopen(base + "/api/nope", timeout=5)
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_module_is_importable_without_side_effects():
    # the launcher must not open sockets or browsers at import time
    assert callable(eva_dashboard.build_state)
    assert callable(eva_dashboard.main)
