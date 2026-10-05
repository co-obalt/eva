"""AVATAR agent-bus client (http://127.0.0.1:47903 by default).

Design constraints (from the integration plan):
  * daemon worker queue — events never block the core loop
  * 3-failure / 30s circuit breaker — a closed companion window is invisible
  * expressions are pre-baked mappings, zero tokens, zero latency beyond the hop
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


STATE_TO_ANIMATION = {
    "IDLE": ("animation.default", {}),
    "THINKING": ("animation.play", {"id": "Model Pose", "mode": "once"}),
    "SUCCESS": ("animation.play", {"id": "Greeting", "mode": "once"}),
    "FAIL": ("animation.play", {"id": "Squat", "mode": "once"}),
    "CELEBRATE": ("animation.play", {"id": "Peace Sign", "mode": "once"}),
    "WORKING": ("animation.play", {"id": "Spin", "mode": "once"}),
}

AMBIENT = {
    "WAITING": "#ffcf70",
    "SAD": "#3a4a6b",
    "HAPPY": "#0f2a24",
    "NEUTRAL": "#101828",
}


class AvatarBus:
    def __init__(self, settings, event_bus=None):
        self.settings = settings
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self.failures = 0
        self.dead_until = 0.0
        self.sent = 0
        self.skipped = 0
        self._worker: threading.Thread | None = None
        if event_bus is not None:
            self.attach(event_bus)

    # ------------------------------------------------------------------ wiring
    def attach(self, event_bus) -> None:
        event_bus.subscribe(self._react)

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="avatar-bus", daemon=True)
            self._worker.start()

    # ------------------------------------------------------------------ reactor
    def _react(self, event) -> None:
        t = event.type
        try:
            if t in ("TASK_STARTED",):
                self.notify("THINKING")
            elif t in ("TASK_PLAN_READY", "MODEL_ROUTED"):
                self.notify("THINKING")
            elif t == "TOOL_START":
                self.notify("WORKING")
            elif t == "TASK_STEP_RESULT" and event.payload.get("status") == "VERIFIED":
                self.notify("SUCCESS")
            elif t == "TASK_STEP_RESULT" and event.payload.get("status") in ("FAILED", "BLOCKED"):
                self.notify("FAIL")
            elif t in ("TASK_WAITING_USER", "RISK_GATE"):
                self.set_ambient("WAITING")
            elif t == "TASK_FINISHED" and event.payload.get("status") == "success":
                self.notify("CELEBRATE")
                self.set_ambient("HAPPY")
            elif t == "TASK_FAILED":
                self.notify("FAIL")
                self.set_ambient("SAD")
            elif t == "MODE_CHANGED" and event.payload.get("mode") == "degraded":
                self.set_ambient("WAITING")
            elif t == "REMINDER_FIRED":
                self.notify("CELEBRATE")
                self.set_ambient("WAITING")
        except Exception:
            pass

    # ------------------------------------------------------------------ commands
    def notify(self, state: str) -> None:
        anim = STATE_TO_ANIMATION.get(state)
        if anim:
            self._enqueue("animation.play" if anim[1] else "animation.default", anim[1])

    def set_ambient(self, mood: str) -> None:
        color = AMBIENT.get(mood)
        if color:
            self._enqueue("environment.set", {"type": "color", "value": color})

    def on_speak(self) -> None:
        self._enqueue("audio.source", {"source": "system"})

    def _enqueue(self, command: str, payload: dict[str, Any]) -> None:
        if time.time() < self.dead_until:
            self.skipped += 1
            return
        try:
            self.q.put_nowait((command, payload))
            self.start()
        except queue.Full:
            self.skipped += 1

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        while True:
            command, payload = self.q.get()
            try:
                self._post(command, payload)
                self.failures = 0
                self.sent += 1
            except Exception:
                self.failures += 1
                if self.failures >= 3:
                    self.dead_until = time.time() + 30.0
            finally:
                self.q.task_done()

    def _post(self, command: str, payload: dict[str, Any]) -> None:
        if requests is None:
            raise RuntimeError("requests unavailable")
        headers = {"Content-Type": "application/json"}
        if self.settings.avatar_token:
            headers["Authorization"] = f"Bearer {self.settings.avatar_token}"
        url = self.settings.avatar_bus.rstrip("/") + "/v1/command"
        resp = requests.post(url, data=json.dumps({"command": command, "payload": payload}),
                             headers=headers, timeout=3.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"avatar bus HTTP {resp.status_code}")

    def status(self) -> dict:
        return {"sent": self.sent, "skipped": self.skipped, "failures": self.failures,
                "dead": time.time() < self.dead_until}
