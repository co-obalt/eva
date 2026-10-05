"""Scheduler tools: reminders persist in SQLite and fire as bus events."""
from __future__ import annotations

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.task import RiskLevel
from . import note_read, read_effect


def register_all(reg: ToolRegistry, settings, scheduler=None) -> None:
    async def add_reminder(args, ctx: ToolContext) -> ToolResult:
        if scheduler is None:
            return ToolResult(ok=False, note="scheduler not wired")
        minutes = float(args["minutes"])
        text = str(args["text"])
        job_id = scheduler.add_reminder(minutes, text)
        note_read(ctx, "scheduler.add_reminder")
        when = f"{minutes:.0f} min" if minutes < 90 else f"{minutes / 60:.1f} h"
        return ToolResult(ok=True, outputs={"job": job_id, "when": when, "text": text},
                          observed=[ctx.perception.note("reads.scheduler.add_reminder", True)],
                          note=f"reminder set for +{when}")

    reg.register(ToolContract(
        name="scheduler.add_reminder", category="system",
        description="Schedule a reminder (fires even after restarts)",
        params={"minutes": ParamSpec(type="float"), "text": ParamSpec()},
        effects=read_effect("scheduler.add_reminder"),
        verification=VerificationSpec(method="L0_scheduler_row", timeout_s=5.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.LOW), add_reminder)

    async def list_jobs(args, ctx: ToolContext) -> ToolResult:
        if scheduler is None:
            return ToolResult(ok=False, note="scheduler not wired")
        jobs = scheduler.list_jobs()
        note_read(ctx, "scheduler.list_jobs")
        return ToolResult(ok=True, outputs={"jobs": jobs},
                          observed=[ctx.perception.note("reads.scheduler.list_jobs", True)])

    reg.register(ToolContract(
        name="scheduler.list_jobs", category="system", description="List scheduled jobs",
        params={}, effects=read_effect("scheduler.list_jobs"),
        verification=VerificationSpec(method="L0_scheduler_row", timeout_s=5.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), list_jobs)
