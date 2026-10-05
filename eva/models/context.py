"""Context builder (doc 07 §3, P11): assembled per decision, never dumped.

Hard budget enforced; overflow drops in priority order:
history -> low-relevance memory -> verbose tool descriptions -> state rows.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.state import Observation
from ..core.task import Task


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


SYSTEM_PROMPT = (
    "You are EVA's planning module. You propose; the system disposes. "
    "Rules you must never break: emit ONLY registered tools with exact parameter names; "
    "one action per step; every step must name its intent; plans are maps, not contracts. "
    "Verification is done by the system from world state — you never claim success. "
    "If you cannot plan with the tools given, return {\"steps\": []}."
)


class ContextBuilder:
    def __init__(self, budget_tokens: int = 4000):
        self.budget_tokens = budget_tokens

    # ------------------------------------------------------------------ build
    def build(self, *, task: Task, state: list[dict] | str | None = None,
              memory_slice: dict | None = None, tools: str = "",
              observations: list[dict] | None = None,
              history: list[str] | None = None,
              extra_rules: str = "") -> list[dict]:
        sections: dict[str, str] = {}
        sections["system"] = SYSTEM_PROMPT + ("\n" + extra_rules if extra_rules else "")
        sections["task"] = self._task_brief(task)
        sections["state"] = self._state_block(state)
        sections["memory"] = self._memory_block(memory_slice)
        sections["tools"] = tools
        sections["observations"] = self._obs_block(observations or [])
        sections["history"] = self._history_block(history or [])

        # trim to budget in priority order (doc 07 §3 rule 6)
        order = ["history", "observations", "memory", "tools", "state"]
        for key in order:
            if self._size(sections) <= self.budget_tokens:
                break
            if key == "observations" and len(sections["observations"]) > 200:
                sections["observations"] = sections["observations"][:200]
            elif key == "memory" and sections["memory"]:
                sections["memory"] = ""
            else:
                sections[key] = ""

        user = "\n\n".join(f"## {k}\n{v}" for k, v in sections.items() if v)
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]

    def _size(self, sections: dict[str, str]) -> int:
        return estimate_tokens("\n".join(sections.values()))

    # ------------------------------------------------------------- sectioners
    def _task_brief(self, task: Task) -> str:
        lines = [f"user_request: {task.user_request}", f"goal: {task.goal}"]
        if task.constraints:
            lines.append("constraints: " + "; ".join(task.constraints))
        lines.append(f"completion_condition: {task.completion.describe()}")
        lines.append(f"risk_ceiling: {task.risk_level.value}")
        return "\n".join(lines)

    def _state_block(self, state: list[dict] | str | None) -> str:
        if state is None:
            return "(no state provided)"
        if isinstance(state, str):
            return state[:1200]
        rows = []
        for r in state[:40]:
            rows.append(f"{r.get('path')} = {r.get('value')!r} [{r.get('level')}, "
                        f"{r.get('age_s')}s, {r.get('truth')}]")
        return "\n".join(rows) or "(empty)"

    def _memory_block(self, memory_slice: dict | None) -> str:
        if not memory_slice:
            return ""
        try:
            return json.dumps(memory_slice, ensure_ascii=False, default=str)[:2000]
        except (TypeError, ValueError):
            return ""

    def _obs_block(self, observations: list[dict]) -> str:
        return "\n".join(str(o) for o in observations[-3:])

    def _history_block(self, history: list[str]) -> str:
        return "\n".join(history[-8:])

    # ------------------------------------------------------------- tool menus
    @staticmethod
    def tools_catalogue(registry, domains: list[str] | None = None,
                        names: list[str] | None = None) -> str:
        """Only contract info the planner/actor needs: name(params) — doc (risk).

        `names` narrows the catalogue to a relevance-filtered menu (order preserved)."""
        lines = []
        if names is not None:
            tools = [t for n in names if (t := registry.get(n)) is not None]
        else:
            tools = registry.all()
        for t in tools:
            c = t.contract
            if domains and c.category not in domains:
                continue
            params = ", ".join(
                f"{name}{'' if spec.required else '?'}:{spec.type}"
                for name, spec in c.params.items())
            lines.append(f"- {c.name}({params}) — {c.description} [risk:{c.risk.value}]")
        return "\n".join(lines)
