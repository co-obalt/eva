"""Local presence — one tiny JSON file every surface reads.

EVA's processes stay separate (CLI, headless run, dashboard, pet window), so
they need a seam that is *not* a coupling: each EvaApp writes "what am I doing
right now" here (throttled by content, written atomically), and any viewer
process reads it without importing the app, opening the DB, or spawning a
worker. When nothing runs, the file simply goes stale and viewers show idle.

Stdlib only; a closed viewer or a missing file is invisible, never an error.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

STALE_AFTER_S = 12.0          # no write for this long = nothing is happening
NEUTRAL = "#101828"

MOODS = {                     # same vocabulary as AvatarBus (doc: VRM plan)
    "WAITING": "#ffcf70",
    "SAD": "#3a4a6b",
    "HAPPY": "#0f2a24",
    "NEUTRAL": NEUTRAL,
}

_STATE_MOOD = {
    "IDLE": "NEUTRAL",
    "THINKING": "NEUTRAL",
    "WORKING": "NEUTRAL",
    "SUCCESS": "HAPPY",
    "FAIL": "SAD",
    "WAITING": "WAITING",
}


def presence_path(settings) -> Path:
    return Path(getattr(settings, "data_dir", Path("."))) / "presence.json"


def _clip(text: Any, limit: int = 200) -> str:
    text = str(text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_presence(path) -> dict[str, Any]:
    """What a viewer needs: the state, its mood colour, and whether it is alive."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
    except (OSError, ValueError, AssertionError):
        return {"state": "NONE", "running": False, "mood": NEUTRAL,
                "headline": "EVA is not running", "detail": "", "stale": True,
                "updated_at": 0.0}
    age = time.time() - float(raw.get("updated_at") or 0.0)
    raw["stale"] = (not raw.get("running")) or age > STALE_AFTER_S
    raw["age_s"] = round(age, 1)
    raw.setdefault("state", "IDLE")
    raw.setdefault("mood", NEUTRAL)
    raw.setdefault("headline", "")
    raw.setdefault("detail", "")
    return raw


class Presence:
    """Writes presence.json from the app's events (never raises into the loop)."""

    def __init__(self, settings, bus=None):
        self.settings = settings
        self.path = presence_path(settings)
        self._state: dict[str, Any] = {
            "state": "IDLE", "mood": MOODS["NEUTRAL"], "headline": "ready",
            "detail": "", "pid": os.getpid(), "running": True,
            "updated_at": time.time(),
        }
        self._last_json = ""
        if bus is not None:
            bus.subscribe(self._react)
        try:
            self.flush()               # viewers see "EVA is up" before any event
        except OSError:
            pass

    # ------------------------------------------------------------------ reactor
    def _react(self, event) -> None:
        try:
            self._apply(event.type, dict(event.payload or {}))
            self.flush()
        except Exception:
            pass                          # presence may never break the core loop

    def _apply(self, kind: str, p: dict[str, Any]) -> None:
        s = self._state
        if kind == "TASK_STARTED":
            s.update(state="THINKING", headline=_clip(p.get("goal") or "working"))
            s["detail"] = ""
        elif kind in ("MODEL_ROUTED", "TASK_PLAN_READY"):
            s.update(state="THINKING", headline="thinking")
            s["detail"] = _clip(p.get("reason") or "")
        elif kind == "ACTOR_ACTION":
            s.update(state="WORKING",
                     headline=_clip(f"{p.get('kind', 'tool')}: {p.get('action', '')}"),
                     detail=_clip(p.get("reason"), 160))
        elif kind == "TOOL_START":
            s.update(state="WORKING", headline=_clip(p.get("tool")),
                     detail=_clip(f"step {p.get('step')} running"))
        elif kind == "TASK_STEP_RESULT":
            status = p.get("status")
            if status == "VERIFIED":
                s.update(state="SUCCESS", headline=_clip(f"{p.get('effect') or 'verified'}"),
                         detail=_clip("step verified"))
            elif status == "FAILED":
                s.update(state="FAIL", headline="step failed",
                         detail=_clip(p.get("observed") or p.get("reason")))
            elif status == "BLOCKED":
                s.update(state="WAITING", headline="blocked",
                         detail=_clip(p.get("reason")))
            elif status == "SKIPPED_ALREADY_DONE":
                s.update(state="SUCCESS", headline="already done (P3)", detail="")
        elif kind == "TASK_WAITING_USER":
            s.update(state="WAITING", headline="waiting for you",
                     detail=_clip(p.get("ask") or p.get("reason")))
        elif kind == "RISK_GATE":
            s.update(state="WAITING", headline="confirmation needed",
                     detail=_clip(p.get("title") or p.get("action")))
        elif kind == "TASK_ESCALATED":
            s.update(state="WORKING", headline="escalating to the actor loop",
                     detail=_clip(f"was: {p.get('from', '')}"))
        elif kind == "RULE_ENFORCED":
            s.update(state="WORKING", headline="a rule forbids an action",
                     detail=_clip(p.get("instruction")))
        elif kind == "RULE_REMEMBERED":
            s.update(headline=_clip(f"remembered: {p.get('instruction')}"))
        elif kind == "TASK_FINISHED" and p.get("status") == "success":
            s.update(state="SUCCESS", headline="task finished ✓",
                     detail=_clip(p.get("message")))
        elif kind == "TASK_FAILED":
            s.update(state="FAIL", headline="task failed",
                     detail=_clip(p.get("reason") or p.get("message")))
        elif kind == "MODE_CHANGED":
            s.update(state="WAITING" if p.get("mode") == "degraded" else "IDLE",
                     headline=_clip(p.get("mode")))
        else:
            return                          # irrelevant event: no write
        s["mood"] = MOODS[_STATE_MOOD.get(s["state"], "NEUTRAL")]
        s["updated_at"] = time.time()

    # ------------------------------------------------------------------- writing
    def flush(self) -> None:
        payload = json.dumps(self._state, ensure_ascii=False, default=str)
        if payload == self._last_json:
            return                          # nothing changed: no disk churn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)          # atomic: readers never see half a file
        self._last_json = payload

    def close(self) -> None:
        # keep the last headline (so viewers can say what EVA last did), but mark
        # the process as down — readers will treat it as sleeping
        self._state.update(running=False, updated_at=time.time())
        try:
            self.flush()
        except OSError:
            pass
