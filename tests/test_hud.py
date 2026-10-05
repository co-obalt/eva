"""M1 surface — the full-screen HUD: pure model + render (no display needed)."""
from __future__ import annotations

from types import SimpleNamespace

from rich.console import Console

from eva.ui.hud import (HudModel, apply_key, ascii_orb, bar, big_clock,
                        classify_input, cpu_groups, feed_line, matrix,
                        mode_label, render, sparkline)


def ev(event_type: str, **payload) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, payload=payload)


def test_pure_bars_are_total():
    assert sparkline([]) == ""
    assert sparkline([0, 50, 100]) == " ▄█"
    assert len(sparkline([1, 2, 3] * 30, width=10)) == 10
    assert bar(0.5, 4) == "██░░"
    assert bar(-1) == "░" * 20 and bar(9) == "█" * 20


def test_big_clock_and_matrix_are_total():
    rows = big_clock("12:00:00")
    assert len(rows) == 5 and all(len(r) == len(rows[0]) for r in rows)
    assert rows == big_clock("12:00:00")                    # deterministic
    assert "█" in rows[0] and ":" not in rows[0]            # glyphs, not text
    assert len(set(map(len, big_clock("9")))) == 1          # short input ok
    assert matrix(0.5, 4, 2) == ["■■■■", "····"]
    assert matrix(9)[0].count("■") == 24                    # clamped high
    assert set(matrix(-1)[0]) == {"·"}                       # clamped low


def test_ascii_orb_and_mode_label():
    orb = ascii_orb("SUCCESS")
    assert len(orb) == 5 and all(len(r) == len(orb[0]) for r in orb)
    assert orb == ascii_orb("SUCCESS")
    assert orb != ascii_orb("FAIL")                         # mood changes the face
    assert ascii_orb("NONE") == ascii_orb("SLEEP", stale=True)
    assert mode_label("") == "-- NORMAL --"
    assert mode_label("/rules") == "-- COMMAND --"
    assert mode_label("open notepad") == "-- GOAL --"


def test_cpu_groups_splits_cores():
    hist = [[10.0] * 4 + [30.0] * 4, [20.0] * 4 + [40.0] * 4]
    groups = cpu_groups(hist, 8)
    assert [g[0] for g in groups] == ["# 1 - 4", "# 5 - 8"]
    assert groups[0][1] == [10.0, 20.0] and groups[1][1] == [30.0, 40.0]
    assert cpu_groups([], 8) == []                          # cold start
    assert cpu_groups(hist, 0) == []                        # no cores known


def test_classify_input_and_line_editor():
    assert classify_input("  /rules ") == ("command", "/rules")
    assert classify_input("open notepad") == ("goal", "open notepad")
    assert classify_input("   ") == ("empty", "")
    buf, sub, quit = "", None, False
    for ch in "cpu usage":
        buf, sub, quit = apply_key(buf, ch)
        assert sub is None and not quit
    assert buf == "cpu usage"
    buf, sub, quit = apply_key(buf, "\r")           # Enter submits and clears
    assert (buf, sub, quit) == ("", "cpu usage", False)
    assert apply_key("abc", "\b")[0] == "ab"
    assert apply_key("abc", "\x1b") == ("", None, False)   # Esc drops the draft...
    assert apply_key("", "\x1b") == ("", None, True)       # ...twice = quit
    assert apply_key("", "\x03")[2] is True                # Ctrl+C quits
    assert apply_key("x", "\t") == ("x", None, False)      # Tab is the caller's job
    assert apply_key("", " ")[0] == " "                    # whitespace types fine


def test_feed_lines_bucket_events():
    bucket, line = feed_line(ev("ACTOR_ACTION", kind="tool",
                                action="files.mkdir({})", reason="first folder"))
    assert bucket == "steps" and "files.mkdir" in line and "first folder" in line
    bucket, line = feed_line(ev("TASK_STEP_RESULT", status="VERIFIED",
                                effect="files.exists"))
    assert bucket == "steps" and "✓" in line
    assert feed_line(ev("TASK_FINISHED", status="success", message="done"))[0] == "session"
    assert feed_line(ev("TASK_PLAN_READY"))[0] == "state"
    assert feed_line(ev("OBSERVATION", path="files.exists")) is None   # noise stays out


def _model() -> HudModel:
    sys = {"clock": "12:00:00", "date": "2026 OCT 05", "host_uptime": "1:02:03",
           "platform": "windows", "battery": "98%", "cpu": 42.0, "cores": 8,
           "core_pct": [40.0, 41.0, 43.0, 44.0, 45.0, 44.0, 42.0, 39.0],
           "mem_used": 6 * 1024 ** 3, "mem_total": 32 * 1024 ** 3,
           "disk_used": 100 * 1024 ** 3, "disk_total": 500 * 1024 ** 3,
           "top": [{"pid": 1, "name": "eva", "cpu": 3.2, "mem": 1.1}]}
    eva = {"presence": {"state": "SUCCESS", "stale": False, "headline": "task finished",
                        "detail": "verified"}, "tools": 42, "rules": 3, "skills": 5,
           "episodes": 12, "jobs": 0, "actor": True,
           "providers": [{"name": "groq", "alive": True}]}
    model = HudModel(presence_file="x", system=lambda: sys, eva=lambda: eva)
    model.refresh(force=True)
    model.feed(ev("TASK_STARTED", goal="make a folder"))
    model.feed(ev("ACTOR_ACTION", kind="tool", action="browser.find({})",
                  reason="locate the control"))
    return model


def test_model_buffers_filter_by_tab():
    model = _model()
    assert model.data["system"]["cpu"] == 42.0 and model.cpu_hist
    assert any("goal" in line for line in model.lines())          # SESSION tab
    model.tab = 1                                                 # STEPS tab
    assert any("browser.find" in line for line in model.lines())
    assert not any("goal" in line for line in model.lines())
    model.say("line1\n\nline2", bucket="steps")
    assert model.lines()[-2:] == ["line1", "line2"]               # empty lines dropped
    model.tab = 2                                                 # STATE tab is empty
    assert model.lines() == []


def test_render_builds_every_panel():
    model = _model()
    console = Console(record=True, width=160, height=44, force_terminal=True)
    console.print(render(model))
    text = console.export_text()
    for title in ("SYSTEM", "CPU", "MEMORY", "DISK", "TOP PROCESSES",
                  "EVA", "ACTIVITY", "SESSION"):
        assert title in text, title
    assert "esc exits" in text                                  # input bar
    assert "EXIT DISPLAY" in text                              # eDEX exit button
    assert "FILESYSTEM" in text and "DISK" in text             # fs panel + disk line
    assert "# 1 - 4" in text and "# 5 - 8" in text             # grouped core sparklines
    assert "TASKS" in text and "SWAP" in text                  # temp/tasks + swap rows
    assert "-- NORMAL --" in text                              # status line mode
    block_clock = big_clock("12:00:00")
    assert all(row in text for row in block_clock)            # block-digit clock
    assert "42.0" in text and "groq" in text                    # CPU + providers
    assert "task finished" in text                              # presence headline


def test_render_survives_a_cold_start():
    cold = Console(record=True, width=120, height=30, force_terminal=True)
    cold.print(render(HudModel(presence_file="x",
                               system=lambda: {}, eva=lambda: {})))
    assert "SYSTEM" in cold.export_text()
