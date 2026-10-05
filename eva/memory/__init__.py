"""Memory subsystem (doc 06): working · episodic · semantic · procedural · profile.

Memory is a hypothesis store, not gospel (P8): every durable record carries
confidence + source + verify hooks, and low-confidence memory can never drive
autonomous action.
"""
from .envelope import (ACTION_FLOOR, INFORM_FLOOR, MemoryRecord, decay, initial_confidence,
                       register_corroboration)
from .store import MemoryStore, Skill

__all__ = ["ACTION_FLOOR", "INFORM_FLOOR", "MemoryRecord", "decay", "initial_confidence",
           "register_corroboration", "MemoryStore", "Skill"]
