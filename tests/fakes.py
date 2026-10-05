"""Fake-world harness (doc 11 "suite mechanics"): deterministic simulated
windows/CDP/a11y so every scenario is CI-able without touching the machine."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from eva.core import (CheckpointStore, Condition, EventBus, IdempotencyClass, Orchestrator,
                      ParamSpec, Predicate, RiskGate, RiskLevel, Step, Task, ToolContract,
                      ToolRegistry, ToolResult, Truth, VerificationSpec)
from eva.config import Settings
from eva.core.state import Observation, ObservationRequest
from eva.models.actor import ActionProposal
from eva.perception.policy import PolicyGate
from eva.storage import Database
from eva.tools.agent_tools import register_all as register_agent_tools


class FakeWorld:
    def __init__(self) -> None:
        self.chrome_running = False
        self.chrome_profile: str | None = None
        self.chrome_page_url: str | None = None
        self.foreground: str | None = None
        self.wa_login = False
        self.wa_view = "unknown"          # qr_login | chatlist | archived
        self.wa_selected_chat: str | None = None
        self.wa_search_query: str | None = None
        self.apps: dict[str, bool] = {}
        self.files: dict[str, str] = {}    # path -> content
        self.big_paths: dict[str, int] = {}  # path -> bytes (for risk sizing via real fs)
        self.deleted: list[str] = []
        self.downloads: list[str] = []
        self.messages: list[dict[str, str]] = []
        self.clicks: list[str] = []
        self.healed = False
        self.open_calls = 0
        self.search_calls = 0
        self.send_calls = 0
        self.fail_open_times = 0
        self.stall_navigation = False

    def resolve_profile(self, query: str) -> str | None:
        table = {"aiou": "Profile 11", "profile 11": "Profile 11", "11": "Profile 11",
                 "default": "Default", "personal": "Profile 29", "29": "Profile 29"}
        return table.get(str(query).strip().casefold())


def _obs(path: str, args: dict[str, Any], value: Any, level: str, note: str = "") -> Observation:
    return Observation(path=path, args=args, truth=Truth.TRUE, value=value, level=level, note=note)


def _unknown(path: str, args: dict[str, Any], note: str = "no provider") -> Observation:
    return Observation(path=path, args=args, truth=Truth.UNKNOWN, value=None, level="L0", note=note)


class FakePerception:
    def __init__(self, world: FakeWorld, allow_vision: bool = False, memory=None):
        self.world = world
        self.policy = PolicyGate(allow_vision=allow_vision)
        self.observe_calls = 0
        self.notes: dict[str, Any] = {}
        self.seen: dict[str, dict] = {}     # last resolution per request key
        self.memory = memory        # durable rules, mirroring PerceptionEngine
        self.cdp = None            # web verbs are exercised with a dedicated fake

    def explain(self, paths: list[str] | None = None) -> list[dict]:
        """What the actor would be shown: notes (L0 writes) + the last observation
        resolved for each requested path, mirroring PerceptionEngine.explain()."""
        rows: dict[str, dict] = {}
        for k, v in self.notes.items():
            rows[k] = {"path": k, "truth": "TRUE", "value": v, "level": "L0", "age_s": 0.0,
                       "confidence": 0.9, "note": ""}
        rows.update(self.seen)
        return [r for k, r in sorted(rows.items())
                if not paths or any(str(r.get("path")).startswith(p) for p in paths)]

    def note(self, path: str, value: Any, level: str = "L0", args: dict | None = None,
             note: str = "") -> Observation:
        self.notes[path] = value
        return Observation(path=path, args=args or {}, truth=Truth.TRUE, value=value,
                           level=level, note=note, ttl_s=60.0)

    def bind_task(self, task: Task) -> None:
        self.policy.bind_task(task)

    async def observe(self, requests: list[ObservationRequest], max_age: float = 5.0) -> dict[str, Observation]:
        self.observe_calls += 1
        out = {r.key: self._resolve(r.path, r.args) for r in requests}
        for obs in out.values():       # feed explain() without altering resolution
            label = obs.path if not obs.args else (obs.path + json.dumps(obs.args, sort_keys=True,
                                                                        default=str))
            self.seen[obs.key] = {"path": label, "truth": obs.truth.value, "value": obs.value,
                                  "level": obs.level, "age_s": 0.0,
                                  "confidence": obs.confidence, "note": obs.note}
        return out

    async def verify(self, predicates: list[Predicate], timeout_s: float = 8.0) -> dict[str, Observation]:
        reqs = [ObservationRequest(path=p.path, args=p.args) for p in predicates]
        return await self.observe(reqs, max_age=1.0)

    def invalidate_for(self, predicates: list[Predicate]) -> None:
        return None

    # -- provider table --------------------------------------------------------
    def _resolve(self, path: str, args: dict[str, Any]) -> Observation:
        w = self.world
        if path == "chrome.running":
            return _obs(path, args, w.chrome_running, "L1")
        if path == "chrome.active_profile":
            return _obs(path, args, w.chrome_profile, "L3")
        if path == "chrome.page_url":
            return _obs(path, args, w.chrome_page_url, "L3")
        if path == "chrome.profiles.matched":
            name = w.resolve_profile(args.get("query", ""))
            return _obs(path, args, name is not None, "L3", note=name or "")
        if path == "wa.loaded":
            loaded = bool(w.chrome_page_url and "whatsapp" in w.chrome_page_url)
            return _obs(path, args, loaded, "L4")
        if path == "wa.login":
            return _obs(path, args, w.wa_login, "L4")
        if path == "wa.view":
            return _obs(path, args, w.wa_view, "L4")
        if path == "wa.selected_chat":
            return _obs(path, args, w.wa_selected_chat, "L4")
        if path == "wa.search_query":
            return _obs(path, args, w.wa_search_query, "L4")
        if path == "apps.running":
            return _obs(path, args, bool(w.apps.get(str(args.get("name", "")))), "L1")
        if path == "apps.foreground":
            return _obs(path, args, w.foreground, "L1")
        if path == "files.exists":
            return _obs(path, args, str(args.get("path", "")) in w.files, "L1")
        if path == "files.content":
            return _obs(path, args, w.files.get(str(args.get("path", ""))), "L1")
        if path == "downloads.exists":
            return _obs(path, args, str(args.get("file", "")) in w.downloads, "L1")
        if path == "comms.sent":
            found = any(m["recipient"] == args.get("recipient") and m["text"] == args.get("text")
                        for m in w.messages)
            return _obs(path, args, found, "L3")
        if path == "ui.last_click":
            return _obs(path, args, w.clicks[-1] if w.clicks else None, "L2")
        if path == "ui.asked":
            if "ui.asked" not in self.notes:
                return _unknown(path, args, "nothing asked yet")
            return _obs(path, args, self.notes["ui.asked"], "L0")
        if path == "ui.user_answer":
            if "ui.user_answer" not in self.notes:
                return _unknown(path, args, "not answered yet")
            return _obs(path, args, self.notes["ui.user_answer"], "L0")
        if path in ("rules.exists", "rules.count", "rules.list"):
            has = getattr(self.memory, "has_rule", None)
            if not callable(has):
                return _unknown(path, args, "no rule store wired")
            if path == "rules.exists":
                return _obs(path, args, bool(has(str(args.get("rule", "")))), "L1")
            if path == "rules.count":
                return _obs(path, args, len(self.memory.list_rules()), "L1")
            query = str(args.get("query", ""))
            rules = self.memory.list_rules()
            if query:
                rules = [r for r in rules
                         if query.casefold() in f"{r.when_text} {r.instruction}".casefold()]
            return _obs(path, args, [r.instruction for r in rules][:20], "L1")
        if path == "dom.value":
            if "dom.value" not in self.notes:
                return _unknown(path, args, "no dom probe")
            return _obs(path, args, self.notes["dom.value"], "L3")
        if path == "dom.last_action":
            if "dom.last_action" not in self.notes:
                return _unknown(path, args, "no dom action yet")
            return _obs(path, args, self.notes["dom.last_action"], "L0")
        if path == "page.text_contains":
            text = str(self.notes.get("page.text") or "")
            return _obs(path, args, str(args.get("text", "")).casefold() in text.casefold(), "L3")
        if path == "flaky.healed":
            return _obs(path, args, w.healed, "L1")
        if path == "system.cpu_percent":
            return _obs(path, args, 17.0, "L1")
        if path == "system.reported":
            return _obs(path, args, w.foreground is not None or True, "L0")
        return _unknown(path, args)


def build_fake_registry(world: FakeWorld, store: CheckpointStore | None = None) -> ToolRegistry:
    reg = ToolRegistry()

    async def open_profile(args, ctx):
        world.open_calls += 1
        if world.fail_open_times > 0:
            world.fail_open_times -= 1
            return ToolResult(ok=False, note="injected launch failure")
        world.chrome_running = True
        world.chrome_profile = world.resolve_profile(args.get("profile", ""))
        if world.stall_navigation:
            return ToolResult(ok=True, note="launched; page never loaded", mutated=False)
        world.chrome_page_url = args.get("url")
        return ToolResult(ok=True, mutated=True, outputs={"profile": world.chrome_profile})

    reg.register(ToolContract(
        name="chrome.open_profile", category="browser",
        description="Launch Chrome with a specific profile and URL",
        params={"profile": ParamSpec(), "url": ParamSpec()},
        effects_for=lambda a: [
            Predicate(path="chrome.running", op="==", value=True),
            Predicate(path="chrome.active_profile", op="exists"),
            Predicate(path="chrome.page_url", op="matches", value=a.get("url", "*")),
        ],
        verification=VerificationSpec(method="L1_process+L3_cdp_url", timeout_s=8.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), open_profile)

    async def navigate(args, ctx):
        if world.stall_navigation:
            return ToolResult(ok=True, note="navigation stalled", mutated=False)
        world.chrome_page_url = args.get("url")
        world.chrome_running = True
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="browser.navigate", category="browser", description="Navigate active tab",
        params={"url": ParamSpec()},
        effects_for=lambda a: [Predicate(path="chrome.page_url", op="matches", value=a.get("url", "*"))],
        verification=VerificationSpec(method="L3_cdp_url", timeout_s=6.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), navigate)

    async def open_archived(args, ctx):
        world.wa_view = "archived"
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="wa.open_archived", category="apps", description="Open the Archived chats folder",
        preconditions=[Predicate(path="wa.login", op="==", value=True),
                       Predicate(path="wa.loaded", op="==", value=True)],
        effects=[Predicate(path="wa.view", op="==", value="archived")],
        verification=VerificationSpec(method="L4_dom_view", timeout_s=6.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), open_archived)

    async def search_contact(args, ctx):
        world.search_calls += 1
        world.wa_search_query = args.get("q")
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="wa.search_contact", category="apps", description="Search a contact in WhatsApp",
        params={"q": ParamSpec()},
        preconditions=[Predicate(path="wa.view", op="==", value="archived")],
        effects_for=lambda a: [Predicate(path="wa.search_query", op="==", value=a.get("q"))],
        verification=VerificationSpec(method="L4_dom_search", timeout_s=6.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), search_contact)

    async def open_chat(args, ctx):
        world.wa_selected_chat = args.get("q")
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="wa.open_chat", category="apps", description="Open a conversation",
        params={"q": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="wa.search_query", op="==", value=a.get("q"))],
        effects_for=lambda a: [Predicate(path="wa.selected_chat", op="==", value=a.get("q"))],
        verification=VerificationSpec(method="L4_dom_chat", timeout_s=6.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), open_chat)

    async def launch_app(args, ctx):
        world.apps[str(args["name"])] = True
        world.foreground = str(args["name"])
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="apps.launch", category="apps", description="Launch an application",
        params={"name": ParamSpec()},
        effects_for=lambda a: [Predicate(path="apps.running", op="==", value=True, args={"name": a["name"]})],
        verification=VerificationSpec(method="L1_process", timeout_s=8.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), launch_app)

    async def focus_window(args, ctx):
        world.foreground = str(args["name"])
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="os.focus_window", category="os", description="Focus a window",
        params={"name": ParamSpec()},
        effects_for=lambda a: [Predicate(path="apps.foreground", op="==", value=a["name"])],
        verification=VerificationSpec(method="L1_foreground_window", timeout_s=4.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), focus_window)

    async def mkdir(args, ctx):
        world.files[str(args["path"])] = "<dir>"
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="files.mkdir", category="files", description="Create a directory",
        params={"path": ParamSpec()},
        effects_for=lambda a: [Predicate(path="files.exists", op="==", value=True, args={"path": a["path"]})],
        verification=VerificationSpec(method="fs_exists", timeout_s=4.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.LOW,
    ), mkdir)

    async def delete_path(args, ctx):
        p = str(args["path"])
        world.files.pop(p, None)
        world.deleted.append(p)
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="files.delete", category="files", description="Permanently delete a path",
        params={"path": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="files.exists", op="==", value=True, args={"path": a["path"]})],
        effects_for=lambda a: [Predicate(path="files.exists", op="==", value=False, args={"path": a["path"]})],
        verification=VerificationSpec(method="fs_not_exists", timeout_s=4.0),
        idempotency=IdempotencyClass.DESTRUCTIVE, risk=RiskLevel.HIGH,
    ), delete_path)

    async def download(args, ctx):
        world.downloads.append(str(args["file"]))
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="browser.download", category="browser", description="Trigger a download",
        params={"file": ParamSpec()},
        effects_for=lambda a: [Predicate(path="downloads.exists", op="==", value=True, args={"file": a["file"]})],
        verification=VerificationSpec(method="fs_event_downloads", timeout_s=8.0),
        idempotency=IdempotencyClass.CHECK_BEFORE_CREATE, risk=RiskLevel.LOW,
    ), download)

    async def send_message(args, ctx):
        world.send_calls += 1
        world.messages.append({"recipient": str(args["recipient"]), "text": str(args["text"])})
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="comms.send_message", category="comms", description="Send an outbound message",
        params={"recipient": ParamSpec(), "text": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="comms.sent", op="==", value=False,
                                               args={"recipient": a["recipient"], "text": a["text"]})],
        effects_for=lambda a: [Predicate(path="comms.sent", op="==", value=True,
                                         args={"recipient": a["recipient"], "text": a["text"]})],
        verification=VerificationSpec(method="L4_dom_transcript", timeout_s=6.0),
        idempotency=IdempotencyClass.EFFECT_TOKEN, risk=RiskLevel.HIGH, ui_lock=True,
    ), send_message)

    async def click_xy(args, ctx):
        world.clicks.append(f"{args['x']},{args['y']}")
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="ui.click_xy", category="os", description="Pointer click at coordinates (last resort)",
        params={"x": ParamSpec(type="int"), "y": ParamSpec(type="int"),
                "justification": ParamSpec(required=False)},
        effects_for=lambda a: [Predicate(path="ui.last_click", op="==", value=f"{a['x']},{a['y']}")],
        verification=VerificationSpec(method="L2_state_diff", timeout_s=3.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, structural_target=True,
    ), click_xy)

    async def flaky_click(args, ctx):
        if not world.healed:
            return ToolResult(ok=False, note="UI element not found")
        world.clicks.append("healed")
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="flaky.click", category="os", description="Deliberately flaky action for recovery tests",
        params={},
        effects=[Predicate(path="flaky.healed", op="==", value=True)],
        verification=VerificationSpec(method="L2_state_diff", timeout_s=2.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
        alternatives=["flaky.click_alt"],
    ), flaky_click)

    async def flaky_click_alt(args, ctx):
        world.healed = True
        world.clicks.append("alt-healed")
        return ToolResult(ok=True, mutated=True)

    reg.register(ToolContract(
        name="flaky.click_alt", category="os",
        description="Alternative structural targeting path (retry with a11y handle)",
        params={},
        effects=[Predicate(path="flaky.healed", op="==", value=True)],
        verification=VerificationSpec(method="L2_a11y_state_diff", timeout_s=2.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW, ui_lock=True,
    ), flaky_click_alt)

    return reg


class ScriptedActor:
    """Deterministic stand-in for the LLM actor: a queue of proposals, no network.

    Records what it was shown (state/history/rules/budget) so tests can assert
    the loop feeds the actor real observations instead of an empty prompt."""

    def __init__(self, proposals: list[Any] | None = None):
        self.proposals = list(proposals or [])
        self.calls: list[dict[str, Any]] = []

    async def next_action(self, task, *, state=None, history=None, memories=None, budget=None):
        self.calls.append({"state": state, "history": list(history or []),
                           "memories": memories, "budget": budget})
        if not self.proposals:
            raise RuntimeError("ScriptedActor ran out of proposals")
        item = self.proposals.pop(0)
        if isinstance(item, ActionProposal):
            return item
        return ActionProposal(**item)


def propose(tool: str, args: dict | None = None, reason: str = "do the thing") -> ActionProposal:
    return ActionProposal(kind="tool", tool=tool, args=args or {}, reason=reason)


def ask(question: str, options: list[str] | None = None, reason: str = "need a decision") -> ActionProposal:
    return ActionProposal(kind="ask", question=question, options=options or [], reason=reason)


def finish(reason: str = "looks done") -> ActionProposal:
    return ActionProposal(kind="finish", reason=reason)


class ScriptedPlanner:
    def __init__(self, plan: list[Step] | None = None, repairs: list[list[Step]] | None = None):
        self.plan_steps = plan or []
        self.repair_queue = list(repairs or [])
        self.plan_calls = 0
        self.repair_calls = 0

    async def plan(self, task, *, state=None, memory_slice=None):
        self.plan_calls += 1
        return [s.model_copy(deep=True) for s in self.plan_steps]

    async def repair(self, task, *, reason, state=None):
        self.repair_calls += 1
        if self.repair_queue:
            return [s.model_copy(deep=True) for s in self.repair_queue.pop(0)]
        return None


class ScriptedConfirm:
    def __init__(self, answers: list[bool] | None = None):
        self.answers = list(answers or [])
        self.seen: list[Any] = []

    async def __call__(self, decision, step, task):
        self.seen.append(decision)
        return self.answers.pop(0) if self.answers else False


class SimpleMemory:
    def __init__(self):
        self.episodes: list[Task] = []
        self.working: dict[str, Any] = {}

    def record_episode(self, task: Task) -> None:
        self.episodes.append(task)

    def clear_working(self, task_id: str) -> None:
        self.working.pop(task_id, None)

    def retrieve(self, task: Task) -> dict:
        return {}


def step(n: int, tool: str, args: dict | None = None, **kw) -> Step:
    return Step(n=n, intent=kw.pop("intent", f"step {n}: {tool}"), tool=tool, args=args or {}, **kw)


def whatsapp_steps() -> list[Step]:
    return [
        step(1, "chrome.open_profile", {"profile": "AIOU", "url": "https://web.whatsapp.com/"},
             intent="open WhatsApp Web in AIOU profile"),
        step(2, "wa.open_archived", intent="open Archived folder"),
        step(3, "wa.search_contact", {"q": "Saad Bhai"}, intent="search Saad Bhai"),
        step(4, "wa.open_chat", {"q": "Saad Bhai"}, intent="open conversation"),
    ]


def whatsapp_completion() -> Condition:
    return Condition(all_of=[
        Predicate(path="chrome.running", op="==", value=True),
        Predicate(path="chrome.active_profile", op="==", value="Profile 11"),
        Predicate(path="chrome.page_url", op="matches", value="*web.whatsapp.com*"),
        Predicate(path="wa.view", op="==", value="archived"),
        Predicate(path="wa.selected_chat", op="==", value="Saad Bhai"),
    ])


def make_task(request: str, plan: list[Step], completion: Condition, **kw) -> Task:
    return Task(user_request=request, goal=kw.pop("goal", request), completion=completion,
                plan=plan, **kw)


class Harness:
    """One-stop scenario rig: fake world + registry + perception + real orchestrator."""

    def __init__(self, tmp_path: Path, world: FakeWorld | None = None,
                 planner: ScriptedPlanner | None = None,
                 actor=None, settings=None,
                 confirm=None, memory: SimpleMemory | None = None,
                 distiller=None, allow_vision: bool = False, max_steps: int = 20):
        self.world = world or FakeWorld()
        self.bus = EventBus()
        self.db = Database(tmp_path / "eva.db")
        self.store = CheckpointStore(self.db)
        self.registry = build_fake_registry(self.world)
        self.memory = memory or SimpleMemory()
        self.perception = FakePerception(self.world, allow_vision=allow_vision,
                                         memory=self.memory)
        self.gate = RiskGate(allowed_roots=(tmp_path,), big_bytes=1000)
        self.confirm = confirm
        self.planner = planner
        # the interaction primitives (ask_user / remember) are part of every surface
        register_agent_tools(self.registry,
                             settings or Settings(root=tmp_path, data_dir=tmp_path,
                                                  logs_dir=tmp_path), self.memory)
        self.events: list[Any] = []
        self.bus.subscribe(lambda ev: self.events.append(ev))
        self.orchestrator = Orchestrator(
            registry=self.registry, perception=self.perception, bus=self.bus,
            store=self.store, gate=self.gate, confirm=confirm, planner=planner,
            actor=actor, settings=settings,
            recovery=None, memory=self.memory, distiller=distiller, max_steps=max_steps)
        self._hooks: list[Any] = []

    # -- running ---------------------------------------------------------------
    def run(self, task: Task):
        return asyncio.run(self.orchestrator.run(task))

    def resume(self, task_id: str):
        return asyncio.run(self.orchestrator.resume(task_id))

    def on_event(self, type_: str, fn) -> None:
        def wrapper(ev):
            if ev.type == type_:
                fn(ev)
        self.bus.subscribe(wrapper)

    def events_of(self, type_: str) -> list[Any]:
        return [e for e in self.events if e.type == type_]

    def cancel_after_verified(self, count: int) -> None:
        """Simulate a crash exactly once: cancel after N verified steps."""
        state = {"n": 0, "fired": False}

        def hook(ev):
            if state["fired"]:
                return
            if ev.payload.get("status") == "VERIFIED":
                state["n"] += 1
                if state["n"] >= count:
                    state["fired"] = True
                    self.orchestrator.cancel()
        self.bus.subscribe(hook)
