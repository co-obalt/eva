"""L1 — OS APIs (doc 04 ladder): processes, windows, foreground, resources.

Cost: ~0. Windows first; everything degrades gracefully on other OSes.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import platform
import time
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    def _window_text(hwnd: int) -> str:
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        _user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value

    def _window_class(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _window_pid(hwnd: int) -> int:
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _process_name(pid: int) -> str:
        if psutil is None:
            return ""
        try:
            return psutil.Process(pid).name()
        except Exception:
            return ""


def foreground_window() -> dict[str, Any]:
    """(title, pid, process) of the foreground window — L1."""
    if not IS_WINDOWS:
        return {"title": None, "pid": None, "process": None}
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return {"title": None, "pid": None, "process": None}
    pid = _window_pid(hwnd)
    return {"title": _window_text(hwnd), "pid": pid, "process": _process_name(pid),
            "hwnd": int(hwnd)}


def list_windows(limit: int = 80) -> list[dict[str, Any]]:
    """Visible top-level windows — L1."""
    if not IS_WINDOWS:
        return []
    out: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if len(out) >= limit:
            return False
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if not title.strip():
            return True
        pid = _window_pid(hwnd)
        out.append({"title": title, "pid": pid, "process": _process_name(pid),
                    "class": _window_class(hwnd), "hwnd": int(hwnd)})
        return True

    _user32.EnumWindows(_cb, 0)
    return out


def find_window(query: str) -> dict[str, Any] | None:
    """First visible window whose title or process matches the query (case-insensitive)."""
    q = query.casefold()
    for w in list_windows():
        if q in (w["title"] or "").casefold() or q in (w["process"] or "").casefold():
            return w
    return None


def focus_window(query: str) -> dict[str, Any] | None:
    """Bring a window to the foreground (restore if minimized) — L1 action."""
    w = find_window(query)
    if w is None:
        return None
    hwnd = w["hwnd"]
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    _user32.SetForegroundWindow(hwnd)
    return w


def process_running(names: list[str]) -> bool:
    """Any process whose executable name matches (case-insensitive, .exe optional)."""
    if psutil is None:
        return False
    wanted = {n.casefold().removesuffix(".exe") for n in names}
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").casefold().removesuffix(".exe")
            if name in wanted:
                return True
        except Exception:
            continue
    return False


def process_pids(names: list[str]) -> list[int]:
    if psutil is None:
        return []
    wanted = {n.casefold().removesuffix(".exe") for n in names}
    pids: list[int] = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").casefold().removesuffix(".exe") in wanted:
                pids.append(p.pid)
        except Exception:
            continue
    return pids


def process_cmdlines(names: list[str], limit: int = 20) -> list[list[str]]:
    """Command lines of matching processes — used to detect --profile-directory."""
    if psutil is None:
        return []
    wanted = {n.casefold().removesuffix(".exe") for n in names}
    out: list[list[str]] = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").casefold().removesuffix(".exe") in wanted:
                out.append(list(p.info["cmdline"] or []))
                if len(out) >= limit:
                    break
        except Exception:
            continue
    return out


if psutil is not None:
    psutil.cpu_percent()  # seed the interval counter at import time


def resources() -> dict[str, Any]:
    """CPU / RAM / disk snapshot — cheap, sampled on demand (L1)."""
    if psutil is None:
        return {"cpu_percent": None, "ram_total_mb": None, "ram_free_mb": None,
                "disk_free_gb": None}
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("C:\\" if IS_WINDOWS else "/")
        free_gb = round(disk.free / (1024 ** 3), 1)
    except Exception:
        free_gb = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_total_mb": round(vm.total / (1024 ** 2)),
        "ram_free_mb": round(vm.available / (1024 ** 2)),
        "ram_used_percent": vm.percent,
        "disk_free_gb": free_gb,
    }


def machine() -> dict[str, Any]:
    return {"os": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "hostname": platform.node(),
            "python": platform.python_version()}


def now() -> float:
    return time.time()
