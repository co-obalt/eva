"""Headless one-shot mode: `python eva.py --run "<goal>"`.

Exit code 0 iff the completion condition was verified (or the input was a
conversational reply). JSON output is scriptable/e2e-able; the status and the
counters are the same numbers the test suite asserts (doc 11 "numbers passed").
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

from ..bootstrap import EvaApp
from ..core.task import TaskStatus


async def run_once(goal: str, *, json_out: bool = False, yes_high: bool = False,
                   voice: bool = False, avatar: bool = False,
                   confirm_override=None, explain: bool = False) -> int:
    app = EvaApp(voice=voice or False, avatar=avatar or False, speak=False)

    async def confirm(decision, step, task) -> bool:
        if confirm_override is not None:
            return bool(confirm_override(decision, step, task))
        return bool(yes_high)      # fail-safe cancel unless explicitly allowed

    app.orchestrator.confirm = confirm

    started = time.time()
    try:
        result, reply = await app.handle(goal, source="cli")
    except Exception as exc:
        payload = {"status": "ERROR", "verified": False, "message": f"{type(exc).__name__}: {exc}"}
        _emit(payload, json_out)
        await app.close()
        return 2

    if result is None:
        payload = {"status": "REPLY", "verified": None, "reply": reply,
                   "mode": app.mode(), "seconds": round(time.time() - started, 2)}
        _emit(payload, json_out)
        await app.close()
        return 0

    task = result.task
    payload: dict[str, Any] = {
        "status": task.status.value,
        "verified": task.status is TaskStatus.FINISHED,
        "message": task.result_message,
        "task_id": task.id,
        "planned_by": task.planned_by,
        "steps": [{"n": s.n, "tool": s.tool, "status": s.status.value,
                    "reason": s.intent} for s in task.plan],
        "counters": task.counters.model_dump(),
        "outputs": app.outputs_of(result),
        "mode": app.mode(),
        "seconds": round(time.time() - started, 2),
        "explain": bool(explain),
    }
    _emit(payload, json_out)
    await app.close()
    return 0 if task.status is TaskStatus.FINISHED else 1


def _emit(payload: dict, json_out: bool) -> None:
    if json_out:
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return
    status = payload.get("status")
    if status == "REPLY":
        print(payload.get("reply", ""))
        return
    print(f"[{status}] {payload.get('message', '')}")
    for s in payload.get("steps", []):
        mark = {"VERIFIED": "✓", "SKIPPED_ALREADY_DONE": "↷", "FAILED": "✗",
                "BLOCKED": "⏸"}.get(s["status"], "·")
        print(f"  {mark} {s['n']}. {s['tool']} [{s['status']}]")
        if payload.get("explain") and s.get("reason"):
            print(f"      ↳ why: {s['reason']}")


def reason_lines(steps: list[dict]) -> list[str]:
    """Per-step narration for `--explain`: the actor's own one-line reason."""
    return [f"  {s.get('n')}. {s.get('tool')}: {s.get('reason')}"
            for s in steps if s.get("reason")]
    for entry in payload.get("outputs", [])[-2:]:
        o = entry.get("outputs") or {}
        if "resources" in o:
            r = o["resources"]
            print(f"  cpu {r.get('cpu_percent')}% · ram free {r.get('ram_free_mb')} MB · "
                  f"disk free {r.get('disk_free_gb')} GB")
        elif "windows" in o:
            print(f"  windows ({len(o['windows'])}): "
                  + ", ".join(str(w.get('title') if isinstance(w, dict) else w) for w in o["windows"][:6]))
        elif "matches" in o:
            print(f"  matches ({o.get('count')}): " + ", ".join(map(str, o["matches"][:6])))
        elif "output" in o:
            print("  " + str(o["output"])[:300])
    if not sys.stdout.isatty():
        return
