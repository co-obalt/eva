"""Real tool registry contract tests + risk engine + terminal sandbox."""
from __future__ import annotations

import asyncio
from pathlib import Path

from eva.config import get_settings
from eva.core.contracts import ToolContext
from eva.core.risk import RiskGate
from eva.core.task import RiskLevel
from eva.tools import build_registry
from eva.tools import terminal_tools
from tests.fakes import FakePerception, FakeWorld

S = get_settings()


def _ctx():
    return ToolContext(perception=FakePerception(FakeWorld()))


def test_every_real_tool_carries_a_full_contract():
    reg = build_registry(S)
    names = reg.names()
    assert len(names) >= 25
    for t in reg.all():
        c = t.contract
        assert (c.effects or c.effects_for), f"{c.name}: no effects"
        assert c.verification.method, f"{c.name}: no verification method"
        assert c.verification.timeout_s > 0
        assert c.idempotency is not None and c.risk is not None
        low = c.verification.method.lower()
        assert not any(bad in low for bad in ("l5", "l6", "l7", "screenshot", "vision", "ocr"))


def test_real_registry_rejects_unknown_args():
    reg = build_registry(S)
    try:
        reg.validate_args("files.mkdir", {"path": "C:/x", "hax": 1})
        raised = False
    except Exception:
        raised = True
    assert raised


def test_delete_is_critical_when_big_or_outside_roots(tmp_path):
    big = tmp_path / "logs"
    big.mkdir()
    (big / "dump.bin").write_bytes(b"0" * 2_000_000)
    gate = RiskGate(allowed_roots=(tmp_path,), big_bytes=1_000_000)
    reg = build_registry(S)
    contract = reg.require("files.delete").contract
    d = gate.classify(contract, {"path": str(big)})
    assert d.level is RiskLevel.CRITICAL and d.requires_confirmation
    assert "1.9 MB" in d.consequences or "MB" in d.consequences

    small = tmp_path / "small.txt"
    small.write_text("hi")
    d2 = gate.classify(contract, {"path": str(small)})
    assert d2.level is RiskLevel.HIGH

    outside = Path("C:/Windows/Temp") / "eva_test_dir"
    d3 = gate.classify(contract, {"path": str(outside)})
    assert d3.level is RiskLevel.CRITICAL  # outside allowed roots


def test_message_send_always_high_risk():
    gate = RiskGate()
    reg = build_registry(S)
    contract = reg.require("wa.send_message").contract
    d = gate.classify(contract, {"recipient": "Saad Bhai", "text": "phir kayaa?"})
    assert d.level is RiskLevel.HIGH
    assert "Saad Bhai" in d.consequences


def test_terminal_rejects_metachars_and_unknown_commands(tmp_path):
    reg = build_registry(S)
    handler = reg.require("terminal.run").handler

    bad = asyncio.run(handler({"command": "git status && del important"}, _ctx()))
    assert not bad.ok and bad.blocked and "metachar" in bad.note

    unknown = asyncio.run(handler({"command": "del important.txt"}, _ctx()))
    assert not unknown.ok and unknown.blocked                        # filesystem ops are gated elsewhere

    ok = asyncio.run(handler({"command": "python --version", "cwd": str(tmp_path)}, _ctx()))
    assert ok.ok and "3.12" in ok.outputs["output"]
