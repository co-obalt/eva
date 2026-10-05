"""Terminal tool (doc 05 §2): allowlisted, sandboxed, timeout-bounded.

No shell metacharacters (no pipes/redirects/chaining); argv is parsed with
shlex and executed via asyncio subprocess. Non-allowlisted commands return a
BLOCKED result with a precise ask — the user can always run it themselves.
"""
from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel

READONLY = {
    "dir", "ls", "ipconfig", "tasklist", "where", "whoami", "hostname", "systeminfo",
    "ver", "python", "py", "pip", "git", "node", "npm", "npx", "ping", "tracert",
    "nslookup", "curl", "tree", "type", "find", "findstr", "fc", "wmic",
}
# package/dev operations only — filesystem mutations belong to files.* tools where
# the risk engine sizes and gates them (doc 05 §2: no tunnel under the gate)
MUTATING = {"npm", "pip", "git", "python", "py"}
SHELL_BUILTINS = {"dir", "type", "where", "whoami", "ver", "find", "findstr", "fc", "tree"}
_META = re.compile(r"[;&|<>`$]|\|\||&&")


def register_all(reg: ToolRegistry, settings) -> None:
    async def run(args, ctx: ToolContext) -> ToolResult:
        command = str(args["command"]).strip()
        cwd = str(args.get("cwd") or "").strip() or str(Path.home())
        if _META.search(command):
            return ToolResult(ok=False, blocked=True,
                              note="command contains shell metacharacters (;&|<>`$)",
                              ask="Run that compound command yourself; I only run single allowlisted commands.")
        try:
            parts = shlex.split(command, posix=False)
        except ValueError:
            return ToolResult(ok=False, note="could not parse command")
        if not parts:
            return ToolResult(ok=False, note="empty command")
        head = parts[0].strip('"').casefold()
        if head not in READONLY and head not in MUTATING:
            return ToolResult(
                ok=False, blocked=True,
                note=f"command {head!r} is not on the allowlist",
                ask=f"Allowlisted commands: {', '.join(sorted(READONLY))}. "
                    f"Run {command!r} yourself if you need it.")

        mutating = head in MUTATING and command.split() != [head, "--version"]
        argv = ["cmd", "/c", command] if head in SHELL_BUILTINS else parts
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=40.0)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(ok=False, note="command timed out after 40s (FAILED(timeout))")
        except OSError as exc:
            return ToolResult(ok=False, note=f"exec failed: {exc}")
        code = proc.returncode or 0
        text = (out + err).decode("utf-8", "replace")[:8000]
        ctx.perception.note("terminal.last_exit_code", code)
        ctx.perception.note("terminal.last_output", text)
        if code != 0:
            return ToolResult(ok=False, note=f"exit code {code}: {text[:200]}")
        return ToolResult(ok=True, outputs={"exit_code": code, "output": text},
                          note=f"exit {code}", mutated=mutating,
                          observed=[ctx.perception.note("terminal.last_exit_code", code)])

    reg.register(ToolContract(
        name="terminal.run", category="terminal",
        description="Run a single allowlisted command (no pipes/redirects)",
        params={"command": ParamSpec(), "cwd": ParamSpec(required=False)},
        effects=[Predicate(path="terminal.last_exit_code", op="==", value=0)],
        verification=VerificationSpec(method="L0_process_exit", timeout_s=45.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT,
        risk=RiskLevel.LOW,
        risk_context=lambda a: RiskLevel.MEDIUM if (
            a.get("command", "").split(" ")[0].casefold() in MUTATING) else RiskLevel.LOW,
        parallel_safe=True,
    ), run)
