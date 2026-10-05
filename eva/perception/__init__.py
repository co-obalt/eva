"""Perception & interaction policy (doc 04, doc 13). Implemented in Phase 2.

Ladder: L0 internal | L1 OS APIs | L2 a11y | L3 app APIs (CDP) | L4 DOM |
L5 OCR | L6 screenshot | L7 vision LLM. L5+ requires an authorized
justification (`reason` + `exhausted_levels`) via the PolicyGate.
"""
from .policy import PolicyGate, PolicyViolation

__all__ = ["PolicyGate", "PolicyViolation"]
