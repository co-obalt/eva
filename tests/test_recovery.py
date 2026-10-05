"""S5 — honest reporting, bounded recovery, anti-loop (doc 11 §S5)."""
from __future__ import annotations

from eva.core import Condition, Predicate, TaskStatus
from tests.fakes import (FakeWorld, Harness, ScriptedPlanner, make_task, step,
                         whatsapp_completion, whatsapp_steps)


def test_s5a_failure_is_honest_never_fake_completion(tmp_path):
    world = FakeWorld()
    world.stall_navigation = True
    world.wa_login = True
    h = Harness(tmp_path, world=world)

    task = make_task("open whatsapp web", whatsapp_steps()[:1], whatsapp_completion())
    res = h.run(task)

    assert res.status is TaskStatus.FAILED
    assert res.message.startswith("FAILED")
    low = res.message.lower()
    assert "completed" not in low or "unmet completion condition" in low
    assert world.open_calls == 2                      # retries bounded to max_attempts
    assert len(task.failures) >= 2
    assert "Asserted" not in res.message


def test_retry_then_alternative_action_succeeds(tmp_path):
    world = FakeWorld()                               # not healed -> flaky.click fails
    h = Harness(tmp_path, world=world)
    completion = Condition(all_of=[Predicate(path="flaky.healed", op="==", value=True)])
    task = make_task("fix the UI state", [step(1, "flaky.click")], completion)
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert world.healed is True
    replans = h.events_of("TASK_REPLAN")
    assert any("alternative" in e.payload.get("reason", "") for e in replans)


class HealingPlanner(ScriptedPlanner):
    """Repair informed by the new state: the stall clears on the repair path."""

    def __init__(self, world, plan=None, repairs=None):
        super().__init__(plan=plan, repairs=repairs)
        self.world = world

    async def repair(self, task, *, reason, state=None):
        self.world.stall_navigation = False
        return await super().repair(task, reason=reason, state=state)


def test_s5b_repair_plan_replaces_failing_tail(tmp_path):
    """Zero progress forces a repair; a repair that works completes the task."""
    world = FakeWorld()
    world.stall_navigation = True
    failing = [step(1, "browser.navigate", {"url": "https://wrong.invalid"})]
    fixed = [step(9, "chrome.open_profile", {"profile": "aiou", "url": "https://web.whatsapp.com/"})]
    planner = HealingPlanner(world, plan=failing, repairs=[fixed])
    h = Harness(tmp_path, world=world, planner=planner)

    completion = Condition(all_of=[Predicate(path="chrome.page_url", op="matches",
                                             value="*web.whatsapp.com*")])
    task = make_task("open the site", [], completion)
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert task.counters.repairs == 1
    replan_events = h.events_of("TASK_REPLAN")
    assert any("repair" in str(e.payload.get("reason", "")).lower() or
               e.payload.get("repair") == 1 for e in replan_events)


def test_s5c_two_failed_repairs_end_in_blocked_with_history(tmp_path):
    world = FakeWorld()
    world.stall_navigation = True
    failing = [step(1, "browser.navigate", {"url": "https://a.invalid"})]
    repairs = [
        [step(2, "browser.navigate", {"url": "https://b.invalid"})],
        [step(3, "browser.navigate", {"url": "https://c.invalid"})],
        [step(4, "browser.navigate", {"url": "https://d.invalid"})],
    ]
    planner = ScriptedPlanner(plan=failing, repairs=repairs)
    h = Harness(tmp_path, world=world, planner=planner)
    completion = Condition(all_of=[Predicate(path="chrome.page_url", op="matches",
                                             value="*never.example*")])
    task = make_task("impossible navigation", [], completion)
    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    assert task.counters.repairs == 2                  # repairs bounded
    assert len(task.failures) >= 6                     # full history attached
    assert h.events_of("TASK_WAITING_USER")            # the user is told, not guessed at
    assert "BLOCKED" in res.message


def test_s5d_same_action_same_state_max_two_attempts(tmp_path):
    world = FakeWorld()
    world.stall_navigation = True
    h = Harness(tmp_path, world=world)
    completion = Condition(all_of=[Predicate(path="chrome.page_url", op="matches", value="*x*")])
    task = make_task("stuck navigation", [step(1, "browser.navigate", {"url": "https://x.invalid"})],
                     completion)
    res = h.run(task)
    # 2 attempts on the original + 0 extra: step budget and anti-loop hold
    assert world.stall_navigation is True
    assert res.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
    assert task.counters.actions_run <= 4
