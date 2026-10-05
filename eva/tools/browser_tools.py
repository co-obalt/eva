"""Browser tools — CDP-first, deterministic hands (doc 12 Layer A).

Attach policy (honest, no silent second browser):
  * debug port alive  -> operate in the user's real session (logins preserved)
  * Chrome not running -> launch it WITH the debug flag
  * Chrome running without the flag -> BLOCKED with a precise ask
"""
from __future__ import annotations

from ..core.contracts import (IdempotencyClass, ParamSpec, ToolContract, ToolContext,
                              ToolRegistry, ToolResult, VerificationSpec)
from ..core.state import Predicate
from ..core.task import RiskLevel
from ..perception import l1_system as l1
from ..perception import l4_dom as l4
from ..perception.l3_cdp import chrome_profiles, launch_chrome, match_profile, running_profile_dir

PORT_ASK = ("Chrome is already running without a debug port, so I cannot attach to your real "
            "session. Close Chrome and ask me again (I'll relaunch it with the debug port), "
            "or keep a Chrome shortcut with --remote-debugging-port=9222 for daily use.")


def _profile_choices() -> str:
    profs = chrome_profiles()
    return ", ".join(f"{p['dir']} ({p['name']}{' / ' + p['email'] if p['email'] else ''})"
                     for p in profs[:5]) or "none found"


