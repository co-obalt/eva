"""M1 — the generic CDP web verbs and the site-agnostic L3 DOM layer.

No real browser is touched: a stub CdpManager records the JS each verb sends and
returns scripted results, so what is exercised is the *structure* of the contract
(handles instead of coordinates, effects verifiable from DOM state, honest
blocking when CDP is unavailable) rather than any particular site.
"""
from __future__ import annotations

import asyncio

from eva.config import Settings
from eva.core import CheckpointStore, Condition, EventBus, Orchestrator, RiskGate, TaskStatus
from eva.core.contracts import ToolContext
from eva.core.state import Observation, ObservationRequest, Predicate, Truth
from eva.core.task import RiskLevel, Task
from eva.perception import l3_dom as dom
from eva.perception.engine import PerceptionEngine
from eva.storage import Database
from eva.tools import build_registry
from tests.fakes import ScriptedActor, propose

WEB_VERBS = ("browser.find", "browser.click", "browser.type", "browser.read",
             "browser.upload", "browser.wait_for", "browser.tabs",
             "browser.tab_open", "browser.tab_close", "browser.tab_activate")


class StubCdp:
    """Records the JS it is asked to run and returns scripted results."""

    def __init__(self, *, results=None, tabs=None, port=None, session=None):
        self.results = list(results or [])
        self.js: list[str] = []
        self._tabs = tabs
        self._port = port
        self._session = session

    async def eval_js(self, js, url_contains=None):
        self.js.append(js)
        if not self.results:
            return {"ok": False, "error": "no scripted result"}
        return self.results.pop(0)

    async def tabs(self):
        return self._tabs

    async def discover(self):
        return self._port

    async def ensure(self):
        return self._session

    async def close(self):
        return None


class StubPerception:
    def __init__(self, cdp):
        self.cdp = cdp
        self.notes: dict = {}

    def note(self, path, value, level="L0", args=None, note=""):
        self.notes[path] = value
        return Observation(path=path, args=args or {}, truth=Truth.TRUE, value=value,
                           level=level, note=note)


def _settings(tmp_path) -> Settings:
    return Settings(root=tmp_path, data_dir=tmp_path, logs_dir=tmp_path)


def _registry(tmp_path):
    return build_registry(_settings(tmp_path), perception=None, scheduler=None, memory=None)


def _ctx(tmp_path, cdp) -> ToolContext:
    return ToolContext(perception=StubPerception(cdp), settings=_settings(tmp_path))


def _engine(tmp_path, cdp) -> PerceptionEngine:
    engine = PerceptionEngine(_settings(tmp_path))
    engine.cdp = cdp
    return engine


# ------------------------------------------------------------------ contracts
def test_generic_web_verbs_are_registered_with_structural_verification(tmp_path):
    reg = _registry(tmp_path)
    for name in WEB_VERBS:
        assert reg.get(name) is not None, name
    methods = {reg.require(n).contract.verification.method for n in WEB_VERBS}
    assert methods == {"L3_dom_query", "L3_dom_state", "L3_dom_read",
                       "L3_tab_exists", "L3_url"}
    # pointer-free by construction: no coordinate params in the generic layer
    for name in WEB_VERBS:
        assert not ({"x", "y"} & set(reg.require(name).contract.params)), name
    # reads are LOW risk, acts MEDIUM (nothing here is HIGH by itself)
    assert reg.require("browser.find").contract.risk is RiskLevel.LOW
    assert reg.require("browser.read").contract.risk is RiskLevel.LOW
    for name in ("browser.click", "browser.type", "browser.upload"):
        assert reg.require(name).contract.risk is RiskLevel.MEDIUM, name
    # an act verb cannot even try without a CDP page it can see
    for name in ("browser.click", "browser.type", "browser.upload"):
        pres = reg.require(name).contract.resolve_preconditions({"query": "x", "text": "t",
                                                                 "path": "p"})
        assert any(p.path == "chrome.debug_port" for p in pres), name


def test_click_and_type_effects_come_from_the_contract(tmp_path):
    reg = _registry(tmp_path)
    click = reg.require("browser.click").contract
    weak = click.resolve_effects({"query": "Sign in"})
    assert any(p.path == "dom.last_action" for p in weak)   # weakest honest signal
    strong = click.resolve_effects({"query": "Sign in", "expect_text": "Welcome"})
    assert any(p.path == "page.text_contains" and p.args == {"text": "Welcome"}
               for p in strong)
    typed = reg.require("browser.type").contract.resolve_effects({"text": "hello",
                                                                  "query": "Search"})
    assert any(p.path == "dom.value" and p.value == "hello" for p in typed)


