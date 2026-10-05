"""Generic browser verbs (L3 DOM) — site-agnostic hands for any web app.

Knowledge of *specific* sites lives in data tables (``data/webapps.json``) and in
learned rules; these tools only know HTML concepts. Elements are addressed by
role/name/selector, never by pixels, and their effects are verified from DOM
state (``page.text_contains`` / ``dom.value`` / ``dom.match`` / ``chrome.has_tab``)
or, where no structural signal exists, from a recorded ``dom.last_action`` note
(weakest acceptable evidence — never a screenshot).

Every handler passes the CDP-driven ``expect_text`` alternative so the caller can
demand a *real* page-level confirmation for its click/upload when one exists.
"""
from __future__ import annotations

import time

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from ..perception import l3_dom as dom
from . import note_read, read_effect

_NO_TARGET_ASK = ("Tell me what to act on: the visible text of the control "
                  "(query), a css selector, or a handle returned by browser.find.")
_CDP_ASK = ("Chrome is running without a debug port, so I cannot see the page. Close Chrome and "
            "ask me again (I'll relaunch it with the debug port).")


def _note_action(ctx: ToolContext, kind: str, target: str, **extra) -> None:
    ctx.perception.note("dom.last_action", {"kind": kind, "target": target,
                                            "at": time.time(), **extra}, "L0")


def _need_target(args) -> bool:
    return not (args.get("query") or args.get("handle"))


def _as_predicate_text(text: str) -> list[Predicate]:
    return [Predicate(path="page.text_contains", op="==", value=True, args={"text": text})]


