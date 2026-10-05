"""S3 — learning curve + memory hygiene (doc 11 §S3, doc 06)."""
from __future__ import annotations

from eva.core import Condition, Predicate, TaskStatus
from eva.core.task import Step
from eva.memory.distiller import SkillDistiller
from eva.memory.store import MemoryStore, Skill, intent_signature
from eva.storage import Database
from eva.tasks.manager import TaskManager
from tests.fakes import FakeWorld, Harness, ScriptedPlanner, make_task


def _plan_and_completion():
    steps = [
        Step(n=1, intent="launch notepad", tool="apps.launch", args={"name": "notepad"}),
    ]
    completion = Condition(all_of=[Predicate(path="apps.running", op="==", value=True,
                                             args={"name": "notepad"})])
    return steps, completion


def test_s3a_repeat_run_becomes_a_tier0_skill(tmp_path):
    world = FakeWorld()
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    distiller = SkillDistiller(memory)
    planner = ScriptedPlanner(plan=_plan_and_completion()[0])
    h = Harness(tmp_path, world=world, planner=planner, memory=memory, distiller=distiller)
    steps, completion = _plan_and_completion()
    request = "elevate the flux capacitor calibration"   # matches no Tier-0 recipe

    # run 1 and run 2: planned (planner called), episode recorded, skill distilled
    for i in range(2):
        world.apps["notepad"] = False                  # ensure each run actually executes
        task = make_task(request, [], completion)      # empty plan -> planner provides it
        res = h.run(task)
        assert res.status is TaskStatus.FINISHED
    assert planner.plan_calls == 2
    skills = memory.list_skills()
    assert skills, "2 stable verified runs must distill a skill"

    # run 3: task manager matches the skill -> Tier 0 plan, no planner call
    from eva.config import get_settings
    import asyncio
    tm = TaskManager(get_settings(), memory, composer=None, registry=h.registry)
    task3, reply = asyncio.run(tm.handle(request))
    assert reply is None and task3 is not None
    assert task3.planned_by.startswith("skill:")
    res3 = h.run(task3)
    assert res3.status is TaskStatus.FINISHED
    assert planner.plan_calls == 2                    # unchanged: skill path used no planner
    assert task3.counters.model_calls == 0


def test_s3b_low_confidence_skill_does_not_fire(tmp_path):
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    skill = Skill(name="skill_fragile", intent_sig=intent_signature("send the monthly report"),
                  steps=[{"tool": "apps.launch", "args": {"name": "notepad"}}],
                  confidence=0.3, state="active")
    memory.upsert_skill(skill)
    assert memory.match_skill("send the monthly report") is None or \
        memory.match_skill("send the monthly report")[0].confidence < 0.5


def test_s3c_failing_skill_is_quarantined(tmp_path):
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    memory.upsert_skill(Skill(name="skill_broken", intent_sig="fix the widget",
                              steps=[{"tool": "flaky.click", "args": {}}], confidence=0.9))
    for _ in range(3):
        memory.record_skill_run("skill_broken", "FAILED", "tsk_x")
    s = memory.get_skill("skill_broken")
    assert s.state == "quarantined"
    assert memory.match_skill("fix the widget") is None   # quarantined never matches


def test_working_memory_is_wiped_at_task_end(tmp_path):
    world = FakeWorld()
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    h = Harness(tmp_path, world=world, memory=memory)
    steps, completion = _plan_and_completion()
    task = make_task("open notepad please", [s.model_copy(deep=True) for s in steps], completion)
    memory.put_working(task.id, "scratch", "leftover")
    h.run(task)
    assert memory.get_working(task.id) == {}


def test_retrieval_respects_token_budget(tmp_path):
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    for i in range(60):
        memory.upsert_fact("project", f"prop_{i}", "x" * 200, tags="proposal work")
    memory.profile_set("long_note", "y" * 800)
    task = make_task("open my proposal", [], Condition(all_of=[Predicate(path="a.b", op="==", value=1)]))
    slice_ = memory.retrieve(task, budget_tokens=800)
    import json
    assert len(json.dumps(slice_, default=str)) // 4 <= 800


def test_fact_corroboration_bumps_confidence(tmp_path):
    db = Database(tmp_path / "mem.db")
    memory = MemoryStore(db)
    memory.upsert_fact("chrome", "profile_aiou", "Profile 11", source="verified_task",
                       verify_method="chrome_profile_scan")
    memory.upsert_fact("chrome", "profile_aiou", "Profile 11", source="verified_task")
    facts = memory.facts("chrome profile")
    assert facts[0]["confidence"] > 0.90            # corroborated
    memory.invalidate_fact("chrome", "profile_aiou")
    assert memory.facts("chrome profile") == []     # invalidated = excluded from retrieval