# ------------------------------------------------------------------- handlers
def test_find_and_click_use_handles_not_pixels(tmp_path):
    match = {"ok": True, "matches": [{"handle": "e1-1", "tag": "button",
                                     "label": "Sign in"}],
             "count": 1, "url": "https://example.com/", "title": "Home"}
    cdp = StubCdp(results=[
        match,          # browser.find
        match,          # browser.click resolves its target through find first
        {"ok": True, "tag": "button", "role": "button", "label": "Sign in",
         "method": "click"},
    ])
    per = StubPerception(cdp)
    ctx = ToolContext(perception=per, settings=_settings(tmp_path))
    reg = _registry(tmp_path)

    found = asyncio.run(reg.require("browser.find").handler({"query": "Sign in"}, ctx))
    assert found.ok and found.outputs["count"] == 1
    assert found.outputs["matches"][0]["handle"] == "e1-1"
    assert found.observed and found.observed[0].path == "reads.browser.find"

    clicked = asyncio.run(reg.require("browser.click").handler({"query": "Sign in"}, ctx))
    assert clicked.ok and clicked.mutated
    assert per.notes["dom.last_action"]["kind"] == "click"
    # no coordinates were ever sent: finds tag handles, the click acts on one
    assert len(cdp.js) == 3
    assert all("data-eva-h" in js for js in cdp.js)


def test_click_and_type_without_a_target_ask_instead_of_guessing(tmp_path):
    ctx = _ctx(tmp_path, StubCdp())
    reg = _registry(tmp_path)
    click = asyncio.run(reg.require("browser.click").handler({}, ctx))
    type_ = asyncio.run(reg.require("browser.type").handler({"text": "hi"}, ctx))
    assert click.blocked and not click.mutated and "selector" in click.ask
    assert type_.blocked and not type_.mutated
    wait = asyncio.run(reg.require("browser.wait_for").handler({}, ctx))
    assert wait.blocked and "wait for" in wait.ask


def test_web_verbs_degrade_honestly_when_cdp_is_unavailable(tmp_path):
    ctx = _ctx(tmp_path, StubCdp(session=None, tabs=None))
    reg = _registry(tmp_path)
    # a tab verb cannot run without a CDP session: blocked + the precise ask
    opened = asyncio.run(reg.require("browser.tab_open").handler(
        {"url": "https://example.com/"}, ctx))
    assert opened.blocked and "debug port" in opened.ask
    listed = asyncio.run(reg.require("browser.tabs").handler({}, ctx))
    assert listed.blocked and "debug port" in listed.ask
    # upload reports the CDP gap honestly instead of pretending to succeed
    up = asyncio.run(reg.require("browser.upload").handler(
        {"path": "C:/nope.txt", "query": "input"}, ctx))
    assert not up.ok and "CDP unavailable" in up.note


def test_tabs_lists_and_activates_through_cdp(tmp_path):
    cdp = StubCdp(tabs=[{"targetId": "t1", "url": "https://example.com/a", "title": "A"},
                        {"targetId": "t2", "url": "https://example.com/b", "title": "B"}],
                  session=None)
    per = StubPerception(cdp)
    ctx = ToolContext(perception=per, settings=_settings(tmp_path))
    reg = _registry(tmp_path)

    listed = asyncio.run(reg.require("browser.tabs").handler({}, ctx))
    assert listed.ok and listed.outputs["count"] == 2

    # activation needs a live session; give the stub one for the real call
    class Session:
        def __init__(self):
            self.sent = []

        async def pick_target(self, url_contains):
            return {"targetId": "t2", "url": "https://example.com/b"}

        async def send(self, method, params, session_id=None):
            self.sent.append((method, params))
            return {"ok": True}

    session = Session()
    cdp._session = session
    activated = asyncio.run(reg.require("browser.tab_activate").handler(
        {"target_url_contains": "example.com/b"}, ctx))
    assert activated.ok and session.sent[0][0] == "Target.activateTarget"
    assert per.notes["chrome.active_tab_url"] == "https://example.com/b"


