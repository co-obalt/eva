"""M1 — asking the user, obeying remembered rules, and escalating the fast lane.

Everything runs through the real orchestrator: ask_user parks the task on the real
EvaApp surface, a remembered rule vetoes a tool before execution, and a frozen
recipe that failed hands over to the actor exactly once.
"""
from __future__ import annotations

import asyncio

from eva.bootstrap import EvaApp
from eva.config import Settings
from eva.core import Condition, Predicate, StepStatus, TaskStatus
from eva.core.task import Task
from eva.memory.store import MemoryStore, Rule
from eva.storage import Database
from eva.tasks.manager import TaskManager
from tests.fakes import FakeWorld, Harness, ScriptedActor, ask, finish, make_task, propose, step


def _needs(*paths: str) -> Condition:
    return Condition(all_of=[Predicate(path="files.exists", op="==", value=True,
                                       args={"path": p}) for p in paths])


# --------------------------------------------------------------- ask_user
def test_ask_user_parks_the_task_and_the_answer_resumes_it(tmp_path):
    target = str(tmp_path / "done")
    question = "Should I create the folder now?"
    actor = ScriptedActor([
        ask(question, options=["yes", "no"]),
        ask(question, options=["yes", "no"]),   # same question again: skipped (P3)
        propose("files.mkdir", {"path": target}, reason="user said yes"),
    ])
    world = FakeWorld()
    h = Harness(tmp_path, world=world, actor=actor)
    task = make_task("ask me first", [], _needs(target), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert question in res.message
    waiting = h.events_of("TASK_WAITING_USER")
    assert len(waiting) == 1                       # asked exactly once
    assert "yes" in waiting[-1].payload["ask"]
    assert "no" in waiting[-1].payload["ask"]
    assert world.files == {}                       # nothing was done while waiting

    # the user answers — exactly what EvaApp.answer_pending records
    task.context["answers"] = {question: "yes"}
    h.perception.notes["ui.user_answer"] = "yes"
    h.store.save_task(task)

    res2 = h.resume(task.id)

    assert res2.status is TaskStatus.FINISHED
    assert world.files[target] == "<dir>"
    # resume works on the stored task: re-read it to inspect the new steps
    saved = h.store.load_task(task.id)
    # ask #1, the *re-proposed identical question* (skipped), then the real action
    assert [s.tool for s in saved.plan] == ["agent.ask_user", "agent.ask_user", "files.mkdir"]
    assert saved.plan[1].status is StepStatus.SKIPPED_ALREADY_DONE   # never re-asked
    assert len(h.events_of("TASK_WAITING_USER")) == 1
    # the actor saw the user's answer before deciding the next action
    seen = actor.calls[-1]["state"]
    assert any("ui.user_answer" in str(row) and "yes" in str(row) for row in seen)


def test_app_answer_pending_round_trip(tmp_path):
    """The real EvaApp surface: park on a question, answer it, park on the next."""
    settings = Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path)
    app = EvaApp(settings=settings, voice=False, avatar=False, speak=False)
    actor = ScriptedActor([
        ask("which profile?", options=["personal", "work"]),
        ask("ok — which window?", options=["main", "second"]),
    ])
    app.orchestrator.actor = actor
    # the test is about the ask/answer round trip, not about completion patience
    app.orchestrator.completion_timeout_s = 0.5
    missing = str(tmp_path / "never-there")
    task = Task(user_request="open my files", goal="open my files",
                completion=Condition(all_of=[Predicate(path="files.exists", op="==",
                                                       value=True, args={"path": missing})]),
                mode="actor", planned_by="actor", context={"answers": {}})
    try:
        first = asyncio.run(app.orchestrator.run(task))
        app.pending_ask = app._pending_after(first)
        assert app.pending_ask is not None
        assert app.pending_ask["question"] == "which profile?"
        assert app.pending_ask["options"] == ["personal", "work"]

        second, reply = asyncio.run(app.answer_pending("personal"))
        assert reply is None
        assert second is not None
        # the answer is durable evidence on the task...
        saved = app.store.load_task(task.id)
        assert saved.context["answers"]["which profile?"] == "personal"
        # ...and a real observation any provider can see
        assert any(row["path"] == "ui.user_answer" and row["value"] == "personal"
                   for row in app.perception.explain())
        # ...and the actor's next decision was shown "user answered: personal"
        assert any("personal" in str(h.get("observed"))
                   for h in actor.calls[-1]["history"])
        # the loop moved on to the next genuine question
        assert app.pending_ask is not None
        assert app.pending_ask["question"] == "ok — which window?"
    finally:
        asyncio.run(app.close())


