"""Confidence envelope (doc 06 §2, P8).

  source = user_statement           -> 0.95
  source = verified_task (observed) -> 0.90 + 0.02 * corroborating_runs (cap 0.99)
  source = inference (LLM)          -> <= 0.60

Decay half-lives by type; verification events move confidence; invalid memory
is excluded from retrieval and triggers re-learning.
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

DAY = 86400.0

HALF_LIFE_S: dict[str, float] = {
    "profile": 180 * DAY,
    "semantic": 120 * DAY,
    "episodic": 21 * DAY,
    "procedural": 120 * DAY,
    "inference": 3 * DAY,
}

ACTION_FLOOR = 0.8     # may drive autonomous action
INFORM_FLOOR = 0.4     # below this: hint only, never a fact in context


def initial_confidence(source: str) -> float:
    if source == "user_statement":
        return 0.95
    if source == "verified_task":
        return 0.90
    if source == "inference":
        return 0.60
    return 0.70  # import/other


def register_corroboration(base: float, corroborating_runs: int) -> float:
    return min(0.99, base + 0.02 * max(0, corroborating_runs))


def decay(confidence: float, age_s: float, type_: str) -> float:
    half = HALF_LIFE_S.get(type_, 30 * DAY)
    if half <= 0:
        return confidence
    return max(0.0, confidence * (0.5 ** (age_s / half)))


def verified(confidence: float, now_bonus: float = 0.05) -> float:
    return min(0.99, confidence + now_bonus)


def invalidated(confidence: float) -> float:
    return min(confidence, 0.1)


class MemoryRecord(BaseModel):
    id: str
    type: str = "semantic"           # episodic | semantic | procedural | profile | working
    source: str = "inference"        # verified_task | user_statement | inference | import
    content: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.6
    usage_count: int = 0
    created_at: float = Field(default_factory=time.time)
    last_used: float | None = None
    last_verified: float | None = None
    verify_method: str = ""
    invalid: bool = False
    tags: list[str] = Field(default_factory=list)

    def effective_confidence(self, now: float | None = None) -> float:
        if self.invalid:
            return 0.0
        age = (now or time.time()) - (self.last_verified or self.created_at)
        return decay(self.confidence, age, self.type)

    def may_drive_action(self) -> bool:
        return (not self.invalid) and self.effective_confidence() >= ACTION_FLOOR

    def may_inform(self) -> bool:
        return (not self.invalid) and self.effective_confidence() >= INFORM_FLOOR