# -------------------------------------------------------------------- L3 DOM
def test_l3_dom_find_tags_handles_and_escapes_the_query():
    cdp = StubCdp(results=[{"ok": True, "matches": [{"handle": "e1-1", "label": "Save"}],
                            "count": 1}])
    res = asyncio.run(dom.find(cdp, 'Save "now"', by="text"))
    assert res["ok"] and res["matches"][0]["handle"] == "e1-1"
    js = cdp.js[0]
    assert "data-eva-h" in js and '\\"now\\"' in js        # JSON-escaped into the script
    assert "ROLE_SEL" in js                               # role knowledge is generic HTML


def test_l3_dom_state_and_read_use_the_recorded_handle():
    cdp = StubCdp(results=[
        {"ok": True, "exists": True, "value": "draft", "tag": "textarea", "label": "Message"},
        {"ok": True, "text": "hello world", "url": "https://x/", "title": "X"},
    ])
    state = asyncio.run(dom.state(cdp, "e1-7"))
    read = asyncio.run(dom.read(cdp, handle="e1-7", max_chars=1000))
    assert state["value"] == "draft" and read["text"] == "hello world"
    assert "data-eva-h" in cdp.js[0] and '"e1-7"' in cdp.js[0]


def test_l3_dom_tabs_hides_browser_internals():
    cdp = StubCdp(tabs=[{"targetId": "t1", "url": "chrome://newtab", "title": "New"},
                        {"targetId": "t2", "url": "devtools://devtools/bundled/x",
                         "title": "DevTools"},
                        {"targetId": "t3", "url": "https://example.com/", "title": "E"}])
    assert [t["target_id"] for t in asyncio.run(dom.tabs(cdp))] == ["t3"]
    assert asyncio.run(dom.tabs(StubCdp(tabs=None))) is None


def test_l3_dom_wait_for_polls_until_the_text_appears():
    ready = StubCdp(results=[{"ok": True, "text": "loading"},
                             {"ok": True, "text": "Ready to send"}])
    res = asyncio.run(dom.wait_for(ready, expect_text="ready to send", timeout_s=2.0))
    assert res["ok"] and res["found"] is True
    absent = StubCdp(results=[{"ok": True, "text": "nope"}] * 3)
    res2 = asyncio.run(dom.wait_for(absent, expect_text="absent", timeout_s=0.6))
    assert res2["ok"] and res2["found"] is False


# ------------------------------------------------------------- observations
def test_page_text_and_dom_observations_are_structural(tmp_path):
    cdp = StubCdp(results=[{"ok": True, "text": "Welcome back, Saad",
                            "url": "https://x/", "title": "X"}])
    engine = _engine(tmp_path, cdp)
    obs = asyncio.run(engine.observe([ObservationRequest(
        path="page.text_contains", args={"text": "welcome BACK"})]))
    assert obs['page.text_contains|{"text": "welcome BACK"}'].truth is Truth.TRUE
    assert obs['page.text_contains|{"text": "welcome BACK"}'].level == "L3"

    engine2 = _engine(tmp_path, StubCdp(results=[{"ok": True, "text": "nothing here"}]))
    obs2 = asyncio.run(engine2.observe([ObservationRequest(
        path="page.text_contains", args={"text": "missing"})]))
    assert obs2['page.text_contains|{"text": "missing"}'].value is False


def test_dom_match_value_and_tab_observations(tmp_path):
    state_row = {"ok": True, "exists": True, "value": "a@b.c", "tag": "input",
                 "label": "Email"}
    cdp = StubCdp(results=[
        {"ok": True, "matches": [{"handle": "e1-1", "label": "Email"}], "count": 1},
        state_row,      # dom.match resolves the query, then reads the element state
        state_row,      # dom.value reads the same handle directly
    ], tabs=[{"targetId": "t2", "url": "https://example.com/docs", "title": "Docs"}])
    engine = _engine(tmp_path, cdp)
    obs = asyncio.run(engine.observe([
        ObservationRequest(path="dom.match", args={"query": "Email"}),
        ObservationRequest(path="dom.value", args={"handle": "e1-1"}),
        ObservationRequest(path="chrome.has_tab", args={"match": "*example.com*"}),
        ObservationRequest(path="chrome.has_tab", args={"match": "*nope*"}),
    ]))
    assert obs['dom.match|{"query": "Email"}'].truth is Truth.TRUE
    assert obs['dom.value|{"handle": "e1-1"}'].value == "a@b.c"
    assert obs['chrome.has_tab|{"match": "*example.com*"}'].value is True
    assert obs['chrome.has_tab|{"match": "*nope*"}'].value is False
    # a verb records which tab it activated; the observation reports it
    engine.note("chrome.active_tab_url", "https://example.com/docs", "L3")
    obs2 = asyncio.run(engine.observe([ObservationRequest(path="chrome.active_tab_url")]))
    assert obs2["chrome.active_tab_url"].value == "https://example.com/docs"


