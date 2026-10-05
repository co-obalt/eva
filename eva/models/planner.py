"""LLM planner (Tier 2, doc 03 §2): plan before execution, structurally validated.

The LLM may only propose registered tools with valid params. Effects,
preconditions and verification come from the tool contracts — never from the
model (P1). Malformed plans are rejected before execution.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..core.contracts import ContractError, ToolRegistry
from ..core.state import Condition, Predicate, parse_predicate
from ..core.task import Step, Task
from .context import ContextBuilder
from .router import ModelRouter, NoModelNeeded

KNOWN_BRANCHES = {"system", "chrome", "page", "wa", "files", "apps", "terminal",
                  "reads", "ui", "comms", "downloads"}
MAX_STEPS = 12

OBSERVATION_PATHS = """Known observation paths (use EXACT paths in completion):
  system.cpu_percent, system.ram_free_mb, system.ram_used_percent, system.disk_free_gb,
  system.windows (list of titles), system.foreground (window title)
  chrome.running, chrome.active_profile, chrome.debug_port, chrome.has_tab(match="*url*"),
  chrome.profiles.matched(query="...")
  page.url, wa.login, wa.loaded, wa.view ("chatlist"|"archived"|"qr_login"), wa.selected_chat,
  wa.search_query
  files.exists(path=...), files.is_dir(path=...), files.size(path=...), files.content(path=...)
  apps.running(name="chrome"), apps.foreground
  terminal.last_exit_code, downloads.exists(file="*.pdf")
  comms.sent(recipient=..., text=...)
Unknown paths are forbidden — the completion condition must be checkable."""


class PlannerError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise PlannerError(f"model did not return JSON: {text[:200]!r}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise PlannerError(f"invalid JSON from model: {exc}") from exc


class LlmPlanner:
    def __init__(self, router: ModelRouter, registry: ToolRegistry,
                 contexts: ContextBuilder | None = None, bus=None, settings=None):
        self.router = router
        self.registry = registry
        self.contexts = contexts or ContextBuilder()
        self.bus = bus
        self.settings = settings

    # ------------------------------------------------------------ compose intent
    async def compose_intent(self, text: str) -> tuple[str, Condition, list[str]]:
        prompt = f"""Turn this user request into a machine-checkable task.

USER REQUEST: {text}

{OBSERVATION_PATHS}

