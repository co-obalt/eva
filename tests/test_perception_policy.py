"""S6 — perception ladder discipline + INV-1/2/3/4 (doc 11 §S6, doc 13)."""
from __future__ import annotations

import time

import pytest

from eva.core import Condition, ObservationRequest, Predicate, Task, TaskStatus
from eva.core.task import Step
from eva.perception.policy import PolicyGate, PolicyViolation
from tests.fakes import FakeWorld, Harness, make_task, whatsapp_completion, whatsapp_steps


def _click_task(x=10, y=20, justification=None):
    args = {"x": x, "y": y}
    if justification is not None:
        args["justification"] = justification
    return make_task("click a thing", [Step(n=1, intent="click", tool="ui.click_xy", args=args)],
                     Condition(all_of=[Predicate(path="ui.last_click", op="==",
                                                 value=f"{x},{y}")]))


def test_s6f_unjustified_coordinate_click_is_rejected(tmp_path):
    h = Harness(tmp_path, world=FakeWorld())
    task = _click_task(justification=None)
    res = h.run(task)

    assert res.status is TaskStatus.FAILED
    rejected = h.events_of("POLICY_REJECTED")
    assert rejected and "INV-2" in rejected[0].payload["reason"]
    assert h.world.clicks == []                       # nothing was clicked
    assert task.counters.coord_actions == 0           # rejection is not a coordinate action
    fail = [f for f in task.failures if f.kind == "policy_rejection"]
    assert fail, "policy rejection must be recorded in failures"


def test_s6f_justified_coordinate_click_is_accepted_and_counted(tmp_path):
    h = Harness(tmp_path, world=FakeWorld())
    task = _click_task(justification="structural targeting exhausted: L2 no control, L3 no DOM node")
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert h.world.clicks == ["10,20"]
    assert task.counters.coord_actions == 1           # counted, auditable
    assert h.events_of("POLICY_REJECTED") == []


def test_s6g_vision_request_without_justification_is_rejected():
    gate = PolicyGate(allow_vision=True)
    task = Task(goal="g", completion=Condition(all_of=[Predicate(path="a.b", op="==", value=1)]))
    gate.bind_task(task)

    with pytest.raises(PolicyViolation):
        gate.require_observation("L7", reason="", exhausted_levels=["L1"], task=task)
    with pytest.raises(PolicyViolation):
        gate.require_observation("L7", reason="dialog unreadable", exhausted_levels=[], task=task)
    assert task.counters.l7_calls == 0                # rejected requests never count

    gate.require_observation("L7", reason="dialog unreadable at L1-L5",
                             exhausted_levels=["L1", "L2", "L3", "L4", "L5"], task=task)
    assert task.counters.l7_calls == 1


def test_s6g_vision_disabled_rejects_even_with_justification():
    gate = PolicyGate(allow_vision=False)
    with pytest.raises(PolicyViolation) as exc:
        gate.require_observation("L7", reason="weird dialog", exhausted_levels=["L1"], task=None)
    assert "INV-1" in str(exc.value)


def test_s6e_structural_scenario_has_zero_pixel_counters(tmp_path):
    world = FakeWorld()
    world.wa_login = True
    world.wa_view = "chatlist"
    h = Harness(tmp_path, world=world)
    task = make_task("whatsapp archived", whatsapp_steps(), whatsapp_completion())
    h.run(task)

    c = task.counters
    assert c.l5_calls == c.l6_calls == c.l7_calls == c.coord_actions == 0
    assert not h.events_of("POLICY_REJECTED")


def test_stale_observation_is_refreshed_not_reused(tmp_path):
    """Observation cache reuse + invalidate-on-action (doc 04 §4)."""
    from eva.config import get_settings
    from eva.perception.engine import PerceptionEngine
    import asyncio

    engine = PerceptionEngine(get_settings())
    req = [ObservationRequest(path="chrome.running")]

    async def run():
        first = await engine.observe(req, max_age=30.0)
        second = await engine.observe(req, max_age=30.0)     # cache hit (same object)
        engine.invalidate_for([Predicate(path="chrome.running", op="==", value=True)])
        third = await engine.observe(req, max_age=30.0)      # fresh resolve
        await engine.close()
        return first, second, third

    first, second, third = asyncio.run(run())
    k = req[0].key
    assert second[k].at == first[k].at                       # reused, not re-perceived
    assert third[k].at >= second[k].at                       # invalidated -> fresh
