"""Risk engine (doc 05 §3, P9). Only the engine classifies risk; LLM output
cannot set or bypass it. HIGH → confirm; CRITICAL → confirm with concrete
consequences immediately before execution. Timeout policy: cancel (fail-safe).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .contracts import ToolContract
from .task import RiskLevel, Task

_MEDIA_EXT = {".mp4", ".mov", ".mkv", ".jpg", ".jpeg", ".png", ".heic", ".psd", ".raw", ".avi"}
_BIG_BYTES = 100 * 1024 * 1024  # doc 05: > 100 MB or user media -> CRITICAL delete


class RiskDecision(BaseModel):
    level: RiskLevel
    requires_confirmation: bool = False
    immediate: bool = False                      # CRITICAL: ask right before execution
    title: str = ""
    consequences: str = ""
    reason: str = ""

    def gate_line(self) -> str:
        return f"{self.level.value.upper()} — {self.title}: {self.consequences}"


class RiskGate:
    def __init__(self, allowed_roots: tuple[Path, ...] = (), big_bytes: int = _BIG_BYTES):
        self.allowed_roots = tuple(Path(p).resolve() for p in allowed_roots)
        self.big_bytes = big_bytes

    # -- public --------------------------------------------------------------
    def classify(self, contract: ToolContract, args: dict[str, Any], task: Task | None = None) -> RiskDecision:
        level = contract.risk
        reasons: list[str] = [f"static:{contract.risk.value}"]

        if contract.risk_context:
            dyn = contract.risk_context(args)
            if isinstance(dyn, RiskLevel):
                level = RiskLevel.max(level, dyn)
                reasons.append(f"dynamic:{dyn.value}")

        # destructive: size + location checks
        target = args.get("path") or args.get("target")
        if contract.idempotency.value == "destructive" and isinstance(target, str):
            p = Path(target)
            size = _tree_size(p)
            if size > self.big_bytes:
                level = RiskLevel.max(level, RiskLevel.CRITICAL)
                reasons.append(f"size:{size}")
            elif p.suffix.lower() in _MEDIA_EXT:
                level = RiskLevel.max(level, RiskLevel.HIGH)
                reasons.append("media")
            if self.allowed_roots and not _inside_any(p, self.allowed_roots):
                level = RiskLevel.max(level, RiskLevel.CRITICAL)
                reasons.append("outside_allowed_roots")

        # outbound communication is never below HIGH
        if contract.category in ("comms",):
            level = RiskLevel.max(level, RiskLevel.HIGH)
            reasons.append("outbound_comm")

        if task and task.risk_level.rank > level.rank:
            level = RiskLevel.max(level, task.risk_level)
            reasons.append("task_ceiling")

        decision = RiskDecision(
            level=level,
            requires_confirmation=level in (RiskLevel.HIGH, RiskLevel.CRITICAL),
            immediate=level is RiskLevel.CRITICAL,
            title=contract.name,
            consequences=self._consequences(contract, args, level),
            reason="; ".join(reasons),
        )
        return decision

    # -- helpers -------------------------------------------------------------
    def _consequences(self, contract: ToolContract, args: dict[str, Any], level: RiskLevel) -> str:
        target = args.get("path") or args.get("target")
        if contract.idempotency.value == "destructive" and isinstance(target, str):
            p = Path(target)
            size = _tree_size(p)
            return f"permanently deletes {_human(size)} from {p} (irreversible)"
        if contract.category == "comms":
            to = args.get("recipient") or args.get("to") or args.get("contact")
            text = args.get("text") or args.get("message") or ""
            return f"sends an outbound message to {to!r} that the user will see immediately: {text[:120]!r}"
        if contract.name.endswith("type_text") or contract.name.endswith("type"):
            return f"types text into the focused window of the user's machine"
        if contract.name.endswith("terminal_run") or contract.category == "terminal":
            return f"runs terminal command {args.get('command', '')!r} (mutating, sandboxed allowlist)"
        extra = json.dumps({k: v for k, v in args.items() if k not in ("x", "y")}, default=str)[:160]
        return f"{contract.description} · args={extra}"


def _tree_size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        if p.is_dir():
            total = 0
            for child in p.rglob("*"):
                if child.is_file():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        pass
            return total
    except OSError:
        return 0
    return 0


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _inside_any(p: Path, roots: tuple[Path, ...]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        rp = p
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False
