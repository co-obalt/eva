"""Perception engine (doc 04): providers per branch, observation cache with
invalidate-on-action, and polling-until-deadline verification.

The engine answers TRUE/FALSE/UNKNOWN with provenance (level + timestamp +
confidence). It NEVER mutates the world and NEVER guesses.
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from pathlib import Path
from typing import Any

from ..core.events import OBSERVATION
from ..core.state import Observation, ObservationRequest, Predicate, Truth, request_key
from . import l1_system as l1
from . import l2_a11y as l2
from . import l3_dom as dom
from . import l4_dom as l4
from .l3_cdp import (CdpManager, chrome_binary, chrome_profiles, match_profile,
                     running_profile_dir)
from .l5_vision import VisionGateway, capture_screen, ocr_available
from .policy import PolicyGate

TTL_BY_BRANCH = {
    "system": 2.0, "chrome": 2.0, "page": 2.0, "dom": 2.0, "wa": 2.0, "apps": 3.0,
    "files": 10.0, "downloads": 10.0, "terminal": 60.0, "comms": 5.0, "ui": 5.0,
    "rules": 30.0,
}

def _proc_exes(name: str) -> list[str] | None:
    """Process names for an app: this table first, then the app registry — so any
    app the registry knows (or data/apps.json adds) is observable without edits."""
    exes = APP_EXES.get(name)
    if exes is not None:
        return exes
    try:
        from ..apps.registry import find_app
        info = find_app(name)
        if info is not None and info.process:
            return list(info.process)
    except Exception:
        pass
    return None


APP_EXES = {
    "chrome": ["chrome"], "notepad": ["notepad"], "vscode": ["code", "Code"],
    "code": ["code"], "explorer": ["explorer"], "cmd": ["cmd"],
    "terminal": ["windowsterminal", "WindowsTerminal", "wt", "cmd"],
    "calculator": ["Calculator", "CalculatorApp", "calc"], "calc": ["Calculator", "CalculatorApp"],
    "spotify": ["Spotify"], "word": ["WINWORD"], "excel": ["EXCEL"],
    "powerpoint": ["POWERPNT"], "whatsapp": ["WhatsApp"], "edge": ["msedge"],
    "firefox": ["firefox"], "vlc": ["vlc"], "telegram": ["Telegram"], "discord": ["Discord"],
    "antigravity": ["antigravity", "Antigravity"], "freebuff": ["freebuff"],
}

DOWNLOADS_DIR = Path.home() / "Downloads"


def _obs(path: str, args: dict, value: Any, level: str, note: str = "",
         ttl: float | None = None) -> Observation:
    return Observation(path=path, args=args, truth=Truth.TRUE, value=value, level=level,
                       note=note, ttl_s=ttl or TTL_BY_BRANCH.get(path.split(".")[0], 5.0))


def _unknown(path: str, args: dict, note: str) -> Observation:
    return Observation(path=path, args=args, truth=Truth.UNKNOWN, value=None, level="L0", note=note)


class PerceptionEngine:
    def __init__(self, settings, bus=None, policy: PolicyGate | None = None, memory=None):
        self.settings = settings
        self.bus = bus
        self.policy = policy or PolicyGate(settings.allow_vision)
        self.memory = memory           # durable rules (read-only for observations)
        self.cache: dict[str, Observation] = {}
        self.task: Any = None
        self.cdp = CdpManager(settings)
        self.vision = VisionGateway(settings, self.policy)
        self.memo: dict[str, Any] = {}      # L0 internal state (what *we* just did)
        self._generation = 0

    # ------------------------------------------------------------- task binding
    def bind_task(self, task: Any) -> None:
        self.task = task
        self.policy.bind_task(task)

    def note(self, path: str, value: Any, level: str = "L0", args: dict | None = None,
             note: str = "") -> Observation:
        """L0 write: internal state of the system itself (doc 04 ladder L0)."""
        obs = Observation(path=path, args=args or {}, truth=Truth.TRUE, value=value,
                          level=level, note=note, ttl_s=60.0)
        self.cache[obs.key] = obs
        self.memo[path] = value
        return obs

    # ------------------------------------------------------------------ observe
    async def observe(self, requests: list[ObservationRequest],
                      max_age: float = 5.0) -> dict[str, Observation]:
        out: dict[str, Observation] = {}
        for req in requests:
            key = req.key
            cached = self.cache.get(key)
            if cached is not None and cached.truth is not Truth.UNKNOWN and cached.fresh(max_age):
                out[key] = cached
                continue
            obs = await self._resolve(req)
            self.cache[key] = obs
            out[key] = obs
            if self.bus is not None and obs.truth is not Truth.UNKNOWN:
                self.bus.emit(OBSERVATION, task_id=self.task.id if self.task else None,
                              path=obs.path, level=obs.level,
                              value=obs.value if isinstance(obs.value, (str, int, float, bool, type(None))) else "<complex>")
        return out

    async def verify(self, predicates: list[Predicate],
                     timeout_s: float = 8.0) -> dict[str, Observation]:
        """Poll cheap levels until the declared predicates are TRUE or the
        deadline passes (doc 03 §5 rule 3 — until semantics, not fixed sleeps).
        L5+ is never polled here (INV-1)."""
        deadline = time.time() + max(0.5, timeout_s)
        last: dict[str, Observation] = {}
        while True:
            reqs = [ObservationRequest(path=p.path, args=p.args) for p in predicates]
            last = await self.observe(reqs, max_age=1.0)
            if all(p.matches_observation(last.get(p.key)) is Truth.TRUE for p in predicates):
                return last
            if time.time() >= deadline:
                return last
            await asyncio.sleep(0.35)

    # ---------------------------------------------------------------- invalidation
    def invalidate_for(self, predicates: list[Predicate]) -> None:
        """Invalidate only the branches an action's effects touch (no blanket reset)."""
        branches = {p.path.split(".")[0] for p in predicates}
        for key in list(self.cache):
            if key.split("|")[0].split(".")[0] in branches:
                del self.cache[key]
        self._generation += 1

    # ------------------------------------------------------------------ explain
    def explain(self, paths: list[str] | None = None) -> list[dict]:
        rows = []
        for obs in self.cache.values():
            if paths and not any(obs.path.startswith(p) for p in paths):
                continue
            rows.append({"path": obs.path, "truth": obs.truth.value, "value": obs.value,
                         "level": obs.level, "age_s": round(time.time() - obs.at, 1),
                         "confidence": obs.confidence, "note": obs.note})
        return sorted(rows, key=lambda r: r["path"])

    def state_view(self) -> dict[str, Any]:
        rules = None
        if self.memory is not None:
            try:
                rules = len(self.memory.list_rules())
            except Exception:
                rules = None
        return {"observations": len(self.cache), "generation": self._generation,
                "cdp_port": self.cdp.port, "vision_enabled": bool(self.settings.allow_vision),
                "ocr": ocr_available(), "rules": rules}

    # ---------------------------------------------------------------- resolve
    async def _resolve(self, req: ObservationRequest) -> Observation:
        branch = req.path.split(".")[0]
        method = getattr(self, f"_p_{branch}", None)
        if method is None:
            return _unknown(req.path, req.args, f"no provider for branch {branch!r}")
        try:
            return await method(req)
        except Exception as exc:  # a provider bug never crashes the loop
            return _unknown(req.path, req.args, f"provider error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ providers: L1
    async def _p_system(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        r = l1.resources()
        if p == "system.cpu_percent":
            return _obs(p, a, r.get("cpu_percent"), "L1")
        if p == "system.ram_free_mb":
            return _obs(p, a, r.get("ram_free_mb"), "L1")
        if p == "system.ram_used_percent":
            return _obs(p, a, r.get("ram_used_percent"), "L1")
        if p == "system.disk_free_gb":
            return _obs(p, a, r.get("disk_free_gb"), "L1")
        if p == "system.resources":
            return _obs(p, a, r, "L1")
        if p == "system.machine":
            return _obs(p, a, l1.machine(), "L1")
        if p == "system.windows":
            return _obs(p, a, [w["title"] for w in l1.list_windows()], "L1")
        if p == "system.window_count":
            return _obs(p, a, len(l1.list_windows()), "L1")
        if p == "system.foreground":
            return _obs(p, a, l1.foreground_window().get("title"), "L1")
        if p == "system.foreground_process":
            return _obs(p, a, l1.foreground_window().get("process"), "L1")
        if p == "system.reported":
            return _obs(p, a, bool(self.memo.get("system.reported")), "L0")
        return _unknown(p, a, f"unknown system path {p!r}")

    async def _p_chrome(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p == "chrome.running":
            return _obs(p, a, l1.process_running(["chrome"]), "L1")
        if p == "chrome.binary_found":
            return _obs(p, a, chrome_binary() is not None, "L1")
        if p == "chrome.debug_port":
            port = await self.cdp.discover()
            if port is None:
                return _obs(p, a, None, "L1", note="no Chrome listening on configured debug ports")
            return _obs(p, a, port, "L1", note=f"CDP on {port}")
        if p == "chrome.cdp_available":
            session = await self.cdp.ensure()
            return _obs(p, a, session is not None, "L1")
        if p == "chrome.profiles":
            return _obs(p, a, chrome_profiles(), "L3")
        if p == "chrome.profiles.matched":
            prof = match_profile(str(a.get("query", "")))
            if prof is None:
                return _obs(p, a, False, "L3", note=f"no profile matches {a.get('query')!r}")
            return _obs(p, a, True, "L3", note=prof["dir"])
        if p == "chrome.active_profile":
            running = running_profile_dir()
            if running:
                return _obs(p, a, running, "L3", note="from chrome process cmdline")
            profiles = chrome_profiles()
            if len(profiles) == 1:
                return _obs(p, a, profiles[0]["dir"], "L3", note="only one profile exists")
            return _unknown(p, a, "active profile not discoverable (cmdline has no --profile-directory)")
        if p == "chrome.active_tab_url":
            value = self.memo.get("chrome.active_tab_url")
            if value is None:
                return _unknown(p, a, "no tab activated in this session")
            return _obs(p, a, value, "L3", note="targets the tab we just activated")
        if p in ("chrome.tabs", "chrome.page_url"):
            tabs = await self.cdp.tabs()
            if tabs is None:
                return _unknown(p, a, "CDP unavailable (Chrome not started with --remote-debugging-port)")
            urls = [t.get("url") or "" for t in tabs]
            if p == "chrome.tabs":
                return _obs(p, a, urls, "L3")
            for u in urls:
                if not u.startswith("chrome://") and "devtools" not in u:
                    return _obs(p, a, u, "L3")
            return _obs(p, a, urls[0] if urls else None, "L3")
        if p == "chrome.has_tab":
            tabs = await self.cdp.tabs()
            if tabs is None:
                return _unknown(p, a, "CDP unavailable")
            want = str(a.get("match", "*"))
            urls = [t.get("url") or "" for t in tabs]
            hits = [u for u in urls if fnmatch.fnmatch(u.casefold(), want.casefold())]
            return _obs(p, a, bool(hits), "L3", note=(hits[0] if hits else f"no tab matches {want}"))
        return _unknown(p, a, f"unknown chrome path {p!r}")

    async def _p_page(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p in ("page.text", "page.text_contains"):
            res = await dom.page_text(self.cdp, max_chars=20000)
            if not res.get("ok"):
                return _unknown(p, a, str(res.get("error"))[:160])
            text = str(res.get("text") or "")
            if p == "page.text":
                return _obs(p, a, text[:4000], "L3", note=str(res.get("url") or ""))
            needle = str(a.get("text", ""))
            return _obs(p, a, needle.casefold() in text.casefold(), "L3",
                        note=f"page said: {text[:120]!r}")
        tabs = await self.cdp.tabs()
        if tabs is None:
            return _unknown(p, a, "CDP unavailable")
        for t in tabs:
            u = t.get("url") or ""
            if not u.startswith("chrome://") and "devtools" not in u:
                if p == "page.url":
                    return _obs(p, a, u, "L3")
                if p == "page.title":
                    return _obs(p, a, t.get("title"), "L3")
        return _unknown(p, a, "no page targets")

    # ------------------------------------------------------------ provider: L3 dom
    async def _p_dom(self, req: ObservationRequest) -> Observation:
        """Generic DOM observations: element existence / value (site-agnostic)."""
        p, a = req.path, req.args
        if p == "dom.last_action":
            value = self.memo.get("dom.last_action")
            if value is None:
                return _unknown(p, a, "no DOM action performed in this session")
            return _obs(p, a, value, "L0", note=str(value.get("kind") if isinstance(value, dict) else ""))
        if p in ("dom.match", "dom.value"):
            handle = str(a.get("handle") or "")
            if not handle:
                query = str(a.get("query") or "")
                if not query:
                    return _unknown(p, a, "needs a handle or a query")
                found = await dom.find(self.cdp, query, by=str(a.get("by") or "text"),
                                       url_contains=a.get("url_contains"), limit=1)
                if not found.get("ok"):
                    return _unknown(p, a, str(found.get("error"))[:160])
                matches = found.get("matches") or []
                if p == "dom.match":
                    return _obs(p, a, bool(matches), "L3",
                                note=f"{(matches[0]['label'] if matches else 'no match')}"[:120])
                if not matches:
                    return _obs(p, a, None, "L3", note=f"no element matched {query!r}")
                handle = matches[0]["handle"]
            state = await dom.state(self.cdp, handle, url_contains=a.get("url_contains"))
            if not state.get("ok"):
                return _unknown(p, a, str(state.get("error"))[:160])
            if p == "dom.match":
                return _obs(p, a, bool(state.get("exists")), "L3")
            if not state.get("exists"):
                return _obs(p, a, None, "L3", note="element no longer exists")
            return _obs(p, a, state.get("value"), "L3",
                        note=f"{state.get('tag')} {str(state.get('label'))[:60]}")
        return _unknown(p, a, f"unknown dom path {p!r}")

    # ------------------------------------------------------------ provider: L4
    async def _p_wa(self, req: ObservationRequest) -> Observation:
        state = await l4.wa_state(self.cdp)
        if state is None or not state.get("ok"):
            return _unknown(req.path, req.args,
                            "WhatsApp Web tab not reachable (CDP missing or tab closed)")
        p, a = req.path, req.args
        if p == "wa.loaded":
            return _obs(p, a, state.get("ready") == "complete", "L4")
        if p == "wa.login":
            return _obs(p, a, bool(state.get("logged_in")), "L4",
                        note="qr_visible" if state.get("qr_visible") else "")
        if p == "wa.qr_visible":
            return _obs(p, a, bool(state.get("qr_visible")), "L4")
        if p == "wa.view":
            return _obs(p, a, state.get("view", "unknown"), "L4")
        if p == "wa.selected_chat":
            return _obs(p, a, state.get("selected_chat"), "L4")
        if p == "wa.search_query":
            return _obs(p, a, state.get("search_query"), "L4")
        if p == "wa.chat_count":
            return _obs(p, a, state.get("chat_count"), "L4")
        return _unknown(p, a, f"unknown wa path {p!r}")

    async def _p_comms(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p == "comms.sent":
            recipient = str(a.get("recipient", ""))
            text = str(a.get("text", ""))
            state = await l4.wa_state(self.cdp)
            if state is None or not state.get("logged_in"):
                return _unknown(p, a, "chat transcript not reachable at L4")
            if recipient.casefold() not in str(state.get("selected_chat") or "").casefold():
                return _unknown(p, a, "recipient chat is not the open conversation")
            found = await l4.wa_transcript_contains(self.cdp, text)
            if found is None:
                return _unknown(p, a, "transcript probe failed")
            return _obs(p, a, found, "L4")
        return _unknown(p, a, f"unknown comms path {p!r}")

    # ------------------------------------------------------------ provider: fs
    async def _p_files(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        raw = str(a.get("path", ""))
        path = Path(raw).expanduser() if raw else None

        def _stat() -> dict | None:
            if path is None:
                return None
            try:
                st = path.stat()
                return {"exists": True, "is_dir": path.is_dir(), "size": st.st_size,
                        "modified": st.st_mtime}
            except OSError:
                return None

        info = await asyncio.to_thread(_stat)
        exists = bool(info and info["exists"])
        if p == "files.exists":
            return _obs(p, a, exists, "L1")
        if p == "files.is_dir":
            return _obs(p, a, bool(info and info["is_dir"]), "L1")
        if p == "files.size":
            return _obs(p, a, info["size"] if info else 0, "L1")
        if p == "files.modified_at":
            return _obs(p, a, info["modified"] if info else None, "L1")
        if p == "files.content":
            if not exists or (info and info["is_dir"]):
                return _obs(p, a, None, "L1")
            def _read() -> str | None:
                try:
                    return path.read_text(encoding="utf-8", errors="replace")[:20000]
                except OSError:
                    return None
            return _obs(p, a, await asyncio.to_thread(_read), "L1")
        if p == "files.list":
            if not exists or not info["is_dir"]:
                return _obs(p, a, [], "L1")
            def _list() -> list[str]:
                try:
                    return sorted(str(c.name) for c in list(path.iterdir())[:200])
                except OSError:
                    return []
            return _obs(p, a, await asyncio.to_thread(_list), "L1")
        if p == "files.free_bytes":
            try:
                import shutil as _sh
                usage = await asyncio.to_thread(_sh.disk_usage, str(path.parent if path else Path.home()))
                return _obs(p, a, usage.free, "L1")
            except Exception:
                return _unknown(p, a, "disk usage unavailable")
        return _unknown(p, a, f"unknown files path {p!r}")

    async def _p_downloads(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p == "downloads.exists":
            pat = str(a.get("file", "*"))
            target = DOWNLOADS_DIR / pat if not pat.endswith(("*", "?")) else None

            def _check() -> bool:
                try:
                    if target is not None:
                        return target.exists()
                    return any(DOWNLOADS_DIR.glob(pat))
                except OSError:
                    return False

            return _obs(p, a, await asyncio.to_thread(_check), "L1")
        if p == "downloads.newest":
            def _newest() -> str | None:
                try:
                    files = [f for f in DOWNLOADS_DIR.iterdir() if f.is_file()]
                    if not files:
                        return None
                    newest = max(files, key=lambda f: f.stat().st_mtime)
                    return newest.name
                except OSError:
                    return None
            return _obs(p, a, await asyncio.to_thread(_newest), "L1")
        return _unknown(p, a, f"unknown downloads path {p!r}")

    # ------------------------------------------------------------ provider: apps
    async def _p_apps(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p == "apps.running":
            name = str(a.get("name", "")).casefold()
            exes = _proc_exes(name)
            if exes is None:
                return _unknown(p, a, f"unknown app {name!r} (no exe mapping)")
            running = l1.process_running(exes)
            return _obs(p, a, running, "L1")
        if p == "apps.pids":
            name = str(a.get("name", "")).casefold()
            exes = _proc_exes(name)
            if exes is None:
                return _unknown(p, a, f"unknown app {name!r}")
            return _obs(p, a, l1.process_pids(exes), "L1")
        if p == "apps.foreground":
            fg = l1.foreground_window()
            return _obs(p, a, fg.get("title"), "L1", note=str(fg.get("process") or ""))
        if p == "apps.foreground_process":
            return _obs(p, a, l1.foreground_window().get("process"), "L1")
        return _unknown(p, a, f"unknown apps path {p!r}")

    async def _p_terminal(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p == "terminal.last_exit_code":
            if "terminal.last_exit_code" not in self.memo:
                return _unknown(p, a, "no terminal command executed in this session")
            return _obs(p, a, self.memo.get("terminal.last_exit_code"), "L0")
        if p == "terminal.last_output":
            return _obs(p, a, self.memo.get("terminal.last_output"), "L0")
        return _unknown(p, a, f"unknown terminal path {p!r}")

    async def _p_reads(self, req: ObservationRequest) -> Observation:
        """L0 completion signal for pure read-only tools (the weakest acceptable
        evidence level; only used where the action mutates nothing)."""
        value = self.memo.get(req.path)
        if value is None:
            return _unknown(req.path, req.args, "read not performed yet")
        return _obs(req.path, req.args, value, "L0")

    async def _p_ui(self, req: ObservationRequest) -> Observation:
        p, a = req.path, req.args
        if p == "ui.last_click":
            if "ui.last_click" in self.memo:
                return _obs(p, a, self.memo["ui.last_click"], "L0")
            return _unknown(p, a, "no pointer action recorded in this session")
        if p == "ui.asked":
            value = self.memo.get("ui.asked")
            if value is None:
                return _unknown(p, a, "no question asked in this session")
            return _obs(p, a, value, "L0")
        if p == "ui.user_answer":
            # answers are per-question and live on the task (durable across restarts),
            # so asking a *different* question is never mistaken for an answer
            question = str(a.get("question", ""))
            answers = self._answers()
            if question and question in answers:
                return _obs(p, a, answers[question], "L0", note="user answered")
            return _unknown(p, a, "the user has not answered this question yet")
        return _unknown(p, a, f"unknown ui path {req.path!r}")

    def _answers(self) -> dict[str, str]:
        task = self.task
        if task is None:
            return {}
        try:
            answers = task.context.get("answers") or {}
        except AttributeError:
            return {}
        return dict(answers) if isinstance(answers, dict) else {}

    # ---------------------------------------------------------- provider: rules
    async def _p_rules(self, req: ObservationRequest) -> Observation:
        """Durable user rules are memory, not code: this provider only reports them."""
        p, a = req.path, req.args
        if self.memory is None:
            return _unknown(p, a, "no memory store wired (rules unavailable)")
        try:
            if p == "rules.exists":
                return _obs(p, a, bool(self.memory.has_rule(str(a.get("rule", "")))), "L1")
            if p == "rules.count":
                return _obs(p, a, len(self.memory.list_rules()), "L1")
            if p == "rules.list":
                query = str(a.get("query", ""))
                rules = self.memory.list_rules()
                if query:
                    rules = [r for r in rules
                             if query.casefold() in f"{r.when_text} {r.instruction}".casefold()]
                return _obs(p, a, [r.instruction for r in rules][:20], "L1")
        except Exception as exc:
            return _unknown(p, a, f"rule store error: {type(exc).__name__}")
        return _unknown(p, a, f"unknown rules path {p!r}")

    # ------------------------------------------------------------------ extras
    async def capture_evidence(self, label: str = "screen",
                               region: tuple[int, int, int, int] | None = None) -> str | None:
        path = await capture_screen(self.settings, region=region, label=label)
        return str(path) if path else None

    async def close(self) -> None:
        await self.cdp.close()