Return JSON:
{{"goal": "...", "constraints": ["..."], "completion": [{{"path": "...", "op": "==", "value": ...}}]}}
`completion` is the contract: the task is FINISHED only when every predicate is TRUE
against observed world state. Prefer 1-4 predicates. Use args for parameterized paths."""
        messages = self.contexts.build(
            task=Task(goal="compose-intent", user_request=text,
                      completion=Condition(all_of=[Predicate(path="system.reported", op="==", value=True)])),
            extra_rules="This call only composes the intent and completion condition; it does not plan steps.")
        messages.append({"role": "user", "content": prompt})
        last_err = ""
        for _ in range(2):
            try:
                resp = await self.router.call(2, "compose_intent", messages=messages,
                                              json_mode=True, max_tokens=500)
                data = _extract_json(resp.text)
                goal = str(data.get("goal") or text)[:300]
                constraints = [str(c)[:200] for c in (data.get("constraints") or [])][:6]
                preds = self._validate_completion(data.get("completion") or [])
                return goal, Condition(all_of=preds), constraints
            except (PlannerError, ValueError) as exc:
                last_err = str(exc)
                messages.append({"role": "user",
                                 "content": f"Your previous answer was invalid: {last_err}. "
                                            f"Return valid JSON only."})
        raise PlannerError(f"intent composition failed: {last_err}")

    def _validate_completion(self, raw: Any) -> list[Predicate]:
        if isinstance(raw, dict):
            raw = raw.get("all_of") or [raw]
        if not isinstance(raw, list) or not raw:
            raise PlannerError("completion must be a non-empty list")
        preds: list[Predicate] = []
        for item in raw[:6]:
            try:
                if isinstance(item, str):
                    p = parse_predicate(item)
                else:
                    p = Predicate.model_validate(item)
            except Exception as exc:
                raise PlannerError(f"invalid predicate {item!r}: {exc}") from exc
            branch = p.path.split(".")[0]
            if branch not in KNOWN_BRANCHES:
                raise PlannerError(f"unknown observation branch {branch!r} in {p.describe()}")
            preds.append(p)
        return preds

    # ------------------------------------------------------------------- plan
    async def plan(self, task: Task, *, state=None, memory_slice=None) -> list[Step]:
        tools = ContextBuilder.tools_catalogue(self.registry)
        base = self.contexts.build(
            task=task, state=state, memory_slice=memory_slice, tools=tools,
            observations=(state[:5] if isinstance(state, list) else None))
        base.append({"role": "user", "content":
                     "Produce the plan now as JSON: {\"steps\": [{\"intent\": \"...\", "
                     "\"tool\": \"registered.tool\", \"args\": {...}}]} — at most 10 steps, "
                     "registered tools only, exact param names."})
        last_err = ""
        for attempt in range(2):
            try:
                resp = await self.router.call(2, "plan", messages=base, json_mode=True,
                                              max_tokens=900)
                steps = self._validate_steps(_extract_json(resp.text).get("steps") or [])
                return steps
            except (PlannerError, ContractError) as exc:
                last_err = str(exc)
                base.append({"role": "user",
                             "content": f"That plan was rejected: {last_err}. "
                                        f"Fix it and return the JSON again."})
        raise PlannerError(f"planning failed after 2 attempts: {last_err}")

    def _validate_steps(self, raw: list) -> list[Step]:
        if not isinstance(raw, list) or not raw:
            raise PlannerError("plan must contain at least one step")
        if len(raw) > MAX_STEPS:
            raise PlannerError(f"plan has {len(raw)} steps > max {MAX_STEPS}")
        steps: list[Step] = []
        for i, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise PlannerError(f"step {i} is not an object")
            tool_name = str(item.get("tool") or "")
            tool = self.registry.get(tool_name)
            if tool is None:
                raise PlannerError(f"step {i}: tool {tool_name!r} is not registered "
                                   f"(available: {', '.join(self.registry.names()[:30])})")
            try:
                args = self.registry.validate_args(tool_name, dict(item.get("args") or {}))
            except ContractError as exc:
                raise PlannerError(f"step {i}: {exc}") from exc
            steps.append(Step(n=i, intent=str(item.get("intent") or tool_name)[:200],
                              tool=tool_name, args=args,
                              verification_timeout_s=tool.contract.verification.timeout_s))
        return steps

    # ------------------------------------------------------------------ repair
    async def repair(self, task: Task, *, reason: str, state=None) -> list[Step] | None:
        done = [s for s in task.plan if s.status.done]
        failed = [s for s in task.plan if not s.status.done]
        history = [f"step {s.n} {s.tool}: {s.status.value} — {s.note}" for s in failed]
        tools = ContextBuilder.tools_catalogue(self.registry)
        messages = self.contexts.build(
            task=task, state=state, tools=tools, history=history,
            extra_rules=("You are REPAIRING a plan. The reason for repair is: " + reason +
                         ". Completed steps: " + ", ".join(f"{s.n}:{s.tool}" for s in done) +
                         ". Produce ONLY the remaining tail steps."))
        messages.append({"role": "user", "content":
                         "Return JSON: {\"steps\": [...]} for the remaining work only."})
        try:
            resp = await self.router.call(2, "repair", messages=messages, json_mode=True,
                                          max_tokens=700)
            return self._validate_steps(_extract_json(resp.text).get("steps") or [])
        except (PlannerError, ContractError, NoModelNeeded):
            return None
