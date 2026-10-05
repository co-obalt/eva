"""File tools (doc 05 §2). check_before_create / destructive classes; every
mutation verified through the filesystem (never a screenshot)."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from . import note_read, read_effect

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".psd"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".xlsx", ".pptx", ".csv", ".html"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Installers": {".exe", ".msi", ".dmg", ".apk", ".deb"},
}


def _p(path: str) -> Path:
    return Path(path).expanduser()


def register_all(reg: ToolRegistry, settings) -> None:
    # ---------------------------------------------------------------- mkdir
    async def mkdir(args, ctx: ToolContext) -> ToolResult:
        p = _p(args["path"])
        try:
            await asyncio.to_thread(p.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(ok=False, note=f"mkdir failed: {exc}")
        return ToolResult(ok=True, outputs={"path": str(p)}, mutated=True)

    reg.register(ToolContract(
        name="files.mkdir", category="files", description="Create a directory (parents included)",
        params={"path": ParamSpec()},
        effects_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                         args={"path": a["path"]}),
                               Predicate(path="files.is_dir", op="==", value=True,
                                         args={"path": a["path"]})],
        verification=VerificationSpec(method="fs_exists", timeout_s=5.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.LOW), mkdir)

    # ---------------------------------------------------------------- write
    async def write(args, ctx: ToolContext) -> ToolResult:
        p = _p(args["path"])
        content = args["content"]
        data = content.encode("utf-8")
        try:
            await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(p.write_text, content, "utf-8")
        except OSError as exc:
            return ToolResult(ok=False, note=f"write failed: {exc}")

        def _size() -> int:
            try:
                return p.stat().st_size
            except OSError:
                return -1

        size = await asyncio.to_thread(_size)
        if size != len(data):
            return ToolResult(ok=False, note=f"size mismatch after write: {size} != {len(data)}")
        return ToolResult(ok=True, outputs={"path": str(p), "bytes": size}, mutated=True)

    reg.register(ToolContract(
        name="files.write", category="files", description="Write a UTF-8 text file",
        params={"path": ParamSpec(), "content": ParamSpec(),
                "overwrite": ParamSpec(type="bool", required=False)},
        preconditions_for=lambda a: [] if a.get("overwrite") else [
            Predicate(path="files.exists", op="==", value=False, args={"path": a["path"]})],
        effects_for=lambda a: [
            Predicate(path="files.exists", op="==", value=True, args={"path": a["path"]}),
            Predicate(path="files.size", op="==", value=len(a["content"].encode("utf-8")),
                      args={"path": a["path"]})],
        verification=VerificationSpec(method="fs_stat", timeout_s=5.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE,
        risk=RiskLevel.LOW,
        risk_context=lambda a: RiskLevel.MEDIUM if a.get("overwrite") else RiskLevel.LOW,
    ), write)

    # ---------------------------------------------------------------- read
    async def read(args, ctx: ToolContext) -> ToolResult:
        p = _p(args["path"])
        if not p.exists():
            return ToolResult(ok=False, note=f"{p} does not exist")
        try:
            text = await asyncio.to_thread(p.read_text, "utf-8", "replace")
        except OSError as exc:
            return ToolResult(ok=False, note=f"read failed: {exc}")
        note_read(ctx, "files.read")
        return ToolResult(ok=True, outputs={"path": str(p), "content": text[:20000],
                                            "size": len(text)},
                          observed=[ctx.perception.note("reads.files.read", True, "L1")])

    reg.register(ToolContract(
        name="files.read", category="files", description="Read a text file (capped)",
        params={"path": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                               args={"path": a["path"]})],
        effects=read_effect("files.read"),
        verification=VerificationSpec(method="fs_stat", timeout_s=4.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), read)

    # ---------------------------------------------------------------- copy/move
    async def move(args, ctx: ToolContext) -> ToolResult:
        src, dst = _p(args["src"]), _p(args["dst"])
        try:
            await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            return ToolResult(ok=False, note=f"move failed: {exc}")
        return ToolResult(ok=True, outputs={"src": str(src), "dst": str(dst)}, mutated=True)

    reg.register(ToolContract(
        name="files.move", category="files", description="Move a file/folder",
        params={"src": ParamSpec(), "dst": ParamSpec()},
        preconditions_for=lambda a: [
            Predicate(path="files.exists", op="==", value=True, args={"path": a["src"]}),
            Predicate(path="files.exists", op="==", value=False, args={"path": a["dst"]})],
        effects_for=lambda a: [
            Predicate(path="files.exists", op="==", value=False, args={"path": a["src"]}),
            Predicate(path="files.exists", op="==", value=True, args={"path": a["dst"]})],
        verification=VerificationSpec(method="fs_stat", timeout_s=6.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.MEDIUM), move)

    async def copy(args, ctx: ToolContext) -> ToolResult:
        src, dst = _p(args["src"]), _p(args["dst"])
        try:
            await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
            if src.is_dir():
                await asyncio.to_thread(shutil.copytree, str(src), str(dst))
            else:
                await asyncio.to_thread(shutil.copy2, str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            return ToolResult(ok=False, note=f"copy failed: {exc}")
        return ToolResult(ok=True, outputs={"src": str(src), "dst": str(dst)}, mutated=True)

    reg.register(ToolContract(
        name="files.copy", category="files", description="Copy a file/folder",
        params={"src": ParamSpec(), "dst": ParamSpec()},
        preconditions_for=lambda a: [
            Predicate(path="files.exists", op="==", value=True, args={"path": a["src"]}),
            Predicate(path="files.exists", op="==", value=False, args={"path": a["dst"]})],
        effects_for=lambda a: [
            Predicate(path="files.exists", op="==", value=True, args={"path": a["dst"]})],
        verification=VerificationSpec(method="fs_stat", timeout_s=6.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.LOW), copy)

    # ---------------------------------------------------------------- delete
    async def delete(args, ctx: ToolContext) -> ToolResult:
        p = _p(args["path"])
        try:
            if p.is_dir():
                await asyncio.to_thread(shutil.rmtree, str(p))
            else:
                await asyncio.to_thread(p.unlink)
        except OSError as exc:
            return ToolResult(ok=False, note=f"delete failed: {exc}")
        return ToolResult(ok=True, outputs={"path": str(p)}, mutated=True)

    reg.register(ToolContract(
        name="files.delete", category="files", description="Permanently delete a path",
        params={"path": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                               args={"path": a["path"]})],
        effects_for=lambda a: [Predicate(path="files.exists", op="==", value=False,
                                         args={"path": a["path"]})],
        verification=VerificationSpec(method="fs_not_exists", timeout_s=6.0),
        idempotency=IdempotencyClass.DESTRUCTIVE, risk=RiskLevel.HIGH), delete)

    # ---------------------------------------------------------------- search
    async def search(args, ctx: ToolContext) -> ToolResult:
        root, pattern = _p(args["root"]), args["pattern"]
        if not root.exists():
            return ToolResult(ok=False, note=f"{root} does not exist")

        def _search() -> list[str]:
            try:
                return [str(p) for p in list(root.rglob(pattern))[:200]]
            except OSError:
                return []

        hits = await asyncio.to_thread(_search)
        note_read(ctx, "files.search")
        return ToolResult(ok=True, outputs={"matches": hits, "count": len(hits)},
                          observed=[ctx.perception.note("reads.files.search", True, "L1")])

    reg.register(ToolContract(
        name="files.search", category="files", description="Search a folder tree by glob",
        params={"root": ParamSpec(), "pattern": ParamSpec()},
        effects=read_effect("files.search"),
        verification=VerificationSpec(method="fs_glob", timeout_s=15.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        parallel_safe=True), search)

    # ---------------------------------------------------------------- organize
    async def organize(args, ctx: ToolContext) -> ToolResult:
        root = _p(args["path"])
        dry_run = bool(args.get("dry_run", False))
        if not root.is_dir():
            return ToolResult(ok=False, note=f"{root} is not a directory")

        def _plan() -> list[dict]:
            moves = []
            try:
                for f in root.iterdir():
                    if not f.is_file():
                        continue
                    cat = "Other"
                    for name, exts in CATEGORIES.items():
                        if f.suffix.lower() in exts:
                            cat = name
                            break
                    target = root / cat / f.name
                    if target != f:
                        moves.append({"src": str(f), "dst": str(target), "category": cat})
            except OSError:
                pass
            return moves

        moves = await asyncio.to_thread(_plan)
        if dry_run:
            note_read(ctx, "files.organize")
            return ToolResult(ok=True, outputs={"dry_run": True, "plan": moves,
                                                "count": len(moves)},
                              observed=[ctx.perception.note("reads.files.organize", True, "L1")])
        done, failed = 0, 0
        for m in moves:
            try:
                dst = Path(m["dst"])
                await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.move, m["src"], str(dst))
                done += 1
            except (OSError, shutil.Error):
                failed += 1
        ctx.perception.note("reads.files.organize", True, "L1")
        return ToolResult(ok=True, outputs={"moved": done, "failed": failed, "plan": moves},
                          observed=[ctx.perception.note("reads.files.organize", True, "L1")],
                          mutated=done > 0)

    reg.register(ToolContract(
        name="files.organize", category="files",
        description="Organize files into category folders (dry_run preview supported)",
        params={"path": ParamSpec(), "dry_run": ParamSpec(type="bool", required=False)},
        effects=read_effect("files.organize"),
        verification=VerificationSpec(method="fs_organize_journal", timeout_s=30.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.MEDIUM,
    ), organize)
