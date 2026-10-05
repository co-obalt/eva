"""M1 surface — the desktop pet's presentation logic (pure: no display needed)."""
from __future__ import annotations

import sys
import time

import eva_pet
from eva_pet import FACE, face_for, status_lines


def test_importing_the_pet_never_touches_tkinter():
    # the viewer stays importable (and testable) on machines without a display
    assert "tkinter" not in sys.modules
    assert callable(eva_pet.main)


def test_face_for_every_known_state():
    for state in ("IDLE", "THINKING", "WORKING", "SUCCESS", "FAIL", "WAITING"):
        face = face_for(state)
        assert set(face) == {"color", "eyes", "mouth", "zzz"}, state
        assert face["color"].startswith("#") and face["zzz"] is False, state
    # sleeping whenever nothing is happening, and unknown states degrade kindly
    assert face_for("", stale=True)["zzz"] is True
    assert face_for("NONE")["zzz"] is True
    assert face_for("NOT_A_STATE") == FACE["IDLE"]


def test_status_lines_read_sensibly():
    assert status_lines({"stale": True, "updated_at": 0.0}) == \
        ["EVA is sleeping", "run python eva_cli.py"]
    quiet = status_lines({"stale": True, "updated_at": time.time() - 600})
    assert quiet[0] == "EVA is sleeping" and "last active 10m ago" in quiet[1]
    assert status_lines({"stale": False, "headline": "files.mkdir",
                         "detail": "first folder"}) == ["files.mkdir", "first folder"]
    assert status_lines({"stale": False, "headline": "", "detail": ""}) == ["ready"]
