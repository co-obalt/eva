"""M1/M3 seed — durable user rules: stored in SQLite, observable, enforceable.

The rule store is *memory, not code*: these tests drive it through the real
MemoryStore, the real tool registry (memory.remember) and the real perception
engine (rules.* / ui.user_answer providers).
"""
from __future__ import annotations

import asyncio

from eva.config import Settings
from eva.core.contracts import ToolContext
from eva.core.state import ObservationRequest, Truth
from eva.core.task import Task
from eva.memory.store import MemoryStore, Rule
from eva.perception.engine import PerceptionEngine
from eva.storage import Database
from eva.tools import build_registry


def _settings(tmp_path) -> Settings:
    return Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path)


def _task(request: str = "build the website for the restaurant") -> Task:
    return Task(user_request=request, goal=request)


# ------------------------------------------------------------------- the store
def test_rule_store_upsert_list_forget(tmp_path):
    db = Database(tmp_path / "r.db")
    mem = MemoryStore(db)
    rid = mem.upsert_rule(Rule(instruction="use the design tool for website work",
                               when_text="website", prefer_tools=["browser.read"]))
    assert mem.has_rule("use the design tool for website work")
    assert [r.id for r in mem.list_rules()] == [rid]
    # same scope + instruction updates in place instead of duplicating
    again = mem.upsert_rule(Rule(instruction="use the design tool for website work",
                                 when_text="website", forbid_tools=["browser.click"]))
    assert again == rid
    assert len(mem.list_rules()) == 1
    assert mem.get_rule(rid).forbid_tools == ["browser.click"]
    assert mem.forget_rule(rid) is True
    assert mem.list_rules() == []
    assert mem.list_rules(active_only=False)[0].active is False
    db.close()


def test_rule_relevance_and_scope(tmp_path):
    db = Database(tmp_path / "r.db")
    mem = MemoryStore(db)
    mem.upsert_rule(Rule(instruction="always use the visual tool for websites",
                         when_text="website"))
    mem.upsert_rule(Rule(instruction="never touch production data", when_text="deploy"))
    mem.upsert_rule(Rule(instruction="reply in roman urdu"))
    mem.upsert_rule(Rule(instruction="use the site tool for website work",
                         when_text="website", scope="site:example.com"))
    relevant = mem.applicable_rules("make me a website for the cafe")
    # trigger words decide: only the website rules speak about this request
    assert {r.when_text for r in relevant} == {"website"}
    assert mem.applicable_rules("deploy the app")[0].when_text == "deploy"
    # a rule about something else never leaks into this task
    assert mem.applicable_rules("water the plants") == []
    # site-scoped rules only apply on their own site; global ones always apply
    assert {r.scope for r in mem.applicable_rules("make me a website")} == \
        {"global", "site:example.com"}
    assert {r.scope for r in mem.applicable_rules("make me a website",
                                                  scope="site:github.com")} == {"global"}
    db.close()


def test_retrieve_slice_includes_applicable_rules(tmp_path):
    db = Database(tmp_path / "r.db")
    mem = MemoryStore(db)
    mem.upsert_rule(Rule(instruction="use the website tool for website work",
                         when_text="website"))
    slice_ = mem.retrieve(_task("build a website"))
    assert slice_["rules"] and slice_["rules"][0]["when"] == "website"
    assert slice_["rules"][0]["instruction"].startswith("use the website tool")
    db.close()


# ------------------------------------------------------- the memory.remember tool
def test_memory_remember_writes_an_enforceable_rule(tmp_path):
    settings = _settings(tmp_path)
    mem = MemoryStore(Database(tmp_path / "m.db"))
    engine = PerceptionEngine(settings, memory=mem)
    reg = build_registry(settings, perception=engine, scheduler=None, memory=mem)
    contract = reg.require("memory.remember").contract
    assert contract.resolve_effects({"instruction": "x"})[0].path == "rules.exists"
    ctx = ToolContext(perception=engine, settings=settings, task=_task("build a website"))
    handler = reg.require("memory.remember").handler
    args = {"instruction": "use the free tool for website work", "when": "website",
            "prefer_tools": "browser.read|not.a.tool", "forbid_tools": "browser.click"}
    result = asyncio.run(handler(args, ctx))
    assert result.ok and result.mutated
    assert result.outputs["prefer_tools"] == ["browser.read"]      # unknown names dropped
    assert result.outputs["ignored"] == ["not.a.tool"]
    stored = mem.list_rules()[0]
    assert stored.instruction == "use the free tool for website work"
    assert stored.forbid_tools == ["browser.click"]
    # the contract's effect is checkable from the real observation provider
    obs = asyncio.run(engine.observe([ObservationRequest(
        path="rules.exists", args={"rule": "use the free tool for website work"})]))
    assert obs["rules.exists|" + '{"rule": "use the free tool for website work"}'].truth is Truth.TRUE
    assert asyncio.run(engine.observe([ObservationRequest(path="rules.count")]))[
        "rules.count"].value == 1


# ------------------------------------------------------ observation providers
def test_rule_providers_and_answered_questions(tmp_path):
    settings = _settings(tmp_path)
    mem = MemoryStore(Database(tmp_path / "m.db"))
    mem.upsert_rule(Rule(instruction="use the visual tool for website work",
                         when_text="website"))
    engine = PerceptionEngine(settings, memory=mem)
    rows = asyncio.run(engine.observe([
        ObservationRequest(path="rules.exists", args={"rule": "nope"}),
        ObservationRequest(path="rules.list", args={"query": "website"}),
        ObservationRequest(path="rules.count"),
    ]))
    assert rows['rules.exists|{"rule": "nope"}'].truth is Truth.TRUE
    assert rows['rules.exists|{"rule": "nope"}'].value is False
    assert rows['rules.list|{"query": "website"}'].value == \
        ["use the visual tool for website work"]
    assert rows["rules.count"].value == 1
    # with no memory store wired the honest answer is UNKNOWN, never a bare False
    bare = PerceptionEngine(settings, memory=None)
    obs = asyncio.run(bare.observe([ObservationRequest(path="rules.count")]))
    assert obs["rules.count"].truth is Truth.UNKNOWN

    # an answered question is TRUE for *that* question and UNKNOWN for any other
    task = _task("which profile?")
    task.context["answers"] = {"which profile?": "personal"}
    engine.bind_task(task)
    answered = asyncio.run(engine.observe([
        ObservationRequest(path="ui.user_answer", args={"question": "which profile?"}),
        ObservationRequest(path="ui.user_answer", args={"question": "a different question?"}),
    ]))
    assert answered['ui.user_answer|{"question": "which profile?"}'].truth is Truth.TRUE
    assert answered['ui.user_answer|{"question": "which profile?"}'].value == "personal"
    assert answered['ui.user_answer|{"question": "a different question?"}'].truth is Truth.UNKNOWN


def test_agent_ask_user_effects_are_per_question(tmp_path):
    settings = _settings(tmp_path)
    reg = build_registry(settings, perception=None, scheduler=None, memory=None)
    contract = reg.require("agent.ask_user").contract
    effects = contract.resolve_effects({"question": "which one?"})
    assert [p.path for p in effects] == ["ui.user_answer"]
    assert effects[0].op == "exists"
    assert effects[0].args == {"question": "which one?"}
    assert effects[0].key != contract.resolve_effects({"question": "a different one?"})[0].key
