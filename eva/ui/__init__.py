"""Interfaces — renderers only (doc 09 §1). No intelligence lives here."""
from .edex import EdexCLI, main

__all__ = ["EdexCLI", "main"]
