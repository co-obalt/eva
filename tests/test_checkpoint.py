"""Checkpoints & resumption (doc 03 §7, doc 11 S1e)."""
from __future__ import annotations

from eva.core import TaskStatus
from tests.fakes import FakeWorld, Harness, make_task, whatsapp_completion, whatsapp_steps


def test_cancel_then_resume_never_re_executes_verified_steps(tmp_path):
    world = FakeWorld()
    world.wa_login = True
    world.wa_view = "chatlist"
    h = Harness(tmp_path, world=world)
    h.cancel_after_verified(2)                          # simulated crash after step 2
    task = make_task("whatsapp archived", whatsapp_steps(), whatsapp_completion())

    res = h.run(task)
    assert res.status is TaskStatus.CANCELLED
    assert world.open_calls == 1
    assert world.search_calls == 0
    checkpoint = h.store.latest_checkpoint(task.id)
    assert checkpoint is not None and checkpoint["step_index"] == 2

    res2 = h.resume(task.id)
    assert res2.status is TaskStatus.FINISHED
    assert world.open_calls == 1                        # step 1 NOT re-executed
    assert world.search_calls == 1                      # only the remaining tail ran
    assert world.wa_selected_chat == "Saad Bhai"


def test_resume_re_verifies_completion_condition_first(tmp_path):
    world = FakeWorld()
    world.wa_login = True
    world.wa_view = "chatlist"
    h = Harness(tmp_path, world=world)
    h.cancel_after_verified(2)
    task = make_task("whatsapp archived", whatsapp_steps(), whatsapp_completion())
    assert h.run(task).status is TaskStatus.CANCELLED

    # meanwhile the user finished the job themselves
    world.wa_search_query = "Saad Bhai"
    world.wa_selected_chat = "Saad Bhai"
    res2 = h.resume(task.id)

    assert res2.status is TaskStatus.FINISHED
    assert "Already done" in res2.message
    assert world.search_calls == 0                      # nothing executed on resume


def test_open_tasks_are_listed_after_crash(tmp_path):
    world = FakeWorld()
    world.wa_login = True
    world.wa_view = "chatlist"
    h = Harness(tmp_path, world=world)
    h.cancel_after_verified(1)
    task = make_task("whatsapp archived", whatsapp_steps(), whatsapp_completion())
    h.run(task)

    open_tasks = h.store.list_open_tasks()
    assert any(t.id == task.id for t in open_tasks)
