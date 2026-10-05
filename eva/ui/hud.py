"""HUD — an eDEX-UI style full-screen dashboard for EVA's CLI.

Layout mirrors the eDEX-UI three-column dashboard, in EVA's vocabulary:
  left  : SYSTEM (block-digit clock · date/uptime/type/power · host/machine/py) ·
          CPU USAGE (grouped core sparklines + temp/freq/tasks) ·
          MEMORY (block matrix + swap) · TOP PROCESSES · FILESYSTEM ·
          EXIT DISPLAY button
  mid   : EVA STATUS (state/mood/actor/jobs · headline · providers · counters) ·
          AVATAR (presence orb drawn from the live state) ·
          ACTIVITY (live bus events with the actor's reasons)
  main  : buffer tab strip SESSION / STEPS / STATE · numbered feed ·
          status line (mode · buffer · cpu · clock) · input bar

Run:   python eva_cli.py --hud      (type a goal or /help; Esc on an empty
                                     line exits, Tab switches buffer)

The model + render layer is pure (sources are injectable), so the UI is testable
without a display; only the key-read loop touches the console (msvcrt).
"""
from __future__ import annotations

import asyncio
import platform
import sys
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

try:
    import psutil
except ImportError:                     # degrade honestly, never crash the UI
    psutil = None                       # type: ignore

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..avatar.presence import presence_path, read_presence

TABS = ("SESSION", "STEPS", "STATE")
_BLOCKS = " ▁▂▃▄▅▆▇█"


# ------------------------------------------------------------------ pure: bars
def sparkline(values: list[float], width: int = 24) -> str:
    """0..100-ish series -> block chars (empty series -> empty string)."""
    if not values:
        return ""
    vals = [max(0.0, float(v)) for v in values][-width:]
    hi = max(max(vals), 1.0)
    return "".join(_BLOCKS[min(len(_BLOCKS) - 1, int(v / hi * (len(_BLOCKS) - 1)))]
                   for v in vals)


def bar(frac: float, width: int = 20) -> str:
    """0..1 -> a filled/empty bar (never raises)."""
    frac = max(0.0, min(1.0, float(frac)))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


# --------------------------------------------------- pure: eDEX-style pieces
_BIG = {
    "0": ("███", "█ █", "█ █", "█ █", "███"),
    "1": ("  █", " ██", "  █", "  █", "███"),
    "2": ("███", "  █", "███", "█  ", "███"),
    "3": ("███", "  █", "███", "  █", "███"),
    "4": ("█ █", "█ █", "███", "  █", "  █"),
    "5": ("███", "█  ", "███", "  █", "███"),
    "6": ("███", "█  ", "███", "█ █", "███"),
    "7": ("███", "  █", "  █", "  █", "  █"),
    "8": ("███", "█ █", "███", "█ █", "███"),
    "9": ("███", "█ █", "███", "  █", "███"),
    ":": (" ", "█", " ", "█", " "),
    " ": ("  ", "  ", "  ", "  ", "  "),
}


def big_clock(clock: str) -> list[str]:
    """5-row block-digit clock (eDEX's giant clock) — total for any input."""
    glyphs = [_BIG.get(ch, _BIG[" "]) for ch in str(clock)] or [_BIG[" "]]
    return [" ".join(g[row] for g in glyphs) for row in range(5)]


def matrix(frac: float, cols: int = 24, rows: int = 4) -> list[str]:
    """0..1 -> rows of filled/empty cells (the MEMORY block display)."""
    frac = max(0.0, min(1.0, float(frac)))
    total = max(1, cols * rows)
    filled = int(round(frac * total))
    return ["■" * max(0, min(cols, filled - i * cols))
            + "·" * max(0, cols - max(0, min(cols, filled - i * cols)))
            for i in range(rows)]


