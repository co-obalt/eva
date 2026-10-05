"""Checkpoints + idempotency journal (doc 03 §7/§8, doc 05 §4).

Writing points:
  * after EVERY verified/SKIPPED step -> task row + checkpoint row
  * every executed action -> journal row (analytics, launch_count assertions)
  * effect_token tools -> durable dedup: a resumed task can never double-send
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ..storage import Database
from .task import Step, Task, TaskStatus


def action_hash(tool: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class CheckpointStore:
    def __init__(self, db: Database):
        self.db = db

    # -- tasks ----------------------------------------------------------------
    def save_task(self, task: Task) -> None:
        self.db.ex(
            "INSERT OR REPLACE INTO tasks(id, created_at, updated_at, status, task_json) "
            "VALUES (?,?,?,?,?)",
            (task.id, task.created_at, time.time(), task.status.value, task.to_json()),
        )

    def save_checkpoint(self, task: Task) -> None:
        verified = [s.effect_describe() for s in task.plan if s.status.done]
        pending_tail = [
            {"n": s.n, "tool": s.tool, "args": s.args}
            for s in task.plan if not s.status.done
        ]
        tail_hash = hashlib.sha256(
            json.dumps(pending_tail, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        self.db.ex(
            "INSERT INTO checkpoints(task_id, step_index, verified, evidence_refs, plan_tail_hash, ts) "
            "VALUES (?,?,?,?,?,?)",
            (
                task.id,
                len(verified),
                json.dumps(verified, default=str),
                json.dumps([len(task.evidence)], default=str),
                tail_hash,
                time.time(),
            ),
        )
        self.save_task(task)

    def load_task(self, task_id: str) -> Task | None:
        row = self.db.q1("SELECT task_json FROM tasks WHERE id=?", (task_id,))
        if not row:
            return None
        return Task.from_json(row["task_json"])

    def list_open_tasks(self) -> list[Task]:
        rows = self.db.q(
            "SELECT task_json FROM tasks WHERE status != 'FINISHED' ORDER BY updated_at DESC"
        )
        out: list[Task] = []
        for r in rows:
            try:
                out.append(Task.from_json(r["task_json"]))
            except Exception:
                continue
        return out

    def close_task(self, task: Task) -> None:
        self.save_task(task)

    def latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.q1(
            "SELECT * FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        )
        return dict(row) if row else None

    # -- journal ---------------------------------------------------------------
    def journal_add(self, task_id: str, action_h: str, tool: str, args: dict[str, Any],
                    outcome: str, result: dict[str, Any] | None = None,
                    reason: str = "") -> None:
        """One row per executed action. `reason` is the actor's one-line rationale
        (narration) — stored so `--explain` can reconstruct WHY, not just WHAT."""
        self.db.ex(
            "INSERT INTO journal(ts, task_id, action_hash, tool, args_json, outcome, result_json,"
            " reason) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), task_id, action_h, tool,
             json.dumps(args, sort_keys=True, default=str), outcome,
             json.dumps(result or {}, default=str), str(reason or "")[:300]),
        )

    def journal_rows(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.q(
            "SELECT id, ts, tool, args_json, outcome, reason FROM journal "
            "WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
        return [dict(r) for r in reversed(rows)]

    def journal_reasons(self, task_id: str) -> list[str]:
        return [str(r.get("reason") or "") for r in self.journal_rows(task_id, limit=200)
                if r.get("reason")]

    def journal_tool_count(self, task_id: str, tool: str) -> int:
        row = self.db.q1(
            "SELECT COUNT(*) AS c FROM journal WHERE task_id=? AND tool=?", (task_id, tool)
        )
        return int(row["c"]) if row else 0

    def journal_action_count(self, task_id: str, action_h: str) -> int:
        row = self.db.q1(
            "SELECT COUNT(*) AS c FROM journal WHERE task_id=? AND action_hash=?",
            (task_id, action_h),
        )
        return int(row["c"]) if row else 0

    # -- effect tokens (doc 03 §8: re-running never duplicates a side effect) --
    def effect_token_get(self, task_id: str, step: Step) -> dict[str, Any] | None:
        token = f"{task_id}:{step.action_hash}"
        row = self.db.q1("SELECT result_json FROM effect_tokens WHERE token=?", (token,))
        if not row:
            return None
        try:
            return json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            return {}

    def effect_token_put(self, task_id: str, step: Step, result: dict[str, Any]) -> None:
        token = f"{task_id}:{step.action_hash}"
        self.db.ex(
            "INSERT OR REPLACE INTO effect_tokens(token, ts, task_id, tool, result_json) "
            "VALUES (?,?,?,?,?)",
            (token, time.time(), task_id, step.tool, json.dumps(result, default=str)),
        )

    # -- llm response cache (doc 07 §4) ----------------------------------------
    def cache_get(self, key: str, now: float | None = None) -> Any | None:
        row = self.db.q1("SELECT ts, ttl, value_json FROM llm_cache WHERE key=?", (key,))
        if not row:
            return None
        now = now or time.time()
        if row["ttl"] and (now - row["ts"]) > float(row["ttl"]):
            return None
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def cache_set(self, key: str, value: Any, ttl_s: float = 6 * 3600.0) -> None:
        self.db.ex(
            "INSERT OR REPLACE INTO llm_cache(key, ts, ttl, value_json) VALUES (?,?,?,?)",
            (key, time.time(), ttl_s, json.dumps(value, default=str)),
        )