# ------------------------------------------------- end-to-end actor web task
class SimPageCdp:
    """A tiny simulated page: find/read see its text; clicking flips it to the next state."""

    CALL = "Sign in"
    DONE = "Welcome back, Saad"
    url = "https://example.com/dashboard"

    def __init__(self):
        self.text = f"Please {self.CALL} to continue"
        self.clicked = False
        self.js: list[str] = []

    async def eval_js(self, js, url_contains=None):
        self.js.append(js)
        if "typeof el.click" in js:                       # click
            self.clicked = True
            self.text = self.DONE
            return {"ok": True, "tag": "button", "role": "button", "label": self.CALL,
                    "method": "click"}
        if "cands" in js:                                 # find (tags element handles)
            hit = self.CALL.casefold() in self.text.casefold()
            return {"ok": True,
                    "matches": ([{"handle": "e1-1", "tag": "button", "label": self.CALL}]
                                if hit else []),
                    "count": 1 if hit else 0, "url": self.url, "title": "Sim"}
        if "document.body" in js:                         # read / page text
            return {"ok": True, "text": self.text, "url": self.url, "title": "Sim"}
        if "LOOKUP" in js:                                # element state
            return {"ok": True, "exists": True, "value": None, "tag": "button",
                    "label": self.CALL, "visible": True, "enabled": True}
        return {"ok": False, "error": "unrecognised script"}

    async def tabs(self):
        return [{"targetId": "t1", "url": self.url, "title": "Sim"}]

    async def discover(self):
        return 9222

    async def ensure(self):
        return None

    async def close(self):
        return None


def test_actor_runs_a_novel_multi_step_web_task_end_to_end(tmp_path):
    """A site EVA was never taught: find a control, act on it, verify from the page."""
    settings = _settings(tmp_path)
    engine = PerceptionEngine(settings)
    sim = SimPageCdp()
    engine.cdp = sim
    reg = build_registry(settings, perception=engine, scheduler=None, memory=None)
    actor = ScriptedActor([
        propose("browser.find", {"query": "Sign in"}, reason="locate the sign-in control"),
        propose("browser.click", {"query": "Sign in", "expect_text": "Welcome back, Saad"},
                reason="click it, then confirm the welcome text"),
    ])
    task = Task(user_request="sign in to the example dashboard",
                goal="sign in to the example dashboard",
                completion=Condition(all_of=[
                    Predicate(path="page.text_contains", op="==", value=True,
                              args={"text": "Welcome back, Saad"})]),
                mode="actor", planned_by="actor")
    bus = EventBus()
    events: list = []
    bus.subscribe(lambda ev: events.append(ev))
    db = Database(tmp_path / "e2e.db")
    orchestrator = Orchestrator(registry=reg, perception=engine, bus=bus,
                                store=CheckpointStore(db),
                                gate=RiskGate(allowed_roots=(tmp_path,)), actor=actor,
                                settings=settings, max_steps=6)

    res = asyncio.run(orchestrator.run(task))

    assert res.status is TaskStatus.FINISHED
    assert [s.tool for s in task.plan] == ["browser.find", "browser.click"]
    assert all(s.status.value == "VERIFIED" for s in task.plan)
    assert task.counters.actor_calls == 2                   # one decision per action
    assert sim.clicked
    assert any(e.type == "ACTOR_ACTION" for e in events)
    assert [s.intent for s in task.plan] == ["locate the sign-in control",
                                             "click it, then confirm the welcome text"]
    db.close()


def test_debug_port_observation_is_honest(tmp_path):
    engine = _engine(tmp_path, StubCdp(port=9222))
    obs = asyncio.run(engine.observe([ObservationRequest(path="chrome.debug_port")]))
    assert obs["chrome.debug_port"].value == 9222
    # no CDP on the configured ports: value None -> a `debug_port exists` check is FALSE
    engine2 = _engine(tmp_path, StubCdp(port=None))
    obs2 = asyncio.run(engine2.observe([ObservationRequest(path="chrome.debug_port")]))
    assert obs2["chrome.debug_port"].value is None
    assert Predicate(path="chrome.debug_port", op="exists").matches_observation(
        obs2["chrome.debug_port"]) is Truth.FALSE
