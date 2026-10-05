"""Tool inventory (doc 05 §2): OS · Browser (CDP-first) · Files · Terminal · Apps · Documents.

Every tool carries a contract; read-only tools carry an L0 `reads.*` completion
signal (weakest acceptable evidence — they mutate nothing), everything else
verifies through structural state (L1-L4) or the filesystem.
"""
from __future__ import annotations

from ..core.contracts import ToolRegistry
from ..core.state import Predicate


def read_effect(tool: str) -> list[Predicate]:
    return [Predicate(path=f"reads.{tool}", op="==", value=True)]


def note_read(ctx, tool: str) -> None:
    ctx.perception.note(f"reads.{tool}", True)


def build_registry(settings, perception=None, scheduler=None, memory=None) -> ToolRegistry:
    reg = ToolRegistry()
    from . import (agent_tools, app_tools, browser_tools, document_tools, file_tools, os_tools,
                   scheduler_tools, terminal_tools, web_tools)
    os_tools.register_all(reg, settings)
    file_tools.register_all(reg, settings)
    terminal_tools.register_all(reg, settings)
    app_tools.register_all(reg, settings)
    browser_tools.register_all(reg, settings)
    web_tools.register_all(reg, settings)
    document_tools.register_all(reg, settings)
    scheduler_tools.register_all(reg, settings, scheduler)
    agent_tools.register_all(reg, settings, memory)
    return reg