def cpu_groups(core_hist: list, cores: int) -> list[tuple[str, list[float]]]:
    """Per-core history -> [(\"# lo - hi\", group-average series)] (two halves)."""
    if not core_hist or cores < 2:
        return []
    half = cores // 2
    out = []
    for lo, hi in ((0, half), (half, cores)):
        series = [sum(chunk) / len(chunk)
                  for chunk in ([float(x) for x in sample[lo:hi]] for sample in core_hist)
                  if chunk]
        if series:
            out.append((f"# {lo + 1} - {hi}", series))
    return out


_ORB_FACES = {
    "SLEEP": ("-", "-----"),
    "IDLE": ("o", "\\___/"),
    "THINKING": ("^", "_______"),
    "WORKING": ("0", "======= "),
    "SUCCESS": ("^", "\\___/"),
    "FAIL": ("x", "/   \\"),
    "WAITING": ("O", "  o  "),
}


def ascii_orb(state: str, stale: bool = False) -> list[str]:
    """A 5-line presence orb for the AVATAR panel — pure, total, state-driven."""
    key = "SLEEP" if (stale or not state or state == "NONE") else state
    eye, mouth = _ORB_FACES.get(key, _ORB_FACES["IDLE"])
    return [
        "  .-~~~~~~~~~-.",
        f" / {eye}{' ' * 8}{eye} \\",
        f" | {mouth:^10} |",
        "  \\ " + f"{'-----':^7}" + " /  ",
        "   '-........-'",
    ]


def mode_label(buffer: str) -> str:
    """The vim-ish mode line under the buffer (pure)."""
    text = buffer.strip()
    if not text:
        return "-- NORMAL --"
    return "-- COMMAND --" if text.startswith("/") else "-- GOAL --"


def tab_bar(active: int) -> Text:
    """The buffer tab strip across the top of the main column."""
    out = Text()
    for i, name in enumerate(TABS):
        if i:
            out.append("  /  ", style="dim")
        out.append(f" {name} ", style="bold cyan" if i == active else "dim")
    out.append("    buffers", style="dim")
    return out


# ------------------------------------------------------------- pure: input keys
def classify_input(text: str) -> tuple[str, str]:
    """One typed line -> ("command" | "goal" | "empty", payload)."""
    text = text.strip()
    if not text:
        return "empty", ""
    if text.startswith("/"):
        return "command", text
    return "goal", text


def apply_key(buffer: str, key: str) -> tuple[str, str | None, bool]:
    """(buffer, key) -> (new_buffer, submitted_line, quit). Pure line editor:
    printable appends, Backspace deletes, Enter submits, Esc clears/quits,
    Ctrl+C quits."""
    if key in ("\r", "\n"):
        return "", buffer, False
    if key == "\b":
        return buffer[:-1], None, False
    if key == "\x1b":                    # Esc: drop what was typed, else leave
        return "", None, not buffer
    if key in ("\x03",):                 # Ctrl+C
        return buffer, None, True
    if key in ("\t",):                   # Tab is handled by the caller (tab switch)
        return buffer, None, False
    if key and key.isprintable() and len(buffer) < 240:
        return buffer + key, None, False
    return buffer, None, False


