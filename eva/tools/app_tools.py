"""App tools: deterministic launch registry (doc 12 Tier-0 fallback)."""
from __future__ import annotations

import asyncio

from ..apps.registry import find_app, launch_app, list_apps
from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from . import note_read, read_effect

import psutil


def register_all(reg: ToolRegistry, settings) -> None:
    async def find(args, ctx: ToolContext) -> ToolResult:
        app = find_app(args["name"])
        note_read(ctx, "apps.find")
        if app is None:
            return ToolResult(ok=False, note=f"no app registry entry for {args['name']!r}",
                              blocked=True,
                              ask=f"I don't know an executable for {args['name']!r}. "
                                  f"Run it once / tell me the path and I'll add it to data/apps.json.")
        return ToolResult(ok=True, outputs={"name": app.name, "exe": app.exe,
                                            "process": app.process},
                          observed=[ctx.perception.note("reads.apps.find", True, "L1")])

    reg.register(ToolContract(
        name="apps.find", category="apps", description="Resolve an app name to its executable",
        params={"name": ParamSpec()}, effects=read_effect("apps.find"),
        verification=VerificationSpec(method="L3_registry_lookup", timeout_s=3.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), find)

    async def launch(args, ctx: ToolContext) -> ToolResult:
        query = args["name"]
        app = find_app(query)
        if app is None or not app.exe:
            return ToolResult(ok=False, blocked=True,
                              note=f"no executable known for {query!r}",
                              ask=f"Tell me the full path of {query!r} and I'll register it.")
        ok = await asyncio.to_thread(launch_app, app)
        if not ok:
            return ToolResult(ok=False, note=f"could not start {app.exe}")
        return ToolResult(ok=True, outputs={"name": app.name, "exe": app.exe}, mutated=True)

    reg.register(ToolContract(
        name="apps.launch", category="apps", description="Launch an application (idempotent)",
        params={"name": ParamSpec(doc="registry name, e.g. chrome / notepad / vscode")},
        effects_for=lambda a: [Predicate(path="apps.running", op="==", value=True,
                                         args={"name": _canon(a["name"])})],
        verification=VerificationSpec(method="L1_process", timeout_s=12.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), launch)

    async def close(args, ctx: ToolContext) -> ToolResult:
        query = args["name"]
        app = find_app(query)
        if app is None:
            return ToolResult(ok=False, note=f"unknown app {query!r}")
        killed = 0
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info["name"] or "").casefold().removesuffix(".exe")
                if any(name == p.casefold().removesuffix(".exe") for p in app.process):
                    proc.terminate()
                    killed += 1
            except Exception:
                continue
        if killed == 0:
            return ToolResult(ok=False, note=f"{app.name} is not running")
        return ToolResult(ok=True, outputs={"terminated": killed}, mutated=True,
                          note=f"terminated {killed} process(es)")

    reg.register(ToolContract(
        name="apps.close", category="apps", description="Terminate a running application",
        params={"name": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="apps.running", op="==", value=True,
                                               args={"name": _canon(a["name"])})],
        effects_for=lambda a: [Predicate(path="apps.running", op="==", value=False,
                                         args={"name": _canon(a["name"])})],
        verification=VerificationSpec(method="L1_process", timeout_s=10.0),
        idempotency=IdempotencyClass.DESTRUCTIVE,
        risk=RiskLevel.MEDIUM,
        risk_context=lambda a: RiskLevel.HIGH
        if _canon(a.get("name", "")) in ("word", "excel", "powerpoint", "vscode")
        else RiskLevel.MEDIUM,
    ), close)

    async def list_known(args, ctx: ToolContext) -> ToolResult:
        apps = list_apps()
        note_read(ctx, "apps.list")
        return ToolResult(ok=True, outputs={"apps": apps},
                          observed=[ctx.perception.note("reads.apps.list", True, "L1")])

    reg.register(ToolContract(
        name="apps.list", category="apps", description="List known registered apps",
        params={}, effects=read_effect("apps.list"),
        verification=VerificationSpec(method="L3_registry_lookup", timeout_s=3.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), list_known)


def _canon(name: str) -> str:
    app = find_app(name)
    return app.name if app else str(name).casefold()
