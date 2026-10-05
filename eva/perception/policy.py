"""Perception Policy gate (doc 04 "Hard invariants", doc 13).

Runtime-enforced, not prose:
  INV-1/INV-3  L5/L6/L7 requests without `reason` + `exhausted_levels` rejected
  INV-2        coordinate click(x,y) rejected without recorded structural-UNKNOWN
               justification; accepted (and counted) with one
  Counters     every allowed expensive call increments the task counters so
               S6e can assert l5/l6/l7/coord == 0 for structural scenarios.
"""
from __future__ import annotations

from typing import Any


class PolicyViolation(RuntimeError):
    """Raised when a request violates the interaction policy. Never caught silently."""


_EXPENSIVE = {"L5", "L6", "L7"}


class PolicyGate:
    def __init__(self, allow_vision: bool = False) -> None:
        self.allow_vision = allow_vision
        self.task: Any = None
        self.log: list[dict[str, Any]] = []

    def bind_task(self, task: Any) -> None:
        self.task = task

    # -- INV-2 ---------------------------------------------------------------
    def check_coordinate_action(self, contract: Any, args: dict[str, Any],
                                task: Any = None) -> tuple[bool, str]:
        """A pointer action must justify that structural targeting was tried first."""
        params = set(getattr(contract, "params", {}) or {})
        if not getattr(contract, "structural_target", False):
            return True, ""
        if "x" not in params and "y" not in params:
            return True, ""  # not a pointer tool
        if "x" not in args and "y" not in args:
            return True, ""  # semantic variant of the same tool
        justification = str(args.get("justification") or args.get("exhausted_levels") or "").strip()
        if not justification:
            return False, (
                "INV-2: coordinate action rejected — no `justification` recorded "
                "(structural targeting: a11y -> DOM/CDP -> named control -> keyboard, all UNKNOWN?)"
            )
        t = task or self.task
        if t is not None:
            t.counters.coord_actions += 1
        self.log.append({"kind": "coordinate", "action": getattr(contract, "name", "?"),
                         "justification": justification})
        return True, ""

    # -- INV-1 / INV-3 ---------------------------------------------------------
    def authorize_observation(self, level: str, reason: str = "",
                              exhausted_levels: Any = None, task: Any = None) -> tuple[bool, str]:
        lv = (level or "L0").upper()
        if lv in _EXPENSIVE:
            if not str(reason or "").strip():
                return False, f"INV-3: {lv} request rejected — missing `reason`"
            if not exhausted_levels:
                return False, (f"INV-3: {lv} request rejected — missing `exhausted_levels` "
                               f"(which cheap levels already returned UNKNOWN)")
            iff = lv == "L7"
            if iff and not self.allow_vision:
                return False, "INV-1: vision disabled (EVA_ALLOW_VISION=0) — cannot escalate to L7"
            t = task or self.task
            if t is not None:
                if lv == "L5":
                    t.counters.l5_calls += 1
                elif lv == "L6":
                    t.counters.l6_calls += 1
                else:
                    t.counters.l7_calls += 1
            self.log.append({"kind": "observation", "level": lv, "reason": reason,
                             "exhausted_levels": list(exhausted_levels or [])})
        return True, ""

    def require_observation(self, level: str, reason: str = "", exhausted_levels: Any = None,
                            task: Any = None) -> None:
        ok, why = self.authorize_observation(level, reason, exhausted_levels, task)
        if not ok:
            raise PolicyViolation(why)
