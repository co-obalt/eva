"""Persona: mood vector + archetypes (EVA_INTELLIGENT_RESOURCES §3).

Text flavor and avatar ambience ONLY — the persona never touches control flow,
risk, or verification.
"""
from .mood import ARCHETYPES, MoodVector, Persona

__all__ = ["ARCHETYPES", "MoodVector", "Persona"]
