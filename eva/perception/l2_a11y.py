"""L2 — accessibility tree (doc 04). Win32 window/control enumeration.

Lightweight and dependency-free: top-level windows + child controls with
class names, text, enabled/visible state. Full UI Automation is the documented
upgrade path; this covers classic Win32 apps (Notepad dialogs, file pickers,
installers) which is exactly where L2 beats pixels.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from .l1_system import IS_WINDOWS

if IS_WINDOWS:
    _user32 = ctypes.windll.user32

    def _text(hwnd: int) -> str:
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        _user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value

    def _class(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value


def find_top_window(title_contains: str) -> int | None:
    if not IS_WINDOWS:
        return None
    found: list[int] = []
    q = title_contains.casefold()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _l):
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _text(hwnd)
        if q in title.casefold():
            found.append(int(hwnd))
            return False
        return True

    _user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def child_controls(hwnd: int, limit: int = 120) -> list[dict]:
    if not IS_WINDOWS:
        return []
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(child, _l):
        if len(out) >= limit:
            return False
        out.append({
            "hwnd": int(child),
            "class": _class(child),
            "text": _text(child),
            "visible": bool(_user32.IsWindowVisible(child)),
            "enabled": bool(_user32.IsWindowEnabled(child)),
        })
        return True

    _user32.EnumChildWindows(hwnd, _cb, 0)
    return out


def find_control(window_title_contains: str, control_text_contains: str) -> dict | None:
    """Structural targeting for classic apps: find a named control (INV-2 ladder)."""
    top = find_top_window(window_title_contains)
    if top is None:
        return None
    q = control_text_contains.casefold()
    for c in child_controls(top):
        if q in (c["text"] or "").casefold():
            c["top_hwnd"] = top
            return c
    return None


def click_control(control: dict) -> bool:
    """BM_CLICK for classic button controls — no coordinates used."""
    if not IS_WINDOWS:
        return False
    BM_CLICK = 0x00F5
    try:
        _user32.SendMessageW(wt.HWND(control["hwnd"]), BM_CLICK, 0, 0)
        return True
    except Exception:
        return False


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not IS_WINDOWS:
        return None
    rect = wt.RECT()
    if not _user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)