# ----------------------------------------------------------- pure: event feed
def feed_line(event: Any) -> tuple[str, str] | None:
    """Bus event -> (bucket, line) for the buffers, or None if uninteresting.
    buckets: session (talk/verdicts) · steps (actions + reasons) · state (system)."""
    kind = getattr(event, "type", "")
    p = dict(getattr(event, "payload", None) or {})
    stamp = time.strftime("%H:%M:%S")
    if kind == "TASK_STARTED":
        return "session", f"{stamp} ▸ goal: {p.get('goal', '')}"
    if kind == "ACTOR_ACTION":
        return "steps", f"{stamp} ◆ {p.get('kind', 'tool')} {p.get('action', '')} — {p.get('reason', '')}"
    if kind == "TOOL_START":
        return "steps", f"{stamp} → {p.get('tool')} (step {p.get('step')})"
    if kind == "TASK_STEP_RESULT":
        icon = {"VERIFIED": "✓", "FAILED": "✗", "BLOCKED": "⏸",
                "SKIPPED_ALREADY_DONE": "↷"}.get(p.get("status", ""), "·")
        detail = p.get("reason") or p.get("effect") or p.get("observed") or ""
        return "steps", f"{stamp} {icon} {p.get('status', '')} {detail}"
    if kind == "TASK_FINISHED":
        return "session", f"{stamp} ✦ FINISHED — {p.get('message', '')}"
    if kind == "TASK_FAILED":
        return "session", f"{stamp} ✗ FAILED — {p.get('reason', p.get('message', ''))}"
    if kind == "TASK_WAITING_USER":
        return "session", f"{stamp} ⏸ waiting: {p.get('ask', '')}"
    if kind == "RULE_ENFORCED":
        return "session", f"{stamp} ⛔ rule forbids {p.get('tool', '')}: {p.get('instruction', '')}"
    if kind == "RULE_REMEMBERED":
        return "session", f"{stamp} ✦ remembered: {p.get('instruction', '')}"
    if kind == "TASK_ESCALATED":
        return "steps", f"{stamp} ↗ escalating to the actor loop (was {p.get('from', '')})"
    if kind in ("TASK_PLAN_READY", "MODEL_ROUTED", "MODE_CHANGED", "TASK_REPLAN"):
        return "state", f"{stamp} {kind}: {p.get('reason') or p.get('mode') or ''}".rstrip()
    if kind in ("RISK_GATE", "RISK_ANSWER"):
        return "session", f"{stamp} ⚠ {kind}: {p.get('action') or p.get('title') or ''}".rstrip()
    return None


# --------------------------------------------------------------- snapshots
def _fmt_uptime(seconds: float) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


class _TopProcs:
    """Keeps psutil.Process handles so cpu_percent is a real delta (first sample
    shows 0.0, later ones are honest), ordered by cpu/mem impact."""

    def __init__(self):
        self._cache: dict[int, Any] = {}

    def sample(self, n: int = 7) -> list[dict]:
        if psutil is None:
            return []
        rows: list[dict] = []
        for proc in psutil.process_iter(["pid", "name", "memory_percent"]):
            try:
                pid = proc.info["pid"]
                cached = self._cache.get(pid)
                if cached is None:
                    cached = psutil.Process(pid)
                    self._cache[pid] = cached
                rows.append({"pid": pid, "name": str(proc.info["name"] or "?")[:16],
                             "cpu": float(cached.cpu_percent(None)),
                             "mem": float(proc.info["memory_percent"] or 0.0)})
            except Exception:            # process vanished / access denied
                continue
        rows.sort(key=lambda r: (-max(r["cpu"], r["mem"]), -r["cpu"]))
        return rows[:n]


_TOP = _TopProcs()


