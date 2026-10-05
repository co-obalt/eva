"""S7 — model router & context economy (doc 11 §S7, doc 07)."""
from __future__ import annotations

import asyncio

import pytest

from eva.core.contracts import IdempotencyClass, ParamSpec, ToolContract, ToolRegistry, VerificationSpec
from eva.core.state import Condition, Predicate
from eva.core.task import RiskLevel, Task
from eva.models.context import ContextBuilder, estimate_tokens
from eva.models.providers import ChatResult, ProviderUnavailable
from eva.models.router import ModelRouter, NoModelNeeded, Tier
from eva.storage import Database


class FakePool:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def available(self) -> bool:
        return not self.fail

    async def chat(self, messages, **kw) -> ChatResult:
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable("all providers down (simulated)")
        return ChatResult(text="{\"ok\": true}", provider="Fake", model="fake-1",
                          usage={"in": 100, "out": 20}, latency_s=0.01)

    def status(self):
        return [{"name": "Fake", "alive": True, "usable": True}]


def _task():
    return Task(goal="g", completion=Condition(all_of=[Predicate(path="a.b", op="==", value=1)]))


def test_tier0_never_calls_a_model():
    router = ModelRouter(FakePool())
    with pytest.raises(NoModelNeeded):
        asyncio.run(router.call(Tier.T0, "anything", user="hi"))


def test_s7d_same_question_twice_is_one_call(tmp_path):
    db = Database(tmp_path / "c.db")
    from eva.core.checkpoint import CheckpointStore
    store = CheckpointStore(db)
    pool = FakePool()
    router = ModelRouter(pool, store=store)
    task = _task()
    router.bind_task(task)

    async def run():
        r1 = await router.call(2, "plan", user="hello world")
        r2 = await router.call(2, "plan", user="hello world")
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert pool.calls == 1                     # ledger memo — never re-asks
    assert r2.cached is True
    assert task.counters.model_calls == 1
    assert task.counters.cache_hits == 1


def test_s7c_provider_outage_raises_honestly():
    router = ModelRouter(FakePool(fail=True))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(router.call(2, "plan", user="x"))
    assert router.failures == 1


def test_s7b_context_builder_enforces_budget():
    cb = ContextBuilder(budget_tokens=4000)
    task = Task(goal="write the proposal", user_request="write the proposal",
                completion=Condition(all_of=[Predicate(path="files.exists", op="==", value=True,
                                                       args={"path": "x"})]))
    huge_history = ["A" * 2000 for _ in range(50)]
    big_memory = {"facts": [{"s": "x" * 300, "r": "y" * 300, "o": "z" * 300}] * 20}
    tools = "\n".join([f"- tool_{i}(a, b) — " + "d" * 400 for i in range(200)])
    messages = cb.build(task=task, state=[{"path": "p", "value": i, "level": "L1",
                                           "age_s": 1, "truth": "TRUE"} for i in range(60)],
                        memory_slice=big_memory, tools=tools, history=huge_history)
    total = estimate_tokens("".join(m["content"] for m in messages))
    assert total <= 4000
    assert "A" * 2000 not in messages[-1]["content"]     # history dropped first
    assert "d" * 200 not in messages[-1]["content"]      # verbose tool docs dropped next


def test_s7a_recipe_path_needs_no_provider_at_all(tmp_path):
    """'cpu usage' style commands must short-circuit before any model call."""
    from eva.memory.store import MemoryStore
    from eva.tasks.recipes import match_recipe
    from eva.config import get_settings
    task = match_recipe("kya cpu usage hai?", get_settings())
    assert task is not None and task.planned_by == "recipe"
    assert task.counters.model_calls == 0


def test_offline_novel_task_is_honest(tmp_path):
    """No provider: novel request must NOT be silently executed or faked (S8d)."""
    from eva.config import get_settings
    from eva.memory.store import MemoryStore
    from eva.tasks.manager import TaskManager

    class DeadComposer:
        async def compose_intent(self, text):
            raise ProviderUnavailable("no providers available")

    db = Database(tmp_path / "m.db")
    memory = MemoryStore(db)
    tm = TaskManager(get_settings(), memory, composer=DeadComposer())
    task, reply = asyncio.run(tm.handle("design a full website for my bakery"))
    assert task is None
    assert "Nothing was executed" in reply
