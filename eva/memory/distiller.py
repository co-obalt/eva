"""Skill distillation (doc 06 §5): >=2 similar verified runs with a stable plan
become a procedural skill; execution then costs 0 planning tokens.

A skill never locks EVA in: failing runs drop success_rate and a skill below
threshold is quarantined back to the LLM path.
"""
from __future__ import annotations

import re

from ..core.task import Task, TaskStatus
from .store import MemoryStore, Skill, intent_signature

_NAME_CLEAN = re.compile(r"[^a-z0-9_]+")


class SkillDistiller:
    def __init__(self, memory: MemoryStore, bus=None, min_runs: int = 2):
        self.memory = memory
        self.bus = bus
        self.min_runs = min_runs

    def consider(self, task: Task) -> str | None:
        if task.status is not TaskStatus.FINISHED:
            return None
        if task.planned_by.startswith("skill:"):
            return None                       # already a skill run
        if any(f.kind in ("blocked", "policy_rejection") for f in task.failures):
            return None                       # never distill a run that needed intervention
        sig = intent_signature(task.user_request)
        if not sig:
            return None
        similar = self.memory.find_similar_episodes(sig, limit=6)
        # only runs that actually executed a plan count (no-op runs execute nothing)
        finished = [e for e in similar if e.get("outcome") == TaskStatus.FINISHED.value
                    and e.get("tools")]
        if len(finished) < self.min_runs:
            return None

        seqs = {tuple(e.get("tools", [])) for e in finished}
        if len(seqs) != 1:
            return None                       # plan not stable across runs
        tools = list(next(iter(seqs)))
        if not tools or len(tools) != len(task.plan):
            return None

        steps = []
        for s in task.plan:
            if not s.status.done:
                return None                   # partial run: not distillable
            steps.append({"tool": s.tool, "args": s.args})
        if not steps:
            return None

        words = [w for w in sig.split() if len(w) > 2][:4]
        name = "skill_" + "_".join(words) if words else f"skill_{task.id[-6:]}"
        name = _NAME_CLEAN.sub("_", name).strip("_")[:60]
        if not name.startswith("skill_"):
            name = "skill_" + name
        skill = Skill(name=name, intent_sig=sig, steps=steps,
                      success_rate=1.0, runs=len(finished), confidence=0.9)
        self.memory.upsert_skill(skill)
        return name