def _system_snapshot() -> dict:
    snap = {"clock": time.strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%Y %b %d").upper(),
            "host_uptime": "n/a", "platform": platform.system().lower() or "unknown",
            "host": platform.node() or "?",
            "machine": platform.machine() or "?", "py": platform.python_version() or "?",
            "cpu": 0.0, "cores": 0, "core_pct": [], "cpu_model": "",
            "temp": "n/a", "freq_min": 0.0, "freq_max": 0.0, "tasks": 0,
            "mem_used": 0, "mem_total": 0, "swap_used": 0, "swap_total": 0,
            "disk_used": 0, "disk_total": 0, "partitions": [],
            "battery": "n/a", "top": []}
    if psutil is None:
        return snap
    try:
        snap["cpu"] = float(psutil.cpu_percent(None))
        snap["cores"] = int(psutil.cpu_count(logical=True) or 0)
        try:
            snap["core_pct"] = [float(x) for x in psutil.cpu_percent(None, True)]
        except Exception:
            pass
        snap["host_uptime"] = _fmt_uptime(time.time() - psutil.boot_time())
        vm = psutil.virtual_memory()
        snap["mem_used"], snap["mem_total"] = vm.used, vm.total
        try:
            sw = psutil.swap_memory()
            snap["swap_used"], snap["swap_total"] = sw.used, sw.total
        except Exception:
            pass
        dk = psutil.disk_usage("/")
        snap["disk_used"], snap["disk_total"] = dk.used, dk.total
        try:
            parts = []
            for p in psutil.disk_partitions(all=False)[:4]:
                u = psutil.disk_usage(p.mountpoint)
                parts.append({"device": p.device, "mount": p.mountpoint,
                              "fstype": p.fstype,
                              "frac": (u.used / u.total) if u.total else 0.0})
            snap["partitions"] = parts
        except Exception:
            pass
        try:
            freq = psutil.cpu_freq()
            if freq is not None:
                snap["freq_min"] = float(freq.min or 0.0)
                snap["freq_max"] = float(freq.max or 0.0)
                if freq.max:
                    snap["cpu_model"] = f"{freq.max / 1000:.2f} GHz max"
        except Exception:
            pass
        try:                      # Linux only — never crash the UI without it
            temps = getattr(psutil, "sensors_temperatures", None)
            if callable(temps):
                for entries in temps().values():
                    for e in entries:
                        if getattr(e, "current", None):
                            snap["temp"] = f"{e.current:.0f}°C"
                            raise StopIteration
        except StopIteration:
            pass
        except Exception:
            pass
        try:
            snap["tasks"] = len(psutil.pids())
        except Exception:
            pass
        bat = psutil.sensors_battery()
        if bat is not None:
            snap["battery"] = f"{int(bat.percent)}%" + (" AC" if bat.power_plugged else "")
    except Exception:
        pass
    snap["top"] = _TOP.sample()
    return snap


def _eva_snapshot(app, presence_file) -> dict:
    out = {"presence": read_presence(presence_file), "providers": [], "tools": 0,
           "rules": 0, "skills": 0, "episodes": 0, "jobs": 0, "actor": False}
    if app is None:
        return out
    try:
        perf = app.perf()
        out.update(tools=int(perf.get("tools") or 0), rules=int(perf.get("rules") or 0),
                   skills=int(perf.get("skills") or 0), jobs=int(perf.get("jobs") or 0),
                   actor=bool(perf.get("actor")),
                   providers=[{"name": str(p.get("name")), "alive": bool(p.get("alive"))}
                              for p in (perf.get("providers") or [])])
    except Exception:
        pass
    try:
        out["episodes"] = len(app.memory.recent_episodes(100))
    except Exception:
        pass
    return out


class HudModel:
    """Pulls snapshots from injectable sources and keeps the buffer history."""

    def __init__(self, presence_file=None, system: Callable[[], dict] | None = None,
                 eva: Callable[[], dict] | None = None, eva_app=None):
        self.presence_file = presence_file
        self._system = system or _system_snapshot
        self._eva = eva or (lambda: _eva_snapshot(eva_app, presence_file))
        self.cpu_hist: deque[float] = deque(maxlen=40)
        self.core_hist: deque[list[float]] = deque(maxlen=40)
        self.event_hist: deque[int] = deque(maxlen=60)
        self._last_events = 0
        self.session: deque[dict] = deque(maxlen=400)   # {"bucket", "line"}
        self.started = time.time()
        self.tab = 0
        self.input_buffer = ""
        self.busy = False
        self.data: dict[str, Any] = {"system": {}, "eva": {}}
        self._eva_at = 0.0

    def refresh(self, force: bool = False) -> None:
        system = self._system()
        self.cpu_hist.append(float(system.get("cpu") or 0.0))
        core_pct = system.get("core_pct") or []
        if core_pct:
            self.core_hist.append([float(x) for x in core_pct])
        events = len(self.session)
        self.event_hist.append(max(0, events - self._last_events))
        self._last_events = events
        if force or time.time() - self._eva_at > 1.0:      # app introspection is slower
            self.data["eva"] = self._eva()
            self._eva_at = time.time()
        self.data["system"] = system

    def feed(self, event) -> None:
        try:
            got = feed_line(event)
        except Exception:
            return                     # a feed hiccup may never break the loop
        if got:
            bucket, line = got
            self.session.append({"bucket": bucket, "line": line})

    def say(self, text: str, bucket: str = "session") -> None:
        for line in str(text).splitlines():
            if line.strip():
                self.session.append({"bucket": bucket, "line": line})

    def lines(self) -> list[str]:
        bucket = TABS[self.tab].casefold()
        return [r["line"] for r in self.session if r["bucket"] == bucket]


