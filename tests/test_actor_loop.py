"""M1 — the actor loop: it decides its own steps, and its budgets are real limits.

Everything here runs through the same surfaces the rest of the suite uses
(Harness + fake registry + the real orchestrator), so the gates (P2/P3/P6/P9) are
the production ones, not a test double of the loop.
"""
from __future__ import annotations

from eva.config import Settings
from eva.core import Condition, Predicate, TaskStatus
from tests.fakes import FakeWorld, Harness, ScriptedActor, finish, make_task, propose


def _needs(*paths: str) -> Condition:
    return Condition(all_of=[Predicate(path="files.exists", op="==", value=True,
                                       args={"path": p}) for p in paths])


def _settings(tmp_path, **kw) -> Settings:
    return Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path, **kw)


def test_actor_composes_its_own_steps_and_finishes(tmp_path):
    world = FakeWorld()
    a, b = str(tmp_path / "one"), str(tmp_path / "two")
    actor = ScriptedActor([
        propose("files.mkdir", {"path": a}, reason="make the first folder"),
        propose("files.mkdir", {"path": b}, reason="make the second folder"),
    ])
    h = Harness(tmp_path, world=world, actor=actor)
    task = make_task("make two folders", [], _needs(a, b), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert task.mode == "actor"
    assert [s.tool for s in task.plan] == ["files.mkdir", "files.mkdir"]
    assert [s.intent for s in task.plan] == ["make the first folder", "make the second folder"]
    assert world.files[a] == "<dir>" and world.files[b] == "<dir>"
    # one decision per action, and the loop stopped the moment the condition held
    assert task.counters.actor_calls == 2
    # narration is durable: the journal keeps WHY each action was taken
    reasons = h.store.journal_reasons(task.id)
    assert "make the first folder" in reasons
    assert "make the second folder" in reasons
    assert [e.type for e in h.events].count("ACTOR_ACTION") == 2
    # the actor was shown real observations and the remaining budget, not a blank prompt
    assert actor.calls[-1]["state"]
    assert actor.calls[-1]["budget"]["actions_left"] >= 0


def test_actor_action_budget_blocks_instead_of_spinning(tmp_path):
    actor = ScriptedActor([propose("files.mkdir", {"path": str(tmp_path / f"p{i}")},
                                   reason=f"attempt {i}") for i in range(10)])
    h = Harness(tmp_path, world=FakeWorld(), actor=actor, max_steps=3)
    task = make_task("never satisfiable", [], _needs(str(tmp_path / "never")), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert "budget" in res.message.lower()
    assert task.counters.actions_run == 3
    assert task.counters.repairs == 1          # one bounded re-prompt, then an honest stop
    waiting = h.events_of("TASK_WAITING_USER")
    assert waiting and waiting[-1].payload.get("blocker") == "actor_stuck"


def test_actor_token_budget_stops_immediately(tmp_path):
    actor = ScriptedActor([propose("files.mkdir", {"path": str(tmp_path / "x")})])
    settings = _settings(tmp_path, actor_token_budget=0, actor_repairs=0)
    h = Harness(tmp_path, world=FakeWorld(), actor=actor, settings=settings)
    task = make_task("token bound", [], _needs(str(tmp_path / "never")), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert "token budget" in res.message.lower()
    assert task.counters.actions_run == 0


def test_actor_wall_clock_budget_stops(tmp_path):
    actor = ScriptedActor([propose("files.mkdir", {"path": str(tmp_path / "y")})])
    settings = _settings(tmp_path, actor_wall_clock_s=-1.0, actor_repairs=0)
    h = Harness(tmp_path, world=FakeWorld(), actor=actor, settings=settings)
    task = make_task("wall clock bound", [], _needs(str(tmp_path / "never")), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert "wall-clock" in res.message.lower()


def test_actor_repeating_itself_is_stopped(tmp_path):
    same = {"path": str(tmp_path / "again")}
    actor = ScriptedActor([propose("files.mkdir", same, reason="same thing again")
                           for _ in range(6)])
    h = Harness(tmp_path, world=FakeWorld(), actor=actor)
    task = make_task("loop forever", [], _needs(str(tmp_path / "never")), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert "repeated" in res.message.lower()


def test_actor_finish_is_only_a_request(tmp_path):
    actor = ScriptedActor([finish(), finish(), finish()])
    h = Harness(tmp_path, world=FakeWorld(), actor=actor)
    task = make_task("claim victory", [], _needs(str(tmp_path / "missing")), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert "completion condition is not satisfied" in res.message
    assert task.counters.actor_calls == 2      # two claims, then the bounded stop


def test_actor_without_a_model_degrades_honestly(tmp_path):
    """No provider -> no actor: fail with a clear reason, never a fake success."""
    h = Harness(tmp_path, world=FakeWorld(), actor=ScriptedActor([]))
    h.orchestrator.actor = None
    task = make_task("no brain available", [], _needs(str(tmp_path / "x")), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.FAILED
    assert "no actor available" in res.message


def test_actor_failure_is_feedback_not_a_wall(tmp_path):
    """A failed action is fed back; the next action may differ and succeed."""
    target = str(tmp_path / "fresh")
    actor = ScriptedActor([
        # wa.login is False in the fake world -> this action cannot run yet
        propose("wa.open_archived", reason="wrong first guess"),
        propose("files.mkdir", {"path": target}, reason="actually create it"),
    ])
    h = Harness(tmp_path, world=FakeWorld(), actor=actor)
    task = make_task("recover from a wrong guess", [], _needs(target), mode="actor")

    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    # the failed precondition is recorded as feedback, not as a task verdict
    assert [s.status.value for s in task.plan] == ["FAILED", "VERIFIED"]
    seen = actor.calls[1]["history"]
    assert any("precondition" in str(h_.get("observed", "")).lower() for h_ in seen)