# --------------------------------------------------------------- rules
def test_rule_remembered_by_the_tool_is_enforced_on_a_later_task(tmp_path):
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    # task 1: the user says "from now on, never delete while building reports"
    h1 = Harness(tmp_path, world=FakeWorld(), memory=memory, actor=ScriptedActor([
        propose("memory.remember", {"instruction": "never use delete while building reports",
                                    "when": "report", "forbid_tools": "files.delete"},
                reason="the user asked me to remember this"),
        propose("files.mkdir", {"path": str(tmp_path / "r1")}, reason="build the report"),
    ]))
    task1 = make_task("build the report", [], _needs(str(tmp_path / "r1")), mode="actor")

    res1 = h1.run(task1)

    assert res1.status is TaskStatus.FINISHED
    assert memory.has_rule("never use delete while building reports")
    assert task1.counters.rule_rejections == 0        # nothing to veto yet

    # task 2 — a later run on the same memory: the rule is enforced, not advice
    h2 = Harness(tmp_path, world=FakeWorld(), memory=memory, actor=ScriptedActor([
        propose("files.delete", {"path": str(tmp_path / "r2")}, reason="clean up first"),
        propose("files.mkdir", {"path": str(tmp_path / "r2")}, reason="build the report"),
    ]))
    task2 = make_task("build the report again", [], _needs(str(tmp_path / "r2")), mode="actor")

    res2 = h2.run(task2)

    assert res2.status is TaskStatus.FINISHED
    assert task2.counters.rule_rejections == 1
    assert not any(s.tool == "files.delete" for s in task2.plan)
    db.close()


def test_remembered_rule_vetoes_a_forbidden_tool(tmp_path):
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    memory.upsert_rule(Rule(instruction="never delete while building the report",
                            when_text="report", forbid_tools=["files.delete"]))
    target = str(tmp_path / "report")
    actor = ScriptedActor([
        propose("files.delete", {"path": target}, reason="clean up first"),
        propose("files.mkdir", {"path": target}, reason="build the report"),
    ])
    h = Harness(tmp_path, world=FakeWorld(), actor=actor, memory=memory)
    task = make_task("build the report now", [], _needs(target), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert task.counters.rule_rejections == 1
    assert not any(s.tool == "files.delete" for s in task.plan)   # vetoed before execution
    enforced = h.events_of("RULE_ENFORCED")
    assert enforced and enforced[0].payload["tool"] == "files.delete"
    # the actor was told why, so it can choose differently
    history = actor.calls[-1]["history"]
    assert any("rule" in str(h_.get("observed", "")).lower() for h_ in history)
    # rules are advisory only when they say nothing about the request
    assert memory.applicable_rules("water the plants") == []
    db.close()


# --------------------------------------------------------------- escalation
def test_failed_fast_lane_plan_escalates_to_the_actor(tmp_path):
    target = str(tmp_path / "salvaged")
    world = FakeWorld()
    world.stall_navigation = True          # the frozen recipe step cannot verify
    frozen = [step(1, "chrome.open_profile",
                   {"profile": "AIOU", "url": "https://example.com/"},
                   intent="recipe step that never lands")]
    actor = ScriptedActor([propose("files.mkdir", {"path": target},
                                   reason="salvage it differently")])
    h = Harness(tmp_path, world=world, actor=actor)
    task = make_task("recipe-ish task", frozen, _needs(target), planned_by="recipe")

    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert task.counters.escalations == 1
    assert task.mode == "actor"
    assert task.context["escalated_from"] == "recipe"
    assert h.events_of("TASK_ESCALATED")
    assert world.files[target] == "<dir>"
    # the actor was shown what the frozen plan had already tried
    assert any("chrome.open_profile" in str(c.get("history")) for c in actor.calls)


def test_user_fixable_blocker_is_never_escalated(tmp_path):
    missing = str(tmp_path / "not-there")
    actor = ScriptedActor([finish()])      # would raise if the loop ever called it
    h = Harness(tmp_path, world=FakeWorld(), actor=actor)
    # the frozen plan asks the user a question: a precise, user-fixable blocker
    frozen = [step(1, "agent.ask_user", {"question": "which folder did you mean?"},
                   intent="ask instead of guessing")]
    task = make_task("blocked recipe", frozen, _needs(missing), planned_by="recipe")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED     # the honest, precise ask is preserved
    assert task.counters.escalations == 0
    assert actor.calls == []
    assert not h.events_of("TASK_ESCALATED")


def test_no_recipe_or_skill_match_creates_an_actor_task(tmp_path):
    """The fast lane is only for known work; novel requests go to the actor."""

    class StubComposer:
        calls = 0

        async def compose_intent(self, text):
            StubComposer.calls += 1
            return (f"do: {text}",
                    Condition(all_of=[Predicate(path="files.exists", op="==", value=True,
                                                args={"path": str(tmp_path / "x")})]),
                    ["be careful"])

    settings = Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path)
    memory = MemoryStore(Database(tmp_path / "m.db"))
    manager = TaskManager(settings, memory, composer=StubComposer())

    task, reply = asyncio.run(manager.handle("audit last month's expenses and write a summary"))

    assert reply is None and task is not None
    assert StubComposer.calls == 1
    assert task.mode == "actor" and task.planned_by == "actor"
    assert task.plan == []          # no frozen steps: decisions happen at runtime
    assert task.constraints == ["be careful"]


def test_escalation_only_happens_once(tmp_path):
    target = str(tmp_path / "twice")
    world = FakeWorld()
    world.stall_navigation = True
    frozen = [step(1, "chrome.open_profile", {"profile": "AIOU",
                                              "url": "https://example.com/"},
                   intent="never lands")]
    # the actor also fails to finish -> the loop must not bounce back to the recipe
    actor = ScriptedActor([propose("files.delete", {"path": str(tmp_path / "gone")}),
                           propose("files.delete", {"path": str(tmp_path / "gone2")})])
    h = Harness(tmp_path, world=world, actor=actor)
    task = make_task("recipe-ish task", frozen, _needs(target), planned_by="recipe")

    res = h.run(task)

    assert res.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)
    assert task.counters.escalations == 1       # one handoff, ever
