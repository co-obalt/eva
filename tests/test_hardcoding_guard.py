"""Red-line guard (machine-checked): verbs vs knowledge.

The general layer (actor, generic web verbs, L3 DOM, interaction tools, the
orchestrator's actor mode) must know only HTML/OS *concepts*. Any app, site,
workflow or CLI name is knowledge, and knowledge lives in ``data/*.json`` tables
or in learned memory (rules/skills/facts) — never in code.

The pre-M1 adapters (eva/apps/registry.py, eva/perception/l4_dom.py,
eva/tools/browser_tools.py, eva/tools/app_tools.py, eva/tasks/recipes.py) are the
bounded hands this milestone replaces: they may carry concrete names today, and
they are listed here so the exemption is explicit rather than accidental.
"""
from __future__ import annotations

from pathlib import Path

from eva.config import Settings
from eva.core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolResult,
                                VerificationSpec)
from eva.core.state import Predicate
from eva.core.task import RiskLevel, Task
from eva.memory.store import MemoryStore
from eva.models.actor import Actor
from eva.storage import Database
from eva.tools.agent_tools import register_all as register_agent_tools
from tests.fakes import FakeWorld, ScriptedActor, build_fake_registry

ROOT = Path(__file__).resolve().parents[1]

# The M1 general layer: these must stay brand-free forever.
GENERAL_LAYER = (
    "eva/models/actor.py",
    "eva/perception/l3_dom.py",
    "eva/tools/web_tools.py",
    "eva/tools/agent_tools.py",
    "eva/core/orchestrator.py",
    "eva/models/context.py",
)

# Concrete app/site/CLI names that must never appear in the general layer.
BANNED_NAMES = (
    "whatsapp", "antigravity", "freebuff", "gemini", "sheets.google", "gmail",
    "youtube", "notion", "slack", "spotify", "excel", "powerpoint", "vercel",
    "github", "instagram", "facebook", "telegram", "discord",
)

# Legacy adapters (pre-M1 bounded hands) — the only runtime modules allowed to
# carry concrete knowledge; each is to be replaced by data/memory, not extended.
LEGACY_ADAPTERS = {
    "eva/apps/registry.py",
    "eva/perception/engine.py",
    "eva/perception/l4_dom.py",
    "eva/tasks/recipes.py",
    "eva/tools/app_tools.py",
    "eva/tools/browser_tools.py",
}


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace").casefold()


def test_general_layer_contains_no_app_site_or_cli_names():
    for rel in GENERAL_LAYER:
        hits = [name for name in BANNED_NAMES if name in _text(rel)]
        assert not hits, f"{rel} hardcodes knowledge: {hits}"


def test_generic_web_verbs_do_not_import_site_adapters():
    for rel in ("eva/tools/web_tools.py", "eva/perception/l3_dom.py",
                "eva/models/actor.py"):
        src = _text(rel)
        for forbidden in ("l4_dom", "wa_", "apps.registry", "recipes"):
            assert forbidden not in src, f"{rel} must not depend on {forbidden!r}"


def test_concrete_knowledge_is_confined_to_the_adapter_list():
    offenders = []
    for path in (ROOT / "eva").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in LEGACY_ADAPTERS:
            continue
        hits = [name for name in BANNED_NAMES
                if name in path.read_text(encoding="utf-8", errors="replace").casefold()]
        if hits:
            offenders.append((rel, hits))
    assert not offenders, f"concrete app/site knowledge leaked outside the adapters: {offenders}"


def test_actor_menu_is_registry_driven_not_a_frozen_list(tmp_path):
    """New reach comes from the registry (data/tools), never from actor code."""
    world = FakeWorld()
    reg = build_fake_registry(world)
    register_agent_tools(reg, Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path),
                         MemoryStore(Database(tmp_path / "g.db")))
    actor = Actor(ScriptedActor([]), reg)

    async def noop(args, ctx):
        return ToolResult(ok=True)

    task = Task(user_request="open the acme portal", goal="open the acme portal")
    before = actor.menu(task)
    assert set(before) <= set(reg.names())

    # a brand-new capability registered at runtime can enter the menu: the actor
    # code did not change, the *knowledge* did
    reg.register(ToolContract(
        name="acme.portal_open", category="browser",
        description="Open the Acme portal workspace",
        params={"workspace": ParamSpec(required=False)},
        effects=[Predicate(path="reads.acme.portal_open", op="==", value=True)],
        verification=VerificationSpec(method="L3_url", timeout_s=5.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW),
        noop)
    assert "acme.portal_open" in actor.menu(task)
    assert "acme.portal_open" not in before


def test_interaction_primitives_are_generic(tmp_path):
    """ask_user/remember are interfaces, not policies: they carry no site rules."""
    db = Database(tmp_path / "m.db")
    memory = MemoryStore(db)
    reg = build_fake_registry(FakeWorld())
    register_agent_tools(reg, Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path),
                         memory)
    assert memory.list_rules() == []                       # nothing taught yet
    ask = reg.require("agent.ask_user").contract
    assert set(ask.params) == {"question", "options"}      # one question, optional choices
    assert reg.require("memory.remember").contract.params["instruction"].required is True
    # neither tool's interface mentions any concrete app/site: they are generic
    for name in ("agent.ask_user", "memory.remember"):
        contract = reg.require(name).contract
        blob = (contract.description + " "
                + " ".join(f"{k} {v.doc or ''}" for k, v in contract.params.items())).casefold()
        assert not [b for b in BANNED_NAMES if b in blob], name
    db.close()
