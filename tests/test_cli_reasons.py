"""M1 — per-step narration reaches the user: `--explain` (headless) and `/explain`.

The durable half (reason stored in the journal) is asserted in test_actor_loop;
this file covers the surfaces that show it.
"""
from __future__ import annotations

from eva.ui.edex import build_parser
from eva.ui.headless import reason_lines


def test_explain_flag_is_accepted_by_the_cli():
    args = build_parser().parse_args(["--explain", "--run", "open the dashboard"])
    assert args.explain is True
    assert args.run == "open the dashboard"
    assert build_parser().parse_args(["--run", "x"]).explain is False


def test_reason_lines_show_why_each_step_ran():
    steps = [
        {"n": 1, "tool": "browser.find", "status": "VERIFIED",
         "reason": "find the sign-in control before clicking"},
        {"n": 2, "tool": "browser.click", "status": "VERIFIED", "reason": ""},
        {"n": 3, "tool": "browser.type", "status": "FAILED",
         "reason": "type the user's email into the form"},
    ]
    lines = reason_lines(steps)
    assert len(lines) == 2                     # steps without a reason are silent
    assert "browser.find" in lines[0] and "sign-in control" in lines[0]
    assert "browser.type" in lines[1] and "email" in lines[1]
