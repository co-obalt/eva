"""Mood vector (Affection/Engagement/Mood/Energy) + 13 archetypes.

Pure flavor layer: it shapes *how* EVA speaks and looks, never what she does.
Persisted in profiles_kv so it survives restarts.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

ARCHETYPES: dict[str, str] = {
    "tsundere": "cold at first, secretly loyal — 'It's not like I wanted to do this... but it's done.'",
    "kuudere": "calm, precise, JARVIS-like efficiency. Minimal, exact sentences.",
    "dandere": "soft, quiet, gentle — executes with extreme precision.",
    "deredere": "warm, affectionate, cheerful assistant.",
    "genki": "high-energy: 'Let's do this right now!'",
    "yandere": "obsessively devoted to your tasks and your machine's health.",
    "teasedere": "playful, gently teases while finishing tasks flawlessly.",
    "dorodere": "sweet on the outside, sharp wit inside.",
    "utsudere": "melancholic, quiet, needs a little encouragement.",
    "bakadere": "clumsy but energetic and surprisingly loyal.",
    "darudere": "lazy, casual, short minimal phrases: 'Done. What's next?'",
    "hinedere": "cynical, unimpressed — 'Fine. It worked.'",
    "sadodere": "playfully strict, demands efficiency.",
}

DEFAULT_ARCHETYPE = "kuudere"


class MoodVector(BaseModel):
    affection: float = 55.0     # [0,100]
    engagement: float = 60.0    # [0,100]
    mood: float = 65.0          # [-100,100]
    energy: float = 80.0        # [0,100]
    archetype: str = DEFAULT_ARCHETYPE
    updated_at: float = Field(default_factory=time.time)

    def clamp(self) -> None:
        self.affection = max(0.0, min(100.0, self.affection))
        self.engagement = max(0.0, min(100.0, self.engagement))
        self.mood = max(-100.0, min(100.0, self.mood))
        self.energy = max(0.0, min(100.0, self.energy))


class Persona:
    def __init__(self, memory=None):
        self.memory = memory
        self.vector = MoodVector()
        self._load()

    def _load(self) -> None:
        if self.memory is None:
            return
        try:
            saved = self.memory.profile_get("persona")
            if isinstance(saved, dict):
                self.vector = MoodVector(**saved)
        except Exception:
            pass

    def _save(self) -> None:
        if self.memory is None:
            return
        try:
            self.memory.profile_set("persona", self.vector.model_dump())
        except Exception:
            pass

    # ------------------------------------------------------------------ updates
    def on_event(self, event) -> None:
        t = event.type
        v = self.vector
        if t == "TASK_FINISHED" and event.payload.get("status") == "success":
            v.mood = min(100, v.mood + 4)
            v.energy = max(0, v.energy - 3)
            v.affection = min(100, v.affection + 1)
        elif t == "TASK_FAILED":
            v.mood = max(-100, v.mood - 5)
            v.energy = max(0, v.energy - 4)
        elif t == "TASK_CANCELLED":
            v.mood = max(-100, v.mood - 2)
        elif t == "TASK_STARTED":
            v.engagement = min(100, v.engagement + 5)
        elif t == "TASK_WAITING_USER":
            v.engagement = min(100, v.engagement + 2)
        elif t in ("TASK_STEP_RESULT",) and event.payload.get("status") == "FAILED":
            v.mood = max(-100, v.mood - 1)
        v.clamp()
        v.updated_at = time.time()
        self._save()

    def set_archetype(self, name: str) -> bool:
        if name not in ARCHETYPES:
            return False
        self.vector.archetype = name
        self._save()
        return True

    # ------------------------------------------------------------------ flavor
    def flavor(self, text: str) -> str:
        """Light shaping of a user-facing line. Never changes facts."""
        v = self.vector
        if v.archetype == "kuudere" or v.mood < -25:
            return text
        prefix = ""
        if v.archetype == "genki":
            prefix = "✦ "
        elif v.archetype == "deredere" and v.mood > 40:
            prefix = "♡ "
        elif v.archetype == "tsundere":
            prefix = "…hm. " if v.mood < 50 else ""
        elif v.archetype == "darudere":
            prefix = ""
        suffix = ""
        if v.archetype == "yandere" and "FAILED" not in text and "BLOCKED" not in text:
            suffix = " (Your machine is safe.)"
        return f"{prefix}{text}{suffix}"

    def mood_line(self) -> str:
        v = self.vector
        return (f"affection {v.affection:.0f} · engagement {v.engagement:.0f} · "
                f"mood {v.mood:+.0f} · energy {v.energy:.0f} · archetype {v.archetype}")

    def ambient_color(self) -> str:
        """Avatar environment color from mood (doc 09 §4 EVA_WAITING etc.)."""
        if self.vector.mood < -20:
            return "#3a4a6b"
        if self.vector.mood < 20:
            return "#101828"
        if self.vector.mood < 60:
            return "#10303a"
        return "#0f2a24"
