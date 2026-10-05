"""OS tools — L1/L2 based, near-free (doc 05 §2)."""
from __future__ import annotations

import asyncio
import ctypes
import time

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from ..perception import l1_system as l1
from ..perception import l2_a11y as l2
from . import note_read, read_effect


def register_all(reg: ToolRegistry, settings) -> None:
    # ------------------------------------------------------------------ windows
    async def list_windows(args, ctx: ToolContext) -> ToolResult:
        windows = await asyncio.to_thread(l1.list_windows)
        note_read(ctx, "os.list_windows")
        return ToolResult(ok=True, outputs={"windows": windows,
                                            "count": len(windows)},
                          observed=[ctx.perception.note("reads.os.list_windows", True, "L1")])

    reg.register(ToolContract(
        name="os.list_windows", category="os", description="Enumerate visible windows (L1)",
        params={}, effects=read_effect("os.list_windows"),
        verification=VerificationSpec(method="L1_window_enum", timeout_s=3.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), list_windows)

    async def focus_window(args, ctx: ToolContext) -> ToolResult:
        name = args["name"]
        found = await asyncio.to_thread(l1.focus_window, name)
        if found is None:
            return ToolResult(ok=False, note=f"no visible window matching {name!r}",
                              blocked=True, ask=f"Open {name!r} first, then ask me to continue.")
        return ToolResult(ok=True, outputs={"window": found}, mutated=True)

    reg.register(ToolContract(
        name="os.focus_window", category="os", description="Focus a window by title/process (L1)",
        params={"name": ParamSpec(doc="window title or process substring")},
        preconditions_for=lambda a: [
            Predicate(path="system.windows", op="contains", value=a["name"])],
        effects_for=lambda a: [
            Predicate(path="system.foreground", op="contains", value=a["name"])],
        verification=VerificationSpec(method="L1_foreground_window", timeout_s=5.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), focus_window)

    # ---------------------------------------------------------------- resources
    async def get_resources(args, ctx: ToolContext) -> ToolResult:
        r = await asyncio.to_thread(l1.resources)
        m = await asyncio.to_thread(l1.machine)
        note_read(ctx, "os.get_resources")
        ctx.perception.note("system.reported", True)
        return ToolResult(ok=True, outputs={"resources": r, "machine": m},
                          observed=[ctx.perception.note("reads.os.get_resources", True, "L1")])

    reg.register(ToolContract(
        name="os.get_resources", category="os",
        description="CPU / RAM / disk / machine report (pure L1, zero LLM)",
        params={}, effects=read_effect("os.get_resources"),
        verification=VerificationSpec(method="L1_resource_query", timeout_s=3.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), get_resources)

    # ------------------------------------------------------------------- a11y L2
    async def click_control(args, ctx: ToolContext) -> ToolResult:
        control = await asyncio.to_thread(l2.find_control, args["window"], args["control"])
        if control is None:
            return ToolResult(ok=False,
                              note=f"control {args['control']!r} not found in window "
                                   f"{args['window']!r} (a11y tree)")
        ok = await asyncio.to_thread(l2.click_control, control)
        if not ok:
            return ToolResult(ok=False, note="BM_CLICK failed on the control")
        key = f"{args['window']}:{args['control']}"
        ctx.perception.note("ui.last_control_click", key, "L2")
        return ToolResult(ok=True, outputs={"control": control}, mutated=True)

    reg.register(ToolContract(
        name="os.click_control", category="os",
        description="Click a named control via its a11y/Win32 handle (never coordinates)",
        params={"window": ParamSpec(), "control": ParamSpec()},
        preconditions=[], effects=[Predicate(path="ui.last_control_click", op="exists")],
        verification=VerificationSpec(method="L2_a11y_state_diff", timeout_s=4.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), click_control)

    async def type_text(args, ctx: ToolContext) -> ToolResult:
        target = args.get("window") or ""
        if target:
            if await asyncio.to_thread(l1.focus_window, target) is None:
                return ToolResult(ok=False, note=f"window {target!r} not found",
                                  blocked=True, ask=f"Open {target!r} first.")
        text = args["text"]

        def _type() -> bool:
            try:
                user32 = ctypes.windll.user32
                KEYEVENTF_UNICODE = 0x0004
                KEYEVENTF_KEYUP = 0x0002
                for ch in text:
                    for flag in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                        user32.keybd_event(0, ord(ch), flag, 0)
                        time.sleep(0.004)
                return True
            except Exception:
                return False

        ok = await asyncio.to_thread(_type)
        if not ok:
            return ToolResult(ok=False, note="input dispatch failed")
        ctx.perception.note("ui.last_typed", text, "L2")
        return ToolResult(ok=True, outputs={"chars": len(text)}, mutated=True)

    reg.register(ToolContract(
        name="os.type_text", category="os", description="Type text into a window via keyboard input",
        params={"text": ParamSpec(), "window": ParamSpec(required=False)},
        effects=[Predicate(path="ui.last_typed", op="exists")],
        verification=VerificationSpec(method="L2_input_dispatch", timeout_s=4.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.MEDIUM,
        ui_lock=True, uiaction=True), type_text)

    # ------------------------------------------------------------------ capture
    async def capture_evidence(args, ctx: ToolContext) -> ToolResult:
        levels = [lv.strip() for lv in str(args.get("exhausted_levels", "")).split(",") if lv.strip()]
        ctx.perception.policy.require_observation(
            "L6", reason=args.get("reason", ""), exhausted_levels=levels, task=ctx.task)
        path = await ctx.perception.capture_evidence(label="manual")
        if path is None:
            return ToolResult(ok=False, note="screen capture unavailable")
        ctx.perception.note("reads.os.capture_evidence", True, "L6", note=path)
        return ToolResult(ok=True, outputs={"evidence": path})

    reg.register(ToolContract(
        name="os.capture_evidence", category="os",
        description="Capture a screenshot as EVIDENCE (L6, requires reason + exhausted levels)",
        params={"reason": ParamSpec(), "exhausted_levels": ParamSpec(
            doc="comma list, e.g. L1,L2,L3,L4,L5")},
        effects=read_effect("os.capture_evidence"),
        verification=VerificationSpec(method="policy_gated_evidence", timeout_s=15.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), capture_evidence)
