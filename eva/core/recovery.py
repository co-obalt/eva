"""Recovery policy table (doc 03 §6). Deterministic ladder first; bounded.

Hard anti-loop rules:
  * same action + same observed state -> max `max_attempts` per step
  * zero-progress counter -> force REPAIR; `max_repairs` repairs -> BLOCKED/FAILED
  * step budget per task (orchestrator) -> force replan reassessment
"""
from __future__ import annotations

from enum import Enum

from .task import Step, StepStatus, Task


class RecoveryAction(str, Enum):
    RETRY = "RETRY"
    ALTERNATIVE = "ALTERNATIVE"
    REPAIR_PLAN = "REPAIR_PLAN"
    BLOCK = "BLOCK"
    FAIL = "FAIL"


class BlockerClass(str, Enum):
    LOGIN = "missing_login"
    PERMISSION = "missing_permission"
    RESOURCE = "missing_resource"
    CONTRADICTORY = "contradictory_state"
    PERCEPTION_GAP = "perception_gap"
    USER_DECISION = "user_decision"
    UNKNOWN = "unknown"


_ASK_TEMPLATES = {
    BlockerClass.LOGIN: "I need you to log in / scan the QR code, then tell me to continue.",
    BlockerClass.PERMISSION: "I need permission for this action — grant it and ask me to continue.",
    BlockerClass.RESOURCE: "A required resource is missing on this machine — fix that and ask me to continue.",
    BlockerClass.CONTRADICTORY: "The current state contradicts the plan — I stopped instead of guessing. Please confirm how to proceed.",
    BlockerClass.PERCEPTION_GAP: "I cannot observe the signal needed to verify this — structural perception is unavailable here.",
    BlockerClass.USER_DECISION: "This needs your decision — I stopped instead of choosing for you.",
    BlockerClass.UNKNOWN: "I am blocked and will not guess — please tell me how to proceed.",
}


class RecoveryPolicy:
    def __init__(self, max_repairs: int = 2, zero_progress_limit: int = 4):
        self.max_repairs = max_repairs
        self.zero_progress_limit = zero_progress_limit

    # -- failed steps --------------------------------------------------------
    def on_failed(self, task: Task, step: Step, *, alternatives_available: bool,
                  tool_error: bool = False) -> RecoveryAction:
        if step.attempts < step.max_attempts:
            # RETRY covers re-perceive: verify() has already polled until its
            # deadline, so a second attempt starts from fresh state (doc 03 §6).
            return RecoveryAction.RETRY
        if alternatives_available and step.status is not StepStatus.VERIFIED:
            return RecoveryAction.ALTERNATIVE
        if task.counters.repairs < self.max_repairs:
            return RecoveryAction.REPAIR_PLAN
        return RecoveryAction.FAIL

    def zero_progress_triggered(self, task: Task) -> bool:
        return task.counters.consecutive_non_verified >= self.zero_progress_limit

    def can_repair(self, task: Task) -> bool:
        return task.counters.repairs < self.max_repairs

    # -- blocked steps --------------------------------------------------------
    def classify_blocker(self, *, precondition_failed: bool, login_related: bool,
                         perception_gap: bool) -> BlockerClass:
        if perception_gap:
            return BlockerClass.PERCEPTION_GAP
        if login_related:
            return BlockerClass.LOGIN
        if precondition_failed:
            return BlockerClass.CONTRADICTORY
        return BlockerClass.UNKNOWN

    def ask_for(self, blocker: BlockerClass) -> str:
        return _ASK_TEMPLATES.get(blocker, _ASK_TEMPLATES[BlockerClass.UNKNOWN])
