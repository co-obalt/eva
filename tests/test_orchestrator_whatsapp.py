"""S1 — the flagship WhatsApp scenario (doc 11 §S1), against the fake world."""
from __future__ import annotations

from eva.core import StepStatus, TaskStatus
from tests.fakes import Harness, FakeWorld, make_task, whatsapp_completion, whatsapp_steps


def test_s1_full_flow_zero_redundant_actions(tmp_path):
    world = FakeWorld()
    world.wa_login = True
    world.wa_view = "chatlist"
    h = Harness(tmp_path, world=world)

    task = make_task("open whatsapp in aiou profile, saad bhai ka archived chat kholo",
                     whatsapp_steps(), whatsapp_completion())
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert world.wa_view == "archived"
    assert world.wa_selected_chat == "Saad Bhai"
    assert world.open_calls == 1                       # Chrome launched EXACTLY once
    assert task.counters.redundant_actions == 0        # no repeated launches / focus thrash
    assert task.counters.coord_actions == 0            # no coordinate actions (INV-2)
    assert task.counters.l5_calls == 0 and task.counters.l6_calls == 0
    assert task.counters.l7_calls == 0                 # no screenshots / vision (S6e)
    assert all(s.status.done for s in task.plan)
    assert "completion condition verified" in res.message
    # every verified step carries evidence with a ladder level
    for s in task.plan:
        assert s.evidence, f"step {s.n} has no evidence"
        assert all(e.level.startswith("L") for e in s.evidence)


def test_s1b_already_open_is_a_noop(tmp_path):
    world = FakeWorld()
    world.chrome_running = True
    world.chrome_profile = "Profile 11"
    world.chrome_page_url = "https://web.whatsapp.com/"
    world.wa_login = True
    world.wa_view = "archived"
    world.wa_selected_chat = "Saad Bhai"
    h = Harness(tmp_path, world=world)

    task = make_task("open whatsapp in aiou profile, saad bhai ka archived chat kholo",
                     whatsapp_steps(), whatsapp_completion())
    res = h.run(task)

    assert res.status is TaskStatus.FINISHED
    assert world.open_calls == 0                       # v1 would have launched Chrome here
    assert task.counters.actions_run == 0
    assert task.counters.model_calls == 0
    assert h.events_of("TOOL_START") == []
    assert "Already done" in res.message


def test_s1c_qr_login_blocks_with_exactly_one_ask(tmp_path):
    world = FakeWorld()
    world.wa_login = False
    world.wa_view = "qr_login"
    h = Harness(tmp_path, world=world)

    task = make_task("open whatsapp in aiou profile, saad bhai ka archived chat kholo",
                     whatsapp_steps(), whatsapp_completion())
    res = h.run(task)

    assert res.status is TaskStatus.BLOCKED
    ask_events = h.events_of("TASK_WAITING_USER")
    assert len(ask_events) == 1                        # one ask, no retry storm
    ask = ask_events[0].payload["ask"].lower()
    assert "log in" in ask or "scan" in ask
    blocked = [s for s in task.plan if s.status is StepStatus.BLOCKED]
    assert blocked and blocked[0].n == 2               # blocked at "open archived"
    assert world.wa_view == "qr_login"                 # never forced a fake success
    assert world.search_calls == 0                     # nothing past the blocker ran


def test_s1c_continuation_after_user_fixes_login(tmp_path):
    world = FakeWorld()
    world.wa_login = False
    world.wa_view = "qr_login"
    h = Harness(tmp_path, world=world)
    task = make_task("whatsapp archived chat", whatsapp_steps(), whatsapp_completion())
    res = h.run(task)
    assert res.status is TaskStatus.BLOCKED

    # the user scans the QR, then says "continue" -> resume
    world.wa_login = True
    world.wa_view = "chatlist"
    res2 = h.resume(task.id)
    assert res2.status is TaskStatus.FINISHED
    assert world.wa_selected_chat == "Saad Bhai"
    assert world.open_calls == 1                       # step 1 effects still held: not re-run


def test_verdict_never_comes_from_step_count(tmp_path):
    """P6: all steps VERIFIED but completion unmet -> FAILED, never 'completed N steps'."""
    world = FakeWorld()
    world.wa_login = True
    h = Harness(tmp_path, world=world)
    steps = [s for s in whatsapp_steps() if s.tool != "wa.open_chat"]
    task = make_task("partial whatsapp flow", steps, whatsapp_completion())
    res = h.run(task)

    assert res.status is TaskStatus.FAILED
    assert "FAILED" in res.message
    assert "completed" not in res.message.lower()
    # the plan steps it could do were genuinely verified
    assert any(s.status is StepStatus.VERIFIED for s in task.plan)
