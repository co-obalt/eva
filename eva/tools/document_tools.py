"""Document tools: HTML->PDF via the user's own Chrome (headless), text extraction.

No new dependencies — Chrome is already on the machine (doc 08 dependency gate).
"""
from __future__ import annotations

import asyncio
import html as html_mod
import re
from html.parser import HTMLParser
from pathlib import Path

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from ..perception.l3_cdp import chrome_binary
from . import note_read, read_effect


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def register_all(reg: ToolRegistry, settings) -> None:
    async def render_pdf(args, ctx: ToolContext) -> ToolResult:
        src = Path(args["html_path"]).expanduser()
        dst = Path(args["pdf_path"]).expanduser()
        if not src.exists():
            return ToolResult(ok=False, note=f"{src} does not exist")
        exe = chrome_binary()
        if exe is None:
            return ToolResult(ok=False, blocked=True, note="Chrome not found",
                              ask="Install Chrome (or tell me another headless browser) and retry.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(exe), "--headless=new", "--disable-gpu", "--no-first-run",
               f"--print-to-pdf={dst}", src.resolve().as_uri()]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except (OSError, asyncio.TimeoutError) as exc:
            return ToolResult(ok=False, note=f"headless print failed: {exc}")
        if not dst.exists() or dst.stat().st_size == 0:
            return ToolResult(ok=False, note="PDF was not produced")
        return ToolResult(ok=True, outputs={"pdf": str(dst), "bytes": dst.stat().st_size},
                          mutated=True)

    reg.register(ToolContract(
        name="documents.render_pdf", category="documents",
        description="Render an HTML file to PDF via headless Chrome",
        params={"html_path": ParamSpec(), "pdf_path": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                               args={"path": a["html_path"]})],
        effects_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                         args={"path": a["pdf_path"]}),
                               Predicate(path="files.size", op=">", value=0,
                                         args={"path": a["pdf_path"]})],
        verification=VerificationSpec(method="fs_stat", timeout_s=30.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.LOW), render_pdf)

    async def extract_text(args, ctx: ToolContext) -> ToolResult:
        p = Path(args["path"]).expanduser()
        if not p.exists():
            return ToolResult(ok=False, note=f"{p} does not exist")
        text = await asyncio.to_thread(p.read_text, "utf-8", "replace")
        if p.suffix.lower() in (".html", ".htm"):
            stripper = _Stripper()
            try:
                stripper.feed(text)
                text = "\n".join(stripper.parts)
            except Exception:
                text = re.sub(r"<[^>]+>", " ", text)
        text = html_mod.unescape(text)[:20000]
        note_read(ctx, "documents.extract_text")
        return ToolResult(ok=True, outputs={"text": text, "chars": len(text)},
                          observed=[ctx.perception.note("reads.documents.extract_text", True, "L1")])

    reg.register(ToolContract(
        name="documents.extract_text", category="documents",
        description="Extract text from a text/HTML file",
        params={"path": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                               args={"path": a["path"]})],
        effects=read_effect("documents.extract_text"),
        verification=VerificationSpec(method="fs_read", timeout_s=10.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), extract_text)
