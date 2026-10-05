"""M1 — the Actor itself: one valid action per turn, and a menu a small model can read."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eva.config import Settings
from eva.core import Condition, Predicate, Task
from eva.models.actor import Actor, ActorError
from eva.tools.agent_tools import register_all as register_agent_tools
from tests.fakes import FakeWorld, build_fake_registry


class ScriptedRouter:
    """Returns queued model replies; records what it was asked."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def call(self, tier, purpose, *, messages=None, json_mode=False, max_tokens=800, **kw):
        self.calls.append({"tier": int(tier), "purpose": purpose, "messages": messages or [],
                           "json_mode": json_mode, "max_tokens": max_tokens})
        if not self.replies:
            raise AssertionError("ScriptedRouter ran out of replies")
        return SimpleNamespace(text=self.replies.pop(0), provider="fake", model="scripted",
                               usage={"in": 10, "out": 5}, cached=False, latency_s=0.0)


def _registry(tmp_path, memory=None):
    reg = build_fake_registry(FakeWorld())
    register_agent_tools(reg, Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path),
                         memory)
    return reg


def _task(request: str = "open whatsapp and search saad bhai") -> Task:
    return Task(user_request=request, goal=request,
                completion=Condition(all_of=[Predicate(path="wa.view", op="==",
                                                       value="archived")]))


def test_actor_parses_tool_ask_and_finish_shapes(tmp_path):
    router = ScriptedRouter([
        '{"kind":"tool","tool":"files.mkdir","args":{"path":"C:/tmp/x"},"reason":"make it"}',
        "```json\n{\"kind\":\"ask\",\"question\":\"which one?\",\"options\":[\"a\",\"b\"],"
        "\"reason\":\"ambiguous\"}\n```",
        '{"kind":"finish","reason":"the effect is already true"}',
    ])
    actor = Actor(router, _registry(tmp_path))

    tool = asyncio.run(actor.next_action(_task()))
    ask_ = asyncio.run(actor.next_action(_task()))
    fin = asyncio.run(actor.next_action(_task()))

    assert (tool.kind, tool.tool, tool.args["path"], tool.reason) == \
        ("tool", "files.mkdir", "C:/tmp/x", "make it")
    assert ask_.kind == "ask" and ask_.question == "which one?" and ask_.options == ["a", "b"]
    assert fin.kind == "finish"
    # every call went out as a Tier-2 JSON request
    assert all(c["tier"] == 2 and c["json_mode"] for c in router.calls)
    assert all(c["purpose"] == "actor" for c in router.calls)


def test_actor_rejects_unknown_tool_then_recovers(tmp_path):
    router = ScriptedRouter([
        '{"kind":"tool","tool":"wa.teleport","args":{},"reason":"made that up"}',
        '{"kind":"tool","tool":"wa.search_contact","args":{"q":"Saad Bhai"},'
        '"reason":"search the chat"}',
    ])
    actor = Actor(router, _registry(tmp_path))

    proposal = asyncio.run(actor.next_action(_task()))

    assert proposal.tool == "wa.search_contact"
    assert proposal.args == {"q": "Saad Bhai"}
    assert len(router.calls) == 2
    feedback = router.calls[1]["messages"][-1]["content"]
    assert "wa.teleport" in feedback and "not registered" in feedback


def test_actor_rejects_bad_params_and_gives_up_after_two_tries(tmp_path):
    router = ScriptedRouter([
        '{"kind":"tool","tool":"files.mkdir","args":{"nope":"x"},"reason":"wrong params"}',
        '{"kind":"tool","tool":"files.mkdir","args":{"nope":"x"},"reason":"still wrong"}',
    ])
    actor = Actor(router, _registry(tmp_path))

    with pytest.raises(ActorError):
        asyncio.run(actor.next_action(_task()))
    assert len(router.calls) == 2


def test_actor_rejects_an_ask_without_a_question(tmp_path):
    router = ScriptedRouter(['{"kind":"ask","options":["a"]}',
                             '{"kind":"ask","question":"ok?"}'])
    actor = Actor(router, _registry(tmp_path))

    proposal = asyncio.run(actor.next_action(_task()))

    assert proposal.kind == "ask" and proposal.question == "ok?"
    assert "question" in router.calls[1]["messages"][-1]["content"]


def test_actor_menu_is_relevance_filtered_and_keeps_the_interaction_tools(tmp_path):
    actor = Actor(ScriptedRouter([]), _registry(tmp_path), menu_size=3)
    names = actor.menu(_task("open whatsapp and search saad bhai"))

    assert "agent.ask_user" in names and "memory.remember" in names   # always available
    assert any(n.startswith("wa.") for n in names)                    # the relevant family wins
    assert "files.mkdir" not in names                                 # unrelated noise is dropped
    # deterministic ordering: same input -> same menu
    assert actor.menu(_task("open whatsapp and search saad bhai")) == names


def test_actor_prompt_carries_state_history_rules_and_budget(tmp_path):
    router = ScriptedRouter(['{"kind":"finish","reason":"done"}'])
    actor = Actor(router, _registry(tmp_path))
    state = [{"path": "wa.view", "value": "chatlist", "truth": "TRUE", "level": "L4",
              "age_s": 1}]
    history = [{"step": 1, "tool": "chrome.open_profile",
                "action": "chrome.open_profile({})", "status": "VERIFIED",
                "observed": "chrome.running = True", "reason": "open it"}]
    memories = {"rules": [{"id": "rule_1", "instruction": "use tool A for websites",
                           "when": "websites", "prefer": ["browser.find"],
                           "forbid": ["browser.type"]}]}

    asyncio.run(actor.next_action(_task(), state=state, history=history, memories=memories,
                                  budget={"actions_left": 7, "tokens_left": 500}))

    prompt = router.calls[0]["messages"][-1]["content"]
    assert "wa.view = chatlist" in prompt
    assert "chrome.open_profile" in prompt and "VERIFIED" in prompt
    assert "use tool A for websites" in prompt
    assert "NEVER use browser.type" in prompt
    assert "never verify your own work" in router.calls[0]["messages"][0]["content"]
    assert "actions_left=7" in prompt
    # the menu is rendered with the exact contract signatures the model must copy
    assert "TOOL MENU" in prompt and "- agent.ask_user(" in prompt
    assert "[risk:low]" in prompt
