"""Agent-level tools: asking the user, and remembering durable instructions.

These are the two verbs that make EVA *adaptable* rather than scripted:

* ``agent.ask_user``  — ambiguity is a legitimate outcome. The task parks in
  WAITING_USER with one precise question; the answer is recorded as the
  ``ui.user_answer`` observation for that question, so a resumed task can see it
  and it never asks the same question twice (P3).
* ``memory.remember`` — a user rule ("aage se X ke liye Y") becomes a Row in the
  ``rules`` table: memory, not code. Forbidden tools are enforced before
  execution by the orchestrator; preferred tools are surfaced as hints.
"""
from __future__ import annotations

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from ..memory.store import Rule

_SENTINELS = {"", "none", "null", "-", "n/a"}


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in str(value).replace(",", "|").split("|")]
    return [p for p in parts if p and p.casefold() not in _SENTINELS]


def register_all(reg: ToolRegistry, settings, memory=None) -> None:
    # ------------------------------------------------------------- ask_user
    async def ask_user(args, ctx: ToolContext) -> ToolResult:
        question = str(args["question"]).strip()
        options = _split(args.get("options"))
        ctx.perception.note("ui.asked", question)
        ask = question if not options else f"{question}  [options: {' / '.join(options)}]"
        return ToolResult(ok=False, blocked=True, note="waiting for the user's answer",
                          ask=ask, outputs={"question": question, "options": options})

    reg.register(ToolContract(
        name="agent.ask_user", category="system",
        description="Ask the user ONE short question (optional `a|b|c` choices) and wait for the answer",
        params={"question": ParamSpec(doc="the single question, in the user's language"),
                "options": ParamSpec(required=False, doc="pipe-separated choices, or empty")},
        effects_for=lambda a: [Predicate(path="ui.user_answer", op="exists",
                                         args={"question": str(a.get("question", ""))})],
        verification=VerificationSpec(method="L0_user_answer", timeout_s=5.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
    ), ask_user)

    # --------------------------------------------------------- memory.remember
    async def remember(args, ctx: ToolContext) -> ToolResult:
        instruction = str(args["instruction"]).strip()
        if not instruction:
            return ToolResult(ok=False, note="empty rule text")
        if memory is None:
            return ToolResult(ok=False, blocked=True, note="no memory store wired",
                              ask="Rule memory is unavailable in this run, so I did not store it.")
        prefer = [t for t in _split(args.get("prefer_tools")) if reg.get(t)]
        forbid = [t for t in _split(args.get("forbid_tools")) if reg.get(t)]
        unknown = [t for t in _split(args.get("prefer_tools")) + _split(args.get("forbid_tools"))
                   if not reg.get(t)]
        rule = Rule(scope=str(args.get("scope") or "global").strip() or "global",
                    when_text=str(args.get("when") or "").strip(),
                    instruction=instruction, prefer_tools=prefer, forbid_tools=forbid)
        rid = memory.upsert_rule(rule)
        note = f"remembered rule {rid}"
        if unknown:
            note += f" (ignored unknown tools: {', '.join(sorted(set(unknown)))})"
        return ToolResult(ok=True, mutated=True, note=note,
                          outputs={"rule_id": rid, "instruction": instruction,
                                   "scope": rule.scope, "when": rule.when_text,
                                   "prefer_tools": prefer, "forbid_tools": forbid,
                                   "ignored": sorted(set(unknown))})

    reg.register(ToolContract(
        name="memory.remember", category="system",
        description="Remember a durable user rule (scope/when + optional prefer_tools/forbid_tools)",
        params={"instruction": ParamSpec(doc="the rule in the user's words"),
                "when": ParamSpec(required=False, doc="trigger words, e.g. 'website'"),
                "scope": ParamSpec(required=False, doc="global | app:<name> | site:<host>"),
                "prefer_tools": ParamSpec(required=False, doc="pipe list of tool names to prefer"),
                "forbid_tools": ParamSpec(required=False, doc="pipe list of tool names to never use here")},
        effects_for=lambda a: [Predicate(path="rules.exists", op="==", value=True,
                                         args={"rule": str(a.get("instruction", ""))})],
        verification=VerificationSpec(method="L1_rule_store", timeout_s=5.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
    ), remember)
