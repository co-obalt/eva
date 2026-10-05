"""Task model (doc 03 §1-§3). The completion condition is the contract;
task status is derived ONLY from it at the end (P6)."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .state import Condition, Observation, Predicate, Truth


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]

    @staticmethod
    def max(a: "RiskLevel", b: "RiskLevel") -> "RiskLevel":
        return a if a.rank >= b.rank else b


class StepStatus(str, Enum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    SKIPPED_ALREADY_DONE = "SKIPPED_ALREADY_DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"

    @property
    def done(self) -> bool:
        return self in (StepStatus.VERIFIED, StepStatus.SKIPPED_ALREADY_DONE)


class TaskStatus(str, Enum):
    INTENT = "INTENT"
    DISCOVERING = "DISCOVERING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.FINISHED, TaskStatus.FAILED,
                        TaskStatus.CANCELLED, TaskStatus.BLOCKED)


class Evidence(BaseModel):
    at: float = Field(default_factory=time.time)
    level: str = "L0"
    path: str = ""
    value: Any = None
    truth: Truth = Truth.TRUE
    note: str = ""

    @classmethod
    def from_observation(cls, obs: Observation) -> "Evidence":
        return cls(at=obs.at, level=obs.level, path=obs.path, value=obs.value,
                   truth=obs.truth, note=obs.note)


class FailureRecord(BaseModel):
    at: float = Field(default_factory=time.time)
    step: int
    kind: str            # verification_failed | tool_error | policy_rejection | precondition | blocked
    expected: str = ""
    observed: str = ""
    detail: str = ""
    attempt: int = 1


class Step(BaseModel):
    n: int
    intent: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[Predicate] = Field(default_factory=list)
    expected_effect: list[Predicate] = Field(default_factory=list)
    verification_timeout_s: float = 8.0
    risk: RiskLevel | None = None          # informational; engine is authoritative (P9)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    max_attempts: int = 2
    evidence: list[Evidence] = Field(default_factory=list)
    started: float | None = None
    ended: float | None = None
    structural_target: bool = True         # contracts override; INV-2 enforcement input
    note: str = ""

    @property
    def action_hash(self) -> str:
        payload = json.dumps({"tool": self.tool, "args": self.args}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def effect_describe(self) -> str:
        return "; ".join(p.describe() for p in self.expected_effect)


class TaskCounters(BaseModel):
    """Instrumentation for doc-11 assertions (S6e: l5/l6/l7/coord == 0)."""
    actions_run: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    l5_calls: int = 0
    l6_calls: int = 0
    l7_calls: int = 0
    coord_actions: int = 0
    policy_rejections: int = 0
    ui_lock_waits: float = 0.0
    replans: int = 0
    repairs: int = 0
    consecutive_non_verified: int = 0
    redundant_actions: int = 0
    cache_hits: int = 0
    actor_calls: int = 0            # decisions requested from the actor loop
    actor_tokens: int = 0           # in+out tokens spent inside the actor loop
    escalations: int = 0            # fast lane -> actor handoffs
    rule_rejections: int = 0        # actions blocked by a remembered user rule


class Task(BaseModel):
    id: str = Field(default_factory=lambda: f"tsk_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}")
    created_at: float = Field(default_factory=time.time)
    source: str = "cli"                     # cli | voice | schedule | event
    user_request: str = ""
    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    priority: str = "normal"
    risk_level: RiskLevel = RiskLevel.LOW   # task ceiling: may RAISE gates, never lower
    completion: Condition = Field(default_factory=Condition)
    context: dict[str, Any] = Field(default_factory=dict)
    plan: list[Step] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.INTENT
    counters: TaskCounters = Field(default_factory=TaskCounters)
    planned_by: str = ""                    # recipe | skill:<name> | actor | repair
    mode: str = "plan"                      # plan = frozen step list | actor = one action per cycle
    result_message: str = ""
    origin_skill: str | None = None
    resumed: bool = False

    # -- helpers ------------------------------------------------------------
    def next_step(self) -> Step | None:
        for s in self.plan:
            if s.status in (StepStatus.PENDING,):
                return s
        return None

    def step_by_n(self, n: int) -> Step | None:
        for s in self.plan:
            if s.n == n:
                return s
        return None

    def bump_verified(self, step: Step) -> None:
        # a step skipped because the world already satisfied its effect stays a
        # skip (P3); bump_verified must not silently relabel it as VERIFIED
        if step.status is not StepStatus.SKIPPED_ALREADY_DONE:
            step.status = StepStatus.VERIFIED
        step.ended = time.time()
        self.counters.consecutive_non_verified = 0

    def bump_non_verified(self) -> None:
        self.counters.consecutive_non_verified += 1

    def record_failure(self, rec: FailureRecord) -> None:
        self.failures.append(rec)

    def validate(self) -> None:
        self.completion.validate_nonempty()
        if not self.goal:
            raise ValueError("task requires a goal")
        plan_ns = [s.n for s in self.plan]
        if len(plan_ns) != len(set(plan_ns)):
            raise ValueError("duplicate step numbers in plan")

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> "Task":
        return cls.model_validate_json(raw)