def register_all(reg: ToolRegistry, settings) -> None:
    # ------------------------------------------------------------------ find
    async def find(args, ctx: ToolContext) -> ToolResult:
        res = await dom.find(ctx.perception.cdp, args["query"], by=args.get("by") or "text",
                             url_contains=args.get("url_contains"),
                             limit=int(args.get("limit") or 10))
        if not res.get("ok"):
            return ToolResult(ok=False, note=f"find failed: {res.get('error')}")
        note_read(ctx, "browser.find")
        matches = res.get("matches") or []
        _note_action(ctx, "find", args["query"], count=len(matches))
        return ToolResult(ok=True, outputs={"matches": matches, "count": len(matches),
                                            "url": res.get("url"), "title": res.get("title")},
                          observed=[ctx.perception.note("reads.browser.find", True, "L3")])

    reg.register(ToolContract(
        name="browser.find", category="browser",
        description="Find page elements by visible text / role / aria label / css selector (returns handles)",
        params={"query": ParamSpec(doc="text, role name, aria label or css selector"),
                "by": ParamSpec(required=False, doc="text|role|aria|selector (default text)"),
                "url_contains": ParamSpec(required=False, doc="pick the tab whose url contains this"),
                "limit": ParamSpec(type="int", required=False)},
        effects=read_effect("browser.find"),
        verification=VerificationSpec(method="L3_dom_query", timeout_s=12.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), find)

    # ----------------------------------------------------------------- click
    async def click(args, ctx: ToolContext) -> ToolResult:
        if _need_target(args):
            return ToolResult(ok=False, blocked=True, note="no click target given", ask=_NO_TARGET_ASK)
        res = await dom.click(ctx.perception.cdp, handle=args.get("handle"),
                              query=args.get("query"), by=args.get("by") or "text",
                              url_contains=args.get("url_contains"))
        if not res.get("ok"):
            return ToolResult(ok=False, note=f"click failed: {res.get('error')}")
        _note_action(ctx, "click", str(res.get("target") or res.get("handle")),
                     label=str(res.get("label") or "")[:120])
        return ToolResult(ok=True, mutated=True, outputs={k: res.get(k) for k in
                                                          ("handle", "tag", "role", "label", "method")},
                          note=f"clicked {res.get('tag')} {str(res.get('label'))[:80]!r}")

    reg.register(ToolContract(
        name="browser.click", category="browser",
        description="Click a page element by handle or by text/role/selector (structural, no coordinates)",
        params={"query": ParamSpec(required=False), "by": ParamSpec(required=False),
                "handle": ParamSpec(required=False), "url_contains": ParamSpec(required=False),
                "expect_text": ParamSpec(required=False,
                                         doc="text that must appear on the page afterwards")},
        preconditions=[Predicate(path="chrome.debug_port", op="exists")],
        effects_for=lambda a: (_as_predicate_text(a["expect_text"]) if a.get("expect_text")
                              else [Predicate(path="dom.last_action", op="exists",
                                              args={"kind": "click"})]),
        verification=VerificationSpec(method="L3_dom_state", timeout_s=12.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.MEDIUM,
        ui_lock=True, uiaction=True), click)

    # ------------------------------------------------------------------ type
    async def type_text(args, ctx: ToolContext) -> ToolResult:
        if _need_target(args):
            return ToolResult(ok=False, blocked=True, note="no input target given", ask=_NO_TARGET_ASK)
        res = await dom.type_into(ctx.perception.cdp, text=args["text"], handle=args.get("handle"),
                                  query=args.get("query"), by=args.get("by") or "text",
                                  submit=bool(args.get("submit")),
                                  url_contains=args.get("url_contains"))
        if not res.get("ok"):
            return ToolResult(ok=False, note=f"type failed: {res.get('error')}")
        _note_action(ctx, "type", str(res.get("target") or res.get("handle")),
                     chars=len(args["text"]))
        return ToolResult(ok=True, mutated=True,
                          outputs={"handle": res.get("handle"), "value": res.get("value"),
                                   "submitted": res.get("submitted")},
                          note=f"typed {len(args['text'])} chars"
                               + (" and submitted" if res.get("submitted") else ""))

    reg.register(ToolContract(
        name="browser.type", category="browser",
        description="Type text into a page field by handle/query; submit=True presses Enter",
        params={"text": ParamSpec(), "query": ParamSpec(required=False),
                "by": ParamSpec(required=False), "handle": ParamSpec(required=False),
                "submit": ParamSpec(type="bool", required=False),
                "url_contains": ParamSpec(required=False),
                "expect_text": ParamSpec(required=False,
                                         doc="text that must appear after submit (overrides value check)")},
        preconditions=[Predicate(path="chrome.debug_port", op="exists")],
        effects_for=lambda a: (_as_predicate_text(a["expect_text"]) if a.get("expect_text") else
                              ([Predicate(path="dom.value", op="==", value=a["text"],
                                          args={"handle": a["handle"]})] if a.get("handle")
                               else [Predicate(path="dom.value", op="==", value=a["text"],
                                               args={"query": a["query"], "by": a.get("by") or "text"})]
                               if a.get("query") else
                               [Predicate(path="dom.last_action", op="exists", args={"kind": "type"})])),
        verification=VerificationSpec(method="L3_dom_state", timeout_s=12.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.MEDIUM,
        ui_lock=True, uiaction=True), type_text)

    # ------------------------------------------------------------------ read
    async def read(args, ctx: ToolContext) -> ToolResult:
        res = await dom.read(ctx.perception.cdp, handle=args.get("handle"),
                             max_chars=int(args.get("max_chars") or 4000),
                             url_contains=args.get("url_contains"))
        if not res.get("ok"):
            return ToolResult(ok=False, note=f"read failed: {res.get('error')}")
        note_read(ctx, "browser.read")
        text = str(res.get("text") or "")
        return ToolResult(ok=True, outputs={"text": text, "url": res.get("url"),
                                            "title": res.get("title"), "chars": len(text)},
                          observed=[ctx.perception.note("reads.browser.read", True, "L3")])

    reg.register(ToolContract(
        name="browser.read", category="browser",
        description="Read page (or element) text via CDP — the cheap way to 'see' a web page",
        params={"handle": ParamSpec(required=False), "max_chars": ParamSpec(type="int", required=False),
                "url_contains": ParamSpec(required=False)},
        effects=read_effect("browser.read"),
        verification=VerificationSpec(method="L3_dom_read", timeout_s=12.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True, parallel_safe=True), read)

    # ---------------------------------------------------------------- upload
    async def upload(args, ctx: ToolContext) -> ToolResult:
        res = await dom.upload(ctx.perception.cdp, path=args["path"], handle=args.get("handle"),
                               query=args.get("query"), by=args.get("by") or "selector",
                               url_contains=args.get("url_contains"))
        if not res.get("ok"):
            return ToolResult(ok=False, note=f"upload failed: {res.get('error')}")
        _note_action(ctx, "upload", args["path"])
        return ToolResult(ok=True, mutated=True, outputs={"path": args["path"]},
                          note=f"attached {args['path']}")

    reg.register(ToolContract(
        name="browser.upload", category="browser",
        description="Attach a local file to a page's file input (CDP setFileInputFiles)",
        params={"path": ParamSpec(), "query": ParamSpec(required=False),
                "by": ParamSpec(required=False), "handle": ParamSpec(required=False),
                "url_contains": ParamSpec(required=False), "expect_text": ParamSpec(required=False)},
        preconditions=[Predicate(path="chrome.debug_port", op="exists")],
        preconditions_for=lambda a: [Predicate(path="files.exists", op="==", value=True,
                                               args={"path": a["path"]})],
        effects_for=lambda a: (_as_predicate_text(a["expect_text"]) if a.get("expect_text")
                              else [Predicate(path="dom.last_action", op="exists",
                                              args={"kind": "upload"})]),
        verification=VerificationSpec(method="L3_dom_state", timeout_s=12.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.MEDIUM,
        ui_lock=True, uiaction=True), upload)

    # -------------------------------------------------------------- wait_for
    async def wait_for(args, ctx: ToolContext) -> ToolResult:
        if not (args.get("query") or args.get("expect_text")):
            return ToolResult(ok=False, blocked=True, note="nothing to wait for",
                              ask="Give me the text or the element to wait for.")
        res = await dom.wait_for(ctx.perception.cdp, query=args.get("query"),
                                 by=args.get("by") or "text",
                                 expect_text=args.get("expect_text"),
                                 timeout_s=float(args.get("timeout_s") or 10.0),
                                 url_contains=args.get("url_contains"))
        if not res.get("ok"):
            return ToolResult(ok=False, note=f"wait failed: {res.get('error')}")
        if not res.get("found"):
            return ToolResult(ok=False, note=str(res.get("note") or "not seen in time"))
        return ToolResult(ok=True, outputs={"found": True, "matches": res.get("matches") or []},
                          note=str(res.get("note") or "visible"))

    reg.register(ToolContract(
        name="browser.wait_for", category="browser",
        description="Poll until an element/text appears on the page (bounded until-semantics)",
        params={"query": ParamSpec(required=False), "by": ParamSpec(required=False),
                "expect_text": ParamSpec(required=False),
                "timeout_s": ParamSpec(type="float", required=False),
                "url_contains": ParamSpec(required=False)},
        preconditions=[Predicate(path="chrome.debug_port", op="exists")],
        effects_for=lambda a: (_as_predicate_text(a["expect_text"]) if a.get("expect_text") else
                              [Predicate(path="dom.match", op="==", value=True,
                                         args={"query": a["query"], "by": a.get("by") or "text"})]),
        verification=VerificationSpec(method="L3_dom_state", timeout_s=15.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), wait_for)

    # ------------------------------------------------------------------ tabs
    async def tabs(args, ctx: ToolContext) -> ToolResult:
        open_tabs = await dom.tabs(ctx.perception.cdp)
        if open_tabs is None:
            return ToolResult(ok=False, blocked=True, note="CDP unavailable", ask=_CDP_ASK)
        note_read(ctx, "browser.tabs")
        return ToolResult(ok=True, outputs={"tabs": open_tabs, "count": len(open_tabs)},
                          observed=[ctx.perception.note("reads.browser.tabs", True, "L3")])

    reg.register(ToolContract(
        name="browser.tabs", category="browser", description="List open tabs (url + title) via CDP",
        params={}, effects=read_effect("browser.tabs"),
        verification=VerificationSpec(method="L3_tab_exists", timeout_s=10.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True, parallel_safe=True), tabs)

    async def tab_open(args, ctx: ToolContext) -> ToolResult:
        res = await dom.new_tab(ctx.perception.cdp, args["url"])
        if not res.get("ok"):
            return ToolResult(ok=False, blocked=True, note=str(res.get("error")), ask=_CDP_ASK)
        return ToolResult(ok=True, mutated=True, outputs=res, note=f"opened {args['url']}")

    reg.register(ToolContract(
        name="browser.tab_open", category="browser", description="Open a new tab at a URL (CDP)",
        params={"url": ParamSpec()},
        preconditions=[Predicate(path="chrome.debug_port", op="exists")],
        effects_for=lambda a: [Predicate(path="chrome.has_tab", op="==", value=True,
                                         args={"match": f"*{a['url']}*"})],
        verification=VerificationSpec(method="L3_tab_exists", timeout_s=15.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), tab_open)

    async def tab_close(args, ctx: ToolContext) -> ToolResult:
        res = await dom.close_tab(ctx.perception.cdp, url_contains=args["target_url_contains"])
        if not res.get("ok"):
            return ToolResult(ok=False, blocked=True, note=str(res.get("error")), ask=_CDP_ASK)
        return ToolResult(ok=True, mutated=True, outputs=res,
                          note=f"closed tab {res.get('closed') or res.get('target_id')}")

    reg.register(ToolContract(
        name="browser.tab_close", category="browser", description="Close the tab whose URL matches",
        params={"target_url_contains": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="chrome.has_tab", op="==", value=True,
                                               args={"match": f"*{a['target_url_contains']}*"})],
        effects_for=lambda a: [Predicate(path="chrome.has_tab", op="==", value=False,
                                         args={"match": f"*{a['target_url_contains']}*"})],
        verification=VerificationSpec(method="L3_tab_exists", timeout_s=10.0),
        idempotency=IdempotencyClass.DESTRUCTIVE, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), tab_close)

    async def tab_activate(args, ctx: ToolContext) -> ToolResult:
        res = await dom.activate_tab(ctx.perception.cdp, url_contains=args["target_url_contains"])
        if not res.get("ok"):
            return ToolResult(ok=False, blocked=True, note=str(res.get("error")), ask=_CDP_ASK)
        ctx.perception.note("chrome.active_tab_url", res.get("url") or args["target_url_contains"],
                            "L3")
        return ToolResult(ok=True, mutated=True, outputs=res, note=f"focused {res.get('url')}")

    reg.register(ToolContract(
        name="browser.tab_activate", category="browser",
        description="Bring the matching tab to the foreground (Target.activateTarget)",
        params={"target_url_contains": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="chrome.has_tab", op="==", value=True,
                                               args={"match": f"*{a['target_url_contains']}*"})],
        effects_for=lambda a: [Predicate(path="chrome.active_tab_url", op="contains",
                                         value=a["target_url_contains"])],
        verification=VerificationSpec(method="L3_url", timeout_s=10.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), tab_activate)
