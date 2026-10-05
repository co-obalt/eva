"""Actor (M1) — one action per cycle, decided for the *current* situation.

This is what turns EVA from "run the frozen step list" into "work the problem":
the model sees the task, the live state slice, the recent action history
(including failures), applicable user rules, and a *relevance-filtered* tool
menu — and proposes exactly ONE next action.

Hard boundaries (unchanged invariants):
  P1  only registered tools, with contract-validated params; effects,
      preconditions and verification always come from the contract.
  P2  the actor never verifies its own work — steps are verified from world
      state by the orchestrator.
  P6  `finish` is a request, not a verdict: only the completion condition can
      finish a task.
  P9  risk gates still apply to every proposed action.

If the model cannot produce a valid action, the actor says so (ActorError) — the
caller decides between a bounded retry and an honest BLOCKED. It never invents a
tool, and it never silently no-ops.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.contracts import ContractError, ToolRegistry
from .context import ContextBuilder
from .planner import _extract_json, PlannerError

MAX_MENU = 14

ACTOR_SYSTEM = (
    "You are EVA's action loop: the part that actually works a computer on the user's behalf. "
    "Each turn you choose EXACTLY ONE next action. Rules you must never break: choose tools only "
    "from the TOOL MENU, with the exact names and parameter names given; one action per turn; "
    "never verify your own work or claim success — the system checks the effect against real "
    "world state and reports back to you; a `finish` action is only a request, the completion "
    "condition decides; when the request is ambiguous, or a choice belongs to the user, ASK "
    "instead of guessing; when the user states a durable instruction (\"from now on\", \"always\"), "
    "use memory.remember so it holds next time too; prefer the cheapest action that makes "
    "observable progress, and never repeat an action that already failed without changing "
    "something. Reply ONLY with JSON."
)

_JSON_SPEC = """Return ONE JSON object:
{"kind": "tool", "tool": "<name from the menu>", "args": {...}, "reason": "<one short line: why this action, in the user's language>"}
{"kind": "ask", "question": "<one precise question>", "options": ["...", "..."], "reason": "..."}
{"kind": "finish", "reason": "<why you believe the completion condition is now satisfied>"}"""


class ActorError(RuntimeError):
    pass


@dataclass
class ActionProposal:
    kind: str                                   # tool | ask | finish
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.kind}:{self.tool}:{json.dumps(self.args, sort_keys=True, default=str)}"

    def describe(self) -> str:
        if self.kind == "tool":
            return f"{self.tool}({json.dumps(self.args, sort_keys=True, default=str)[:160]})"
        if self.kind == "ask":
            return f"ask_user({self.question[:120]!r})"
        return "finish"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zA-Z0-9_.:/\\-]{3,}", str(text).casefold())}


class Actor:
    """Planner-like, but per-cycle and state-aware (see module docstring)."""

    def __init__(self, router, registry: ToolRegistry, memory=None, bus=None, settings=None,
                 menu_size: int = MAX_MENU):
        self.router = router
        self.registry = registry
        self.memory = memory
        self.bus = bus
        self.settings = settings
        self.menu_size = max(4, int(menu_size))

    # ------------------------------------------------------------------ menu
    def menu(self, task, history: list[dict[str, Any]] | None = None) -> list[str]:
        """Relevance-filtered tool menu: a small model cannot read 40 tools."""
        history = history or []
        recent = " ".join(str(h.get("observed") or "") + " " + str(h.get("action") or "")
                          for h in history[-3:])
        want = _tokens(f"{task.user_request} {task.goal} {recent}")
        used = {str(h.get("tool") or "") for h in history}
        failed_tool = ""
        for h in reversed(history):
            if str(h.get("status") or "").upper() in ("FAILED", "PRECONDITION", "REJECTED"):
                failed_tool = str(h.get("tool") or "")
                break
        alts: set[str] = set()
        if failed_tool:
            contract = self.registry.get(failed_tool)
            if contract is not None:
                alts = set(contract.contract.alternatives)

        scored: list[tuple[int, str]] = []
        for tool in self.registry.all():
            c = tool.contract
            score = 0
            score += 3 * len(want & _tokens(c.name))
            score += 2 * len(want & _tokens(c.description))
            score += 1 * len(want & _tokens(c.category))
            if c.name in used:
                score += 1
            if c.name in alts:
                score += 2
            if score:
                scored.append((score, c.name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = [name for _, name in scored[: self.menu_size]]
        # interactive primitives are always available
        for always in ("agent.ask_user", "memory.remember"):
            if always in self.registry.names() and always not in picked:
                picked.append(always)
        return picked

    def _menu_lines(self, names: list[str]) -> str:
        return ContextBuilder.tools_catalogue(self.registry, names=names)

    # ------------------------------------------------------------ the decision
    async def next_action(self, task, *, state: list[dict] | None = None,
                          history: list[dict[str, Any]] | None = None,
                          memories: dict[str, Any] | None = None,
                          budget: dict[str, Any] | None = None) -> ActionProposal:
        history = history or []
        names = self.menu(task, history)
        menu = self._menu_lines(names)
        rules = (memories or {}).get("rules") or []
        prompt = self._prompt(task, state=state, history=history, menu=menu,
                              rules=rules, budget=budget or {})

        messages = [{"role": "system", "content": ACTOR_SYSTEM},
                    {"role": "user", "content": prompt}]

        last_err = ""
        for _ in range(2):
            try:
                resp = await self.router.call(2, "actor", messages=messages, json_mode=True,
                                              max_tokens=500)
            except PlannerError as exc:
                raise ActorError(str(exc)) from exc
            try:
                return self._parse(resp.text)
            except ActorError as exc:
                last_err = str(exc)
                messages.append({"role": "user",
                                 "content": f"That proposal was rejected: {last_err}. "
                                            f"Answer again with ONE valid JSON object."})
        raise ActorError(f"actor could not propose a valid action: {last_err}")

    # ----------------------------------------------------------------- parsing
    def _parse(self, text: str) -> ActionProposal:
        try:
            data = _extract_json(text)
        except PlannerError as exc:
            raise ActorError(str(exc)) from exc
        kind = str(data.get("kind") or "tool").strip().casefold()
        reason = str(data.get("reason") or "")[:300]
        if kind == "finish":
            return ActionProposal(kind="finish", reason=reason, raw=data)
        if kind in ("ask", "ask_user", "question"):
            question = str(data.get("question") or "").strip()
            if not question:
                raise ActorError("an `ask` action needs a non-empty `question`")
            options = [str(o)[:80] for o in (data.get("options") or [])][:4]
            return ActionProposal(kind="ask", question=question[:400], options=options,
                                  reason=reason, raw=data)
        tool = str(data.get("tool") or "").strip()
        if not tool:
            raise ActorError("`tool` action needs a `tool` name")
        if self.registry.get(tool) is None:
            raise ActorError(f"tool {tool!r} is not registered (menu: {', '.join(self.menu_names())})")
        try:
            args = self.registry.validate_args(tool, dict(data.get("args") or {}))
        except ContractError as exc:
            raise ActorError(str(exc)) from exc
        return ActionProposal(kind="tool", tool=tool, args=args, reason=reason, raw=data)

    def menu_names(self) -> list[str]:
        return self.registry.names()[:40]

    # ------------------------------------------------------------------ prompt
    def _prompt(self, task, *, state: list[dict] | None, history: list[dict[str, Any]],
                menu: str, rules: list[dict[str, Any]], budget: dict[str, Any]) -> str:
        lines = [
            "TASK",
            f"user_request: {task.user_request}",
            f"goal: {task.goal}",
            f"completion_condition: {task.completion.describe()}",
        ]
        if task.constraints:
            lines.append("constraints: " + "; ".join(task.constraints))
        if task.context.get("escalated_from"):
            lines.append(f"note: the fast lane ({task.context['escalated_from']}) gave up; "
                         f"you are the general fallback — adapt, do not replay it.")
        lines.append("")
        lines.append("CURRENT WORLD STATE (three-valued: TRUE / FALSE / UNKNOWN)")
        lines.append(self._state_lines(state))
        lines.append("")
        lines.append("WHAT HAPPENED SO FAR (newest last)")
        lines.append(self._history_lines(history))
        if rules:
            lines.append("")
            lines.append("USER RULES THAT APPLY (durable memory — obey them)")
            for r in rules:
                extra = []
                if r.get("prefer"):
                    extra.append("prefer " + ", ".join(r["prefer"]))
                if r.get("forbid"):
                    extra.append("NEVER use " + ", ".join(r["forbid"]))
                lines.append(f"- {r.get('instruction')} ({'; '.join(extra)})" if extra
                             else f"- {r.get('instruction')}")
        if budget:
            lines.append("")
            lines.append("REMAINING BUDGET: " + ", ".join(f"{k}={v}" for k, v in budget.items()))
        lines.append("")
        lines.append("TOOL MENU (the only tools you may call)")
        lines.append(menu or "(no tools matched this request)")
        lines.append("")
        lines.append(_JSON_SPEC)
        return "\n".join(lines)

    @staticmethod
    def _state_lines(state: list[dict] | None) -> str:
        if not state:
            return "(nothing observed yet — start with a cheap read)"
        rows = []
        for r in state[:35]:
            value = r.get("value")
            text = str(value)
            if len(text) > 160:
                text = text[:160] + "…"
            rows.append(f"{r.get('path')} = {text} [{r.get('truth', 'TRUE')}, "
                        f"{r.get('level', 'L0')}, {r.get('age_s', 0)}s]")
        return "\n".join(rows)

    @staticmethod
    def _history_lines(history: list[dict[str, Any]]) -> str:
        if not history:
            return "(nothing yet)"
        rows = []
        for h in history[-12:]:
            rows.append(f"{h.get('step', '?')}. [{h.get('status')}] {h.get('action')}"
                        + (f" → {str(h.get('observed'))[:160]}" if h.get("observed") else "")
                        + (f"  (why: {str(h.get('reason'))[:100]})" if h.get("reason") else ""))
        return "\n".join(rows)
