"""L5/L6/L7 — OCR, screenshots (evidence only) and the vision gateway.

INV-1/INV-3/INV-4 (doc 04 §2, doc 13): these levels are escalation, never the
default. Every L5+ request must pass the PolicyGate with `reason` +
`exhausted_levels`. Images never enter an LLM prompt unless the vision gateway
was explicitly authorized (Tier 3 routing).
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

from ..core.state import Observation, Truth
from .policy import PolicyGate, PolicyViolation


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def evidence_dir(settings) -> Path:
    d = settings.data_dir / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def capture_screen(settings, region: tuple[int, int, int, int] | None = None,
                         label: str = "screen") -> Path | None:
    """L6: screenshot as an EVIDENCE artifact (never context, INV-4)."""
    def _grab() -> Path | None:
        try:
            from PIL import ImageGrab  # pillow (already installed)
        except ImportError:
            return None
        try:
            img = ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
            out = evidence_dir(settings) / f"{label}_{int(time.time())}.png"
            img.save(out, "PNG")
            return out
        except Exception:
            return None

    return await asyncio.to_thread(_grab)


async def ocr_image(png_path: Path, timeout_s: float = 20.0) -> str | None:
    """L5: OCR via tesseract when present; None = honest unavailability."""
    if not ocr_available():
        return None

    def _run() -> str | None:
        try:
            proc = subprocess.run(
                ["tesseract", str(png_path), "stdout", "-l", "eng"],
                capture_output=True, text=True, timeout=timeout_s)
            if proc.returncode == 0:
                return proc.stdout.strip()
        except Exception:
            return None
        return None

    return await asyncio.to_thread(_run)


class VisionGateway:
    """The ONLY module allowed to call a vision model (doc 13 rule 9)."""

    def __init__(self, settings, policy: PolicyGate, router=None):
        self.settings = settings
        self.policy = policy
        self.router = router
        self.calls = 0

    async def look(self, question: str, *, reason: str, exhausted_levels: list[str],
                   task=None, region: tuple[int, int, int, int] | None = None) -> Observation:
        self.policy.require_observation("L7", reason=reason,
                                        exhausted_levels=exhausted_levels, task=task)
        if not self.settings.allow_vision or not self.settings.vision_model:
            return Observation(path=question, truth=Truth.UNKNOWN, level="L7", value=None,
                               note="vision requested but not configured (EVA_ALLOW_VISION/VISION_MODEL)")
        png = await capture_screen(self.settings, region=region, label="l7")
        if png is None:
            return Observation(path=question, truth=Truth.UNKNOWN, level="L7", value=None,
                               note="screenshot capture unavailable")
        if self.router is None:
            return Observation(path=question, truth=Truth.UNKNOWN, level="L7", value=None,
                               note=f"vision gateway: no Tier-3 router wired (image kept as evidence {png.name})")
        try:
            answer = await self.router.call_vision(question, str(png))
        except Exception as exc:
            return Observation(path=question, truth=Truth.UNKNOWN, level="L7", value=None,
                               note=f"vision call failed: {exc}")
        self.calls += 1
        return Observation(path=question, truth=Truth.TRUE if answer else Truth.UNKNOWN,
                           value=answer, level="L7", raw={"evidence": png.name},
                           note=f"vision({self.settings.vision_model})")
