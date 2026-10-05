"""S4 risk gating + S2 idempotency (doc 11 §S4/§S2)."""
from __future__ import annotations

from eva.core import Condition, Predicate, RiskLevel, TaskStatus
from eva.core.state import request_key
from tests.fakes import FakeWorld, Harness, ScriptedConfirm, make_task, step

RECIPIENT = "Saad Bhai"
TEXT = "phir kayaa?"
SENT = [Predicate(path="comms.sent", op="==", value=True,
                  args={"recipient": RECIPIENT, "text": TEXT})]


def send_task():
    return make_task("message saad bhai", [step(1, "comms.send_message",
                                                {"recipient": RECIPIENT, "text": TEXT})],
                     Condition(all_of=SENT))


# ------------------------------------------------------------------- S4
def test_s4a_critical_delete_shows_size_and_path_and_decline_is_clean(tmp_path):
    big = tmp_path / "bigfolder"
    big.mkdir()
    (big / "movie.mp4").write_bytes(b"x" * 2048)       # 2 KB > gate threshold (1000 B)
    world = FakeWorld()
    world.files[str(big)] = "<dir>"
    confirm = ScriptedConfirm([False])
    h = Harness(tmp_path, world=world, confirm=confirm)
    completion = Condition(all_of=[Predicate(path="files.exists", op="==", value=False,
                                             args={"path": str(big)})])
    task = make_task("delete this folder", [step(1, "files.delete", {"path": str(big)})],
                     completion)
    res = h.run(task)

    assert res.status is TaskStatus.CANCELLED
    assert confirm.seen, "CRITICAL action must reach the confirmation gate"
    decision = confirm.seen[0]
    assert decision.level is RiskLevel.CRITICAL
    assert str(big) in decision.consequences
    assert "2.0 KB" in decision.consequences          # concrete consequence, not vibes
    assert world.deleted == []                        # files intact
    assert big.exists()


def test_s4b_high_outbound_message_requires_confirmation(tmp_path):
    world = FakeWorld()
    confirm = ScriptedConfirm([False])                # decline/timeout -> cancel (fail-safe)
    h = Harness(tmp_path, world=world, confirm=confirm)
    res = h.run(send_task())

    assert res.status is TaskStatus.CANCELLED
    d = confirm.seen[0]
    assert d.level is RiskLevel.HIGH
    assert RECIPIENT in d.consequences and TEXT in d.consequences
    assert world.messages == [] and world.send_calls == 0


def test_s4b_approval_sends_exactly_once(tmp_path):
    world = FakeWorld()
    confirm = ScriptedConfirm([True])
    h = Harness(tmp_path, world=world, confirm=confirm)
    res = h.run(send_task())

    assert res.status is TaskStatus.FINISHED
    assert world.send_calls == 1
    assert len(world.messages) == 1


def test_s4c_llm_cannot_lower_risk(tmp_path):
    world = FakeWorld()
    confirm = ScriptedConfirm([False])
    h = Harness(tmp_path, world=world, confirm=confirm)
    st = step(1, "comms.send_message", {"recipient": RECIPIENT, "text": TEXT},
              risk=RiskLevel.LOW)                     # even if the planner says low...
    task = make_task("message", [st], Condition(all_of=SENT))
    h.run(task)

    assert confirm.seen and confirm.seen[0].level is RiskLevel.HIGH   # engine is authoritative


def test_s4d_low_risk_runs_without_confirmation(tmp_path):
    world = FakeWorld()
    confirm = ScriptedConfirm([])                     # any call would auto-decline
    h = Harness(tmp_path, world=world, confirm=confirm)
    completion = Condition(all_of=[Predicate(path="apps.running", op="==", value=True,
                                             args={"name": "notepad"})])
    task = make_task("open notepad", [step(1, "apps.launch", {"name": "notepad"})], completion)
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert confirm.seen == []


# ------------------------------------------------------------------- S2
def test_s2a_already_true_effect_is_skipped(tmp_path):
    world = FakeWorld()
    world.files["C:/x/y"] = "<dir>"                   # effect of step 1 already true
    h = Harness(tmp_path, world=world)
    completion = Condition(all_of=[
        Predicate(path="files.exists", op="==", value=True, args={"path": "C:/x/y"}),
        Predicate(path="apps.running", op="==", value=True, args={"name": "notepad"}),
    ])
    task = make_task("create folder then open notepad", [
        step(1, "files.mkdir", {"path": "C:/x/y"}),
        step(2, "apps.launch", {"name": "notepad"}),
    ], completion)
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert task.counters.actions_run == 1              # only step 2 actually ran
    assert len(h.events_of("TASK_STEP_SKIPPED")) == 1
    assert len(h.events_of("TOOL_START")) == 1


def test_s2b_rerun_never_duplicates_side_effects(tmp_path):
    world = FakeWorld()
    h = Harness(tmp_path, world=world)
    completion = Condition(all_of=[Predicate(path="files.exists", op="==", value=True,
                                             args={"path": "C:/dir/one"})])
    task1 = make_task("mkdir one", [step(1, "files.mkdir", {"path": "C:/dir/one"})], completion)
    assert h.run(task1).status is TaskStatus.FINISHED

    task2 = make_task("mkdir one", [step(1, "files.mkdir", {"path": "C:/dir/one"})], completion)
    res2 = h.run(task2)
    assert res2.status is TaskStatus.FINISHED
    assert len(h.events_of("TOOL_START")) == 1         # executed exactly once across both runs


def test_s2c_effect_token_prevents_duplicate_send_on_resume(tmp_path):
    """Resume re-executes the step, but the effect token returns the prior result:
    exactly ONE world mutation ever."""
    world = FakeWorld()
    h = Harness(tmp_path, world=world, confirm=ScriptedConfirm([True]))
    mk = "C:/dir/after"
    steps = [
        step(1, "comms.send_message", {"recipient": RECIPIENT, "text": TEXT}),
        step(2, "files.mkdir", {"path": mk}),
    ]
    completion = Condition(all_of=[Predicate(path="files.exists", op="==", value=True,
                                             args={"path": mk})])
    task = make_task("send then mkdir", steps, completion)

    # simulate: the send DID happen in a previous life (token recorded), but the
    # transcript observation is unavailable -> state check cannot see it.
    h.store.effect_token_put(task.id, task.plan[0], {"sent": True})
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert world.send_calls == 0                       # duplicate send prevented
    skip_events = h.events_of("IDEMPOTENT_SKIP")
    assert skip_events and skip_events[0].payload["tool"] == "comms.send_message"
    assert world.files.get(mk) == "<dir>"              # the rest of the task completed
