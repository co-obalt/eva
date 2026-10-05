"""Skill runtime (doc 06 §5): matched skills execute as verified contracts —
never synthesized code. A skill failure falls back to the LLM path via repair.
"""
from __future__ import annotations

from ..core.task import Step
from ..memory.store import Skill


def skill_to_steps(skill: Skill, start: int = 1) -> list[Step]:
    steps: list[Step] = []
    for i, raw in enumerate(skill.steps, start=start):
        steps.append(Step(n=i, intent=f"[{skill.name}] {raw.get('tool')}",
                          tool=str(raw.get("tool")), args=dict(raw.get("args") or {})))
    return steps


class SkillPlanner:
    """PlannerLike wrapper: plan() returns the skill's stored recipe."""

    def __init__(self, skill: Skill):
        self.skill = skill

    async def plan(self, task, *, state=None, memory_slice=None) -> list[Step]:
        return skill_to_steps(self.skill)

    async def repair(self, task, *, reason, state=None) -> list[Step] | None:
        return None   # fallback: the composite planner routes to the LLM path