def register_all(reg: ToolRegistry, settings) -> None:
    # ------------------------------------------------------------ ensure debug
    async def ensure_debug(args, ctx: ToolContext) -> ToolResult:
        cdp = ctx.perception.cdp
        port = await cdp.discover()
        if port:
            return ToolResult(ok=True, outputs={"port": port}, note=f"CDP already live on {port}")
        if l1.process_running(["chrome"]):
            return ToolResult(ok=False, blocked=True, note="Chrome running without debug port",
                              ask=PORT_ASK)
        profile = args.get("profile") or ""
        prof = match_profile(profile) if profile else None
        if profile and prof is None:
            return ToolResult(ok=False, blocked=True,
                              note=f"no Chrome profile matches {profile!r}",
                              ask=f"Available profiles: {_profile_choices()}")
        started = launch_chrome(prof["dir"] if prof else None, "about:blank",
                                settings.cdp_ports[0])
        if not started:
            return ToolResult(ok=False, note="could not launch Chrome")
        return ToolResult(ok=True, outputs={"launched": True,
                                            "profile": prof["dir"] if prof else "default"},
                          note="launched Chrome with debug port", mutated=True)

    reg.register(ToolContract(
        name="chrome.ensure_debug", category="browser",
        description="Ensure a CDP-attachable Chrome is running (launch only if not running)",
        params={"profile": ParamSpec(required=False)},
        effects=[Predicate(path="chrome.debug_port", op="exists")],
        verification=VerificationSpec(method="L1_debug_port", timeout_s=15.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), ensure_debug)

    # ------------------------------------------------------------ open profile
    async def open_profile(args, ctx: ToolContext) -> ToolResult:
        cdp = ctx.perception.cdp
        url = args.get("url") or "about:blank"
        query = args.get("profile") or ""
        prof = match_profile(query) if query else None
        if query and prof is None:
            return ToolResult(ok=False, blocked=True,
                              note=f"no Chrome profile matches {query!r}",
                              ask=f"Available profiles: {_profile_choices()}")
        port = await cdp.discover()
        if port:
            running = running_profile_dir()
            if prof and running and running != prof["dir"]:
                return ToolResult(
                    ok=False, blocked=True,
                    note=f"Chrome is running with {running}, not {prof['dir']}",
                    ask=f"Close Chrome (or use profile {running}) and ask me again.")
            opened = await cdp.create_tab(url)
            if not opened:
                return ToolResult(ok=False, note="CDP could not open a tab")
            return ToolResult(ok=True, outputs={"port": port, "url": url,
                                                "profile": running or (prof["dir"] if prof else "current")},
                              mutated=True)
        if l1.process_running(["chrome"]):
            return ToolResult(ok=False, blocked=True, note="Chrome running without debug port",
                              ask=PORT_ASK)
        started = launch_chrome(prof["dir"] if prof else None, url, settings.cdp_ports[0])
        if not started:
            return ToolResult(ok=False, note="could not launch Chrome")
        return ToolResult(ok=True, outputs={"launched": True, "url": url}, mutated=True)

    reg.register(ToolContract(
        name="chrome.open_profile", category="browser",
        description="Open a URL in the user's Chrome profile (attach if possible, else launch)",
        params={"profile": ParamSpec(required=False), "url": ParamSpec(required=False)},
        preconditions=[Predicate(path="chrome.binary_found", op="==", value=True)],
        effects_for=lambda a: [
            Predicate(path="chrome.running", op="==", value=True),
            Predicate(path="chrome.has_tab", op="==", value=True,
                      args={"match": f"*{a.get('url') or ''}*" if a.get("url") else "*"})],
        verification=VerificationSpec(method="L1_process+L3_cdp_tab", timeout_s=20.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), open_profile)

    # ------------------------------------------------------------ navigate
    async def navigate(args, ctx: ToolContext) -> ToolResult:
        cdp = ctx.perception.cdp
        session = await cdp.ensure()
        if session is None:
            return ToolResult(ok=False, blocked=True, note="CDP unavailable",
                              ask=PORT_ASK)
        target = await session.pick_target(args.get("target_url_contains"))
        if target is None:
            created = await session.create_target(args["url"])
            if not created:
                return ToolResult(ok=False, note="no target and createTarget failed")
            return ToolResult(ok=True, outputs={"created": True, "url": args["url"]}, mutated=True)
        try:
            sid = await session.attach(target["targetId"])
            await session.send("Page.navigate", {"url": args["url"]}, session_id=sid, timeout=15.0)
        except Exception as exc:
            return ToolResult(ok=False, note=f"navigate failed: {exc}")
        return ToolResult(ok=True, outputs={"url": args["url"], "target": target.get("url")},
                          mutated=True)

    reg.register(ToolContract(
        name="browser.navigate", category="browser", description="Navigate a tab via CDP (L3)",
        params={"url": ParamSpec(), "target_url_contains": ParamSpec(required=False)},
        preconditions=[Predicate(path="chrome.debug_port", op="exists")],
        effects_for=lambda a: [Predicate(path="chrome.has_tab", op="==", value=True,
                                         args={"match": f"*{a['url']}*"})],
        verification=VerificationSpec(method="L3_cdp_url", timeout_s=15.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), navigate)

    # ------------------------------------------------------------ WhatsApp L4
    async def wa_open_archived(args, ctx: ToolContext) -> ToolResult:
        res = await l4.wa_click_archived(ctx.perception.cdp)
        if not res.get("ok"):
            return ToolResult(ok=False, note=res.get("error", "archived click failed"))
        return ToolResult(ok=True, outputs=res, mutated=True)

    reg.register(ToolContract(
        name="wa.open_archived", category="apps", description="Open the Archived chats view (L4)",
        params={},
        preconditions=[Predicate(path="wa.login", op="==", value=True),
                       Predicate(path="wa.loaded", op="==", value=True)],
        effects=[Predicate(path="wa.view", op="==", value="archived")],
        verification=VerificationSpec(method="L4_dom_view", timeout_s=8.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), wa_open_archived)

    async def wa_search(args, ctx: ToolContext) -> ToolResult:
        res = await l4.wa_search(ctx.perception.cdp, args["q"])
        if not res.get("ok"):
            return ToolResult(ok=False, note=res.get("error", "search failed"))
        return ToolResult(ok=True, outputs=res, mutated=True)

    reg.register(ToolContract(
        name="wa.search_contact", category="apps", description="Search a contact (L4)",
        params={"q": ParamSpec()},
        preconditions=[Predicate(path="wa.login", op="==", value=True)],
        effects_for=lambda a: [Predicate(path="wa.search_query", op="==", value=a["q"])],
        verification=VerificationSpec(method="L4_dom_search", timeout_s=8.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), wa_search)

    async def wa_open_chat(args, ctx: ToolContext) -> ToolResult:
        res = await l4.wa_open_chat(ctx.perception.cdp, args["q"])
        if not res.get("ok"):
            return ToolResult(ok=False, note=res.get("error", "chat open failed"))
        return ToolResult(ok=True, outputs=res, mutated=True)

    reg.register(ToolContract(
        name="wa.open_chat", category="apps", description="Open a conversation row (L4)",
        params={"q": ParamSpec()},
        preconditions_for=lambda a: [Predicate(path="wa.search_query", op="==", value=a["q"])],
        effects_for=lambda a: [Predicate(path="wa.selected_chat", op="==", value=a["q"])],
        verification=VerificationSpec(method="L4_dom_chat", timeout_s=8.0),
        idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW,
        ui_lock=True, uiaction=True), wa_open_chat)

    async def wa_send(args, ctx: ToolContext) -> ToolResult:
        res = await l4.wa_send_message(ctx.perception.cdp, args["recipient"], args["text"])
        if not res.get("ok"):
            return ToolResult(ok=False, note=res.get("error", "send failed"))
        return ToolResult(ok=True, outputs=res, mutated=True)

    reg.register(ToolContract(
        name="wa.send_message", category="comms",
        description="Send a WhatsApp message (outbound communication — HIGH risk)",
        params={"recipient": ParamSpec(), "text": ParamSpec()},
        preconditions=[Predicate(path="wa.login", op="==", value=True)],
        effects_for=lambda a: [Predicate(path="comms.sent", op="==", value=True,
                                         args={"recipient": a["recipient"], "text": a["text"]})],
        verification=VerificationSpec(method="L4_dom_transcript", timeout_s=12.0),
        idempotency=IdempotencyClass.EFFECT_TOKEN, risk=RiskLevel.HIGH,
        ui_lock=True, uiaction=True), wa_send)