# ---------------------------------------------------------------- rendering
_STATE_COLORS = {"SUCCESS": "green", "FAIL": "red", "WAITING": "yellow",
                 "WORKING": "cyan", "THINKING": "magenta", "IDLE": "blue",
                 "NONE": "dim"}


def _gi(size: int) -> str:
    return f"{size / (1024 ** 3):.1f} GiB"


def _ghz(mhz: float) -> str:
    mhz = float(mhz or 0.0)
    if mhz >= 1000:
        return f"{mhz / 1000:.2f}GHz"
    return f"{mhz:.0f}MHz" if mhz else "n/a"


def render(model: HudModel) -> Layout:
    """Build the full-screen layout from the model (pure: no I/O here)."""
    sysd = model.data.get("system") or {}
    evad = model.data.get("eva") or {}
    presence = evad.get("presence") or {}

    root = Layout(name="root")
    root.split_row(Layout(name="left", size=36), Layout(name="mid", size=34),
                   Layout(name="main"))
    root["left"].split_column(Layout(name="l_sys", size=9), Layout(name="l_cpu", size=9),
                              Layout(name="l_mem", size=7), Layout(name="l_top"),
                              Layout(name="l_fs", size=6), Layout(name="l_exit", size=3))
    root["mid"].split_column(Layout(name="m_status", size=11), Layout(name="m_orb", size=9),
                             Layout(name="m_act"))
    root["main"].split_column(Layout(name="tabs", size=1), Layout(name="feed"),
                               Layout(name="status", size=1), Layout(name="input", size=3))

    # -- left: SYSTEM -------------------------------------------------------
    sys_txt = Text(no_wrap=True)
    for row in big_clock(str(sysd.get("clock") or "--:--:--")):
        sys_txt.append(row + "\n", style="bold cyan")
    sys_txt.append(f"{sysd.get('date', '')}  UP {sysd.get('host_uptime', 'n/a')}  "
                   f"{sysd.get('platform', '?')}\n", style="dim")
    sys_txt.append(f"{sysd.get('host', '?')} · {sysd.get('machine', '?')}", style="dim")
    root["l_sys"].update(Panel(
        sys_txt, title="SYSTEM",
        subtitle=f"power {sysd.get('battery', 'n/a')} · py {sysd.get('py', '?')}",
        border_style="cyan"))

    # -- left: CPU USAGE (grouped sparklines + temp/freq/tasks) -------------
    cpu = float(sysd.get("cpu") or 0.0)
    cpu_txt = Text(no_wrap=True)
    cpu_txt.append("CPU USAGE  ", style="bold white")
    cpu_txt.append(f"{cpu:5.1f}%", style="green")
    if sysd.get("cpu_model"):
        cpu_txt.append(f"  {sysd.get('cpu_model')}", style="dim")
    cpu_txt.append("\n")
    groups = cpu_groups(list(model.core_hist), int(sysd.get("cores") or 0))
    if not groups:
        groups = [("# all cores", list(model.cpu_hist))]
    for label, series in groups[:2]:
        avg = series[-1] if series else 0.0
        cpu_txt.append(f"{label}   Avg {avg:.0f}%\n", style="white")
        cpu_txt.append(f"{sparkline(series, width=28)}\n", style="green")
    cpu_txt.append("TEMP    MIN      MAX     TASKS\n", style="dim")
    cpu_txt.append(f"{str(sysd.get('temp') or 'n/a'):<8}"
                   f"{_ghz(sysd.get('freq_min')):<9}"
                   f"{_ghz(sysd.get('freq_max')):<9}"
                   f"{int(sysd.get('tasks') or 0)}", style="white")
    root["l_cpu"].update(Panel(cpu_txt, title="CPU USAGE", border_style="green"))

    # -- left: MEMORY (block matrix) + swap ---------------------------------
    mem_total = int(sysd.get("mem_total") or 0)
    mem_frac = (sysd.get("mem_used") or 0) / mem_total if mem_total else 0.0
    swap_total = int(sysd.get("swap_total") or 0)
    swap_frac = (sysd.get("swap_used") or 0) / swap_total if swap_total else 0.0
    mem_txt = Text(no_wrap=True)
    mem_txt.append(f"USING {_gi(sysd.get('mem_used') or 0)} OUT OF {_gi(mem_total)}\n",
                   style="dim")
    for row in matrix(mem_frac, 32, 3):
        lit = row.count("■")
        mem_txt.append(row[:lit], style="green")
        mem_txt.append(row[lit:] + "\n", style="dim green")
    mem_txt.append(f"SWAP {bar(swap_frac, 14)} {_gi(sysd.get('swap_used') or 0)}",
                   style="dim")
    root["l_mem"].update(Panel(mem_txt, title="MEMORY", border_style="yellow"))

    procs = Table(show_header=True, header_style="bold cyan", expand=True,
                  pad_edge=False, box=None)
    for col in ("PID", "NAME", "CPU", "MEM"):
        procs.add_column(col, overflow="ellipsis")
    for row in (sysd.get("top") or []):
        procs.add_row(str(row["pid"]), str(row["name"]), f"{row['cpu']:.1f}",
                      f"{row['mem']:.1f}")
    root["l_top"].update(Panel(procs, title="TOP PROCESSES", border_style="cyan"))

    # -- left: FILESYSTEM + exit button -------------------------------------
    disk_total = int(sysd.get("disk_total") or 0)
    disk_frac = (sysd.get("disk_used") or 0) / disk_total if disk_total else 0.0
    fs_txt = Text(no_wrap=True)
    fs_txt.append(f"DISK  {bar(disk_frac, 12)}  {disk_frac * 100:3.0f}%\n",
                  style="magenta")
    for p in (sysd.get("partitions") or [])[:2]:
        frac = float(p.get("frac") or 0.0)
        mount = str(p.get("mount") or "?")[:8]
        fs_txt.append(f"{mount:<8}{str(p.get('fstype') or ''):<6}"
                      f"{bar(frac, 11)} {frac * 100:3.0f}%\n", style="white")
    fs_txt.append("showing available block devices", style="dim")
    root["l_fs"].update(Panel(fs_txt, title="FILESYSTEM", border_style="magenta"))

    root["l_exit"].update(Panel(
        Text("EXIT DISPLAY  ·  esc quits", justify="center", style="bold"),
        border_style="white", padding=(0, 0)))

    # -- mid: EVA STATUS -----------------------------------------------------
    state = str(presence.get("state") or "NONE")
    state_style = _STATE_COLORS.get(state, "white")
    stale = bool(presence.get("stale"))
    stat = Text(no_wrap=True)
    stat.append("STATE      MOOD     ACTOR\n", style="dim")
    stat.append(f"{state:<10} {str(presence.get('mood') or '-'):<9}"
                f"{'ON' if evad.get('actor') else 'off'}\n",
                style=f"bold {state_style}")
    if stale:
        stat.append("stale — presence quiet\n", style="dim")
    stat.append(str(presence.get("headline") or "ready") + "\n", style="bold white")
    detail = str(presence.get("detail") or "")
    if detail:
        stat.append(detail[:30] + "\n", style="dim")
    provs = evad.get("providers") or []
    prov_line = " · ".join(f"{p['name']} {'✓' if p['alive'] else '✗'}" for p in provs)
    stat.append(f"providers: {prov_line or 'none (offline)'}\n", style="dim")
    stat.append(f"tools {evad.get('tools', 0)} · rules {evad.get('rules', 0)}"
                f" · skills {evad.get('skills', 0)}\n", style="dim")
    stat.append(f"episodes {evad.get('episodes', 0)} · jobs {evad.get('jobs', 0)}",
                style="dim")
    root["m_status"].update(Panel(stat, title="EVA STATUS", border_style="magenta"))

    # -- mid: AVATAR (presence orb) -----------------------------------------
    orb = Text()
    for line in ascii_orb(state, stale):
        pad = max(0, (30 - len(line)) // 2)
        orb.append(" " * pad + line + "\n", style=f"bold {state_style}")
    orb.append(f"● {state}", style=f"bold {state_style}")
    mood = str(presence.get("mood") or "")
    if mood:
        orb.append(f" · {mood}", style="dim")
    orb.append("\n")
    if stale:
        orb.append("EVA is sleeping", style="dim")
    else:
        orb.append(str(presence.get("headline") or "ready")[:30], style="white")
    root["m_orb"].update(Panel(orb, title="AVATAR", border_style="magenta"))

    # -- mid: ACTIVITY -------------------------------------------------------
    rate = sparkline(list(model.event_hist), width=20)
    last = model.event_hist[-1] if model.event_hist else 0
    act = Text()
    act.append(f"RATE {rate}  {last}/tick\n", style="dim")
    activity = [r["line"] for r in list(model.session)[-16:]]
    if activity:
        for line in activity:
            act.append(line + "\n", style="white")
    else:
        act.append("(events appear here)", style="dim")
    root["m_act"].update(Panel(act, title="ACTIVITY", border_style="cyan"))

    # -- main: tab strip · buffer · status · input --------------------------
    root["tabs"].update(tab_bar(model.tab))

    feed_lines = model.lines()[-40:]
    feed_txt = Text()
    if feed_lines:
        for i, line in enumerate(feed_lines, 1):
            feed_txt.append(f"{i:>3} ", style="dim")
            feed_txt.append("│ ", style="blue")
            feed_txt.append(line + "\n", style="white")
    else:
        feed_txt.append("(buffer empty — type below)", style="dim")
    busy = " · WORKING" if model.busy else ""
    root["feed"].update(Panel(feed_txt, title=f"{TABS[model.tab]}{busy}",
                              border_style="yellow" if model.busy else "green"))

    sgrid = Table.grid(expand=True)
    sgrid.box = None
    sgrid.add_column()
    sgrid.add_column(justify="right")
    sleft = Text()
    sleft.append(f" {mode_label(model.input_buffer)} ", style="bold black on white")
    sleft.append(f"{TABS[model.tab]} · {len(model.lines())} lines  ", style="dim")
    sgrid.add_row(sleft, Text(f"CPU {cpu:5.1f}%   {sysd.get('clock', '')}   ",
                              style="dim"))
    root["status"].update(sgrid)

    typed = model.input_buffer or ""
    prompt = Text((typed or "") + "▌", style="white" if typed else "dim")
    root["input"].update(Panel(
        prompt, title=f"EVA · {TABS[model.tab]} · enter runs · tab switches · esc exits",
        border_style="cyan"))
    return root


# ---------------------------------------------------------------- input loop
def _safe_flavor(app, text: str) -> str:
    try:
        return str(app.persona.flavor(str(text)))
    except Exception:
        return str(text)


async def run_hud(app) -> int:
    """Full-screen loop: ~6 fps refresh, non-blocking keys (msvcrt), goals run as
    a background task so the feed keeps moving while EVA works."""
    try:
        import msvcrt
    except ImportError:                 # non-Windows console: honest fallback
        from rich.console import Console
        Console().print("[yellow]HUD input needs a Windows console — "
                        "dropping back to the plain CLI.[/]")
        from .edex import EdexCLI
        return await EdexCLI(app).run()

    # block glyphs must survive a piped/legacy console (cp1252) without raising
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    from rich.console import Console
    from rich.live import Live

    from .edex import EdexCLI

    cli = EdexCLI(app, quiet=True)      # command dispatch without event spam
    app.orchestrator.confirm = None     # risk prompts would block the key loop:
                                        # HIGH/CRITICAL auto-decline here (fail-safe)
    recorder = Console(record=True, width=100, no_color=True)
    cli.console = recorder
    model = HudModel(presence_file=presence_path(app.settings), eva_app=app)
    app.bus.subscribe(model.feed)
    model.refresh(force=True)
    model.say("EVA HUD — type a goal, or /help · tab switches buffers", "session")
    if psutil is None:
        model.say("psutil missing: system panels are degraded", "state")

    pending: asyncio.Task | None = None
    running = True

    def report(outcome) -> None:
        try:
            result, reply = outcome if isinstance(outcome, tuple) else (None, outcome)
        except Exception as exc:
            model.say(f"error: {type(exc).__name__}: {exc}")
            return
        if reply:
            model.say(_safe_flavor(app, reply))
        if result is not None:
            model.say(f"{result.status.value} — {result.message}")
            for s in result.task.plan:
                why = f" · {s.intent}" if s.intent else ""
                model.say(f"  {s.n}. {s.tool} [{s.status.value}]{why}")
        if app.pending_ask is not None:
            opts = app.pending_ask.get("options") or []
            hint = "  choices: " + " / ".join(opts) if opts else ""
            model.say(f"waiting on you → type the answer{hint}")

    async def submit(text: str) -> None:
        nonlocal pending, running
        kind, payload = classify_input(text)
        if kind == "empty":
            return
        if kind == "command":
            if payload in ("/exit", "/quit"):
                running = False
                return
            recorder.clear()
            try:
                await cli.command(payload)
            except Exception as exc:
                model.say(f"command failed: {type(exc).__name__}: {exc}")
            out = recorder.export_text().strip()
            if out:
                model.say(out)
            return
        if pending is not None:
            model.say("busy — one run at a time (Esc twice to exit)", "state")
            return
        model.say(f"❯ {payload}")
        if app.pending_ask is not None:
            pending = asyncio.create_task(app.answer_pending(payload))
        else:
            pending = asyncio.create_task(app.handle(payload))

    await app.start_services()
    print("EVA HUD online — esc exits, tab switches buffers", flush=True)
    try:
        with Live(render(model), screen=True, auto_refresh=True,
                  refresh_per_second=6) as live:
            while running:
                while msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if key in ("\x00", "\xe0"):     # extended key: drop its prefix
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    if key == "\t":
                        model.tab = (model.tab + 1) % len(TABS)
                        continue
                    new_buffer, submitted, quit = apply_key(model.input_buffer, key)
                    model.input_buffer = new_buffer
                    if quit:
                        running = False
                        break
                    if submitted is not None:
                        await submit(submitted)
                if pending is not None and pending.done():
                    task = pending
                    pending = None
                    try:
                        report(task.result())
                    except Exception as exc:
                        model.say(f"error: {type(exc).__name__}: {exc}")
                model.busy = pending is not None
                model.refresh()
                live.update(render(model))
                await asyncio.sleep(0.15)
    finally:
        if pending is not None:
            try:
                await pending
            except Exception:
                pass
        await app.close()
    return 0
