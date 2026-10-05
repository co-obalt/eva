#!/usr/bin/env python
"""EVA pet — a small always-on-top desktop avatar showing EVA's live state.

Separate process by design: it reads ``data/presence.json`` (written by whichever
EVA process is running — CLI, headless run, dashboard) and draws a face for it.
Nothing is coupled, nothing is spawned: when EVA is quiet, the pet sleeps.

Run:   python eva_pet.py          (close it with the window's ✕)
Flags: --no-topmost   do not float above other windows
"""
from __future__ import annotations

import argparse
import time
from typing import Any

from eva.avatar.presence import STALE_AFTER_S, presence_path, read_presence

# Pure presentation: state -> face spec (kept out of the UI code so it is testable
# without a display).
FACE = {
    "SLEEP":   {"color": "#3b4a63", "eyes": "closed", "mouth": "flat",   "zzz": True},
    "IDLE":    {"color": "#5b8ff9", "eyes": "open",   "mouth": "smile",  "zzz": False},
    "THINKING": {"color": "#8f7bf7", "eyes": "up",    "mouth": "small",  "zzz": False},
    "WORKING": {"color": "#3fbf8f", "eyes": "open",   "mouth": "flat",   "zzz": False},
    "SUCCESS": {"color": "#34c77a", "eyes": "happy",  "mouth": "smile",  "zzz": False},
    "FAIL":    {"color": "#e05b5b", "eyes": "down",   "mouth": "frown",  "zzz": False},
    "WAITING": {"color": "#f2b04e", "eyes": "wide",   "mouth": "small",  "zzz": False},
}


def face_for(state: str, stale: bool = False) -> dict[str, Any]:
    """The face spec for a presence state — sleeping when nothing is happening."""
    if stale or state in ("NONE", "", None):
        return dict(FACE["SLEEP"])
    return dict(FACE.get(state, FACE["IDLE"]))


def status_lines(presence: dict[str, Any]) -> list[str]:
    """Title / headline / detail shown under the face (viewer-side, pure)."""
    if presence.get("stale"):
        lines = ["EVA is sleeping"]
        last = str(presence.get("headline") or "")
        if last and last not in ("ready", "idle"):
            lines.append(f"last: {last}")
        updated = presence.get("updated_at") or 0.0
        if updated:
            mins = int(max(0.0, time.time() - updated) / 60)
            lines.append(f"last active {mins}m ago")
        lines.append("run python eva_cli.py")
        return lines
    lines = [str(presence.get("headline") or "ready")]
    if presence.get("detail"):
        lines.append(str(presence["detail"]))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eva_pet", description="EVA desktop avatar")
    parser.add_argument("--no-topmost", action="store_true",
                        help="do not keep the pet above other windows")
    args = parser.parse_args(argv)

    import tkinter as tk          # lazy: the pure helpers above stay importable headless

    from eva.config import get_settings
    path = presence_path(get_settings())

    BG = "#0b1220"
    INK = "#0b1220"

    root = tk.Tk()
    root.title("EVA pet")
    root.configure(bg=BG)
    root.geometry("260x340")
    root.resizable(False, False)
    if not args.no_topmost:
        root.wm_attributes("-topmost", True)

    canvas = tk.Canvas(root, width=240, height=200, bg=BG, highlightthickness=0)
    canvas.pack(pady=(14, 0))
    title = tk.Label(root, text="EVA", font=("Segoe UI", 14, "bold"), bg=BG, fg="#e8edf7")
    title.pack(pady=(6, 0))
    headline = tk.Label(root, text="…", font=("Segoe UI", 10), bg=BG, fg="#9fb0cc",
                        wraplength=230, justify="center")
    headline.pack(pady=(2, 0))
    detail = tk.Label(root, text="", font=("Segoe UI", 9), bg=BG, fg="#6c7d99",
                      wraplength=230, justify="center")
    detail.pack(pady=(2, 0))
    tk.Label(root, text="python eva_cli.py · eva_dashboard.py",
             font=("Segoe UI", 8), bg=BG, fg="#4a5a75").pack(side="bottom", pady=8)

    def draw(spec: dict[str, Any]) -> None:
        canvas.delete("all")
        body = spec["color"]
        canvas.create_oval(30, 20, 210, 200, fill=body, outline="")
        # eyes
        eyes, cx = spec["eyes"], 120
        for dx in (-35, 35):
            x = cx + dx
            if eyes == "closed":
                canvas.create_line(x - 11, 96, x + 11, 96, fill=INK, width=3)
            elif eyes == "happy":
                canvas.create_arc(x - 12, 84, x + 12, 104, start=180, extent=180,
                                  style="arc", outline=INK, width=3)
            else:
                y = 94
                if eyes == "up":
                    y = 90
                elif eyes == "down":
                    y = 99
                rx, ry = (13, 13) if eyes == "wide" else (9, 11)
                canvas.create_oval(x - rx, y - ry, x + rx, y + ry, fill=INK, outline="")
        # mouth
        mouth = spec["mouth"]
        if mouth == "smile":
            canvas.create_arc(85, 108, 155, 158, start=200, extent=140, style="arc",
                              outline=INK, width=4)
        elif mouth == "frown":
            canvas.create_arc(85, 122, 155, 172, start=20, extent=140, style="arc",
                              outline=INK, width=4)
        elif mouth == "small":
            canvas.create_oval(112, 128, 128, 142, fill=INK, outline="")
        else:
            canvas.create_line(100, 134, 140, 134, fill=INK, width=4)
        if spec.get("zzz"):
            canvas.create_text(206, 42, text="z", font=("Segoe UI", 16, "bold"),
                               fill="#c9d6ee")
            canvas.create_text(222, 24, text="z", font=("Segoe UI", 12, "bold"),
                               fill="#9fb0cc")

    def tick() -> None:
        pres = read_presence(path)
        face = face_for(str(pres.get("state") or ""), bool(pres.get("stale")))
        draw(face)
        canvas.configure(bg=BG)
        lines = status_lines(pres)
        headline.config(text=lines[0])
        detail.config(text="\n".join(lines[1:]))
        # ambient: tint the window border to the mood while active
        root.configure(bg=BG)
        root.after(600, tick)

    tick()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
