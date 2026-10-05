"""Task Manager (doc 03 §9): request -> Task with goal/constraints/completion.

Route order (doc 07 §2):
  1. direct reply (greetings/help — not a task at all)
  2. deterministic recipe            -> Tier 0, 0 model calls
  3. matched skill with confidence   -> Tier 0, 0 planning calls
  4. LLM composer (Tier 2)           -> novel task, completion machine-checkable
  5. no provider                     -> honest reply, NOTHING executed
"""
from __future__ import annotations

from typing import Any

from ..core.task import Task
from ..memory.store import MemoryStore
from .recipes import direct_reply, match_recipe
from .skills import SkillPlanner, skill_to_steps


class TaskManager:
    def __init__(self, settings, memory: MemoryStore, composer=None, bus=None, registry=None):
        self.settings = settings
        self.memory = memory
        self.composer = composer           # LlmPlanner (compose_intent)
        self.bus = bus
        self.registry = registry

    async def handle(self, text: str, source: str = "cli") -> tuple[Task | None, str | None]:
        text = (text or "").strip()
        if not text:
            return None, ""

        reply = direct_reply(text)
        if reply is not None:
            return None, reply

        # 2) deterministic recipe — no model call
        task = match_recipe(text, self.settings)
        if task is not None:
            task.source = source
            self._created(task)
            return task, None

        # 3) learned skill — no planning call
        match = self.memory.match_skill(text)
        if match is not None:
            skill, score = match
            steps = skill_to_steps(skill)
            if steps:
                task = Task(user_request=text, goal=f"[skill {skill.name}] {text}",
                            completion=self._skill_completion(text, steps),
                            plan=steps, planned_by=f"skill:{skill.name}",
                            origin_skill=skill.name, source=source)
                self._created(task)
                return task, None

        # 4) LLM composer (Tier 2) -> ACTOR mode (M1): the composer only fixes the
        #    goal + completion condition; the actor decides each action at runtime.
        if self.composer is not None:
            try:
                goal, completion, constraints = await self.composer.compose_intent(text)
                task = Task(user_request=text, goal=goal, completion=completion,
                            constraints=constraints, planned_by="actor", mode="actor",
                            source=source)
                self._created(task)
                return task, None
            except Exception as exc:
                reason = str(exc)[:200]
                return None, (f"I couldn't turn that into a checkable task right now ({reason}). "
                              f"Nothing was executed — try again when the model provider is reachable.")

        # 5) honest dead end
        return None, "No planner is available for this request, so I did nothing."

    # ------------------------------------------------------------------ helpers
    def _skill_completion(self, text: str, steps) -> Any:
        """Completion for a skill run = the contract-derived effects of its final
        step (never invented). The contract is the authority."""
        from ..core.state import Condition, Predicate
        if self.registry is not None:
            last = steps[-1]
            tool = self.registry.get(last.tool)
            if tool is not None:
                effects = tool.contract.resolve_effects(last.args)
                if effects:
                    return Condition(all_of=effects)
        # conservative fallback: the read/skill signal of the last step
        return Condition(all_of=[Predicate(path=f"reads.{steps[-1].tool}", op="==",
                                           value=True)])

    def _created(self, task: Task) -> None:
        if self.bus is not None:
            self.bus.emit("TASK_CREATED", task_id=task.id, request=task.user_request,
                          planned_by=task.planned_by)
