"""eDEX-style CLI (doc 09 §2) — a pure renderer of core events + a task entry point.

Commands: /help /state /debug /perf /tasks /memory /skills /explain /mode
          /archetype /mood /remind /watch /confirm /exit
Non-TTY confirmations fail safe to CANCEL (never hang, never silent-approve).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    _RICH = True
except ImportError:  # pragma: no cover
    _RICH = False

from ..bootstrap import EvaApp

C = {
    "ok": "green", "warn": "yellow", "err": "red", "dim": "dim",
    "task": "cyan", "accent": "magenta",
}


class _PrintConsole:  # minimal fallback without rich
    def print(self, *args, **kwargs):
        print(*[a if isinstance(a, str) else str(a) for a in args])

    def input(self, prompt=""):
        return input(prompt)

    def rule(self, title=""):
        print(f"--- {title} ---")


class EdexCLI:
    def __init__(self, app: EvaApp, debug: bool = False, quiet: bool = False):
        self.app = app
        self.console: Any = Console() if _RICH else _PrintConsole()
        self.debug = debug
        self.app.orchestrator.confirm = self.confirm
        if not quiet:                    # quiet: the HUD renders events itself
            app.bus.subscribe(self.render)

    # ------------------------------------------------------------------ banner
    def banner(self) -> None:
        tools = len(self.app.registry.names())
        providers = ", ".join(p["name"] for p in (self.app.pool.status() if self.app.pool else []))
        mode = self.app.mode()
        lines = [
            f"[{C['task']}]EVA v2[/]  stateful · verification-first personal agentic OS",
            f"tools {tools} · providers: {providers or 'none (offline planning unavailable)'} · mode: {mode}",
            "Type a goal, or /help for commands.",
        ]
        if _RICH:
            self.console.print(Panel("\n".join(lines), title="EVA", border_style="cyan"))
        else:
            self.console.print("\n".join(lines))

    # ------------------------------------------------------------------ loop
    async def run(self) -> int:
        self.banner()
        await self.app.start_services()
        try:
            while True:
                try:
                    line = await asyncio.to_thread(self.console.input, "\n[EVA] ❯ ")
                except (EOFError, KeyboardInterrupt):
                    break
                except Exception:
                    break
                line = (line or "").strip()
                if not line:
                    continue
                if line in ("/exit", "/quit", "exit", "quit"):
                    break
                if line.startswith("/"):
                    await self.command(line)
                    continue
                if self.app.pending_ask is not None:
                    result, reply = await self.app.answer_pending(line)
                    if reply is not None:
                        self.console.print(self.app.persona.flavor(reply))
                    elif result is not None:
                        self._verdict(result)
                    continue
                await self.goal(line)
        finally:
            await self.app.close()
            self.console.print(f"[{C['dim']}]EVA core stopped. State is checkpointed.[/]")
        return 0

    async def goal(self, text: str) -> None:
        result, reply = await self.app.handle(text)
        if reply is not None:
            self.console.print(self.app.persona.flavor(reply))
            return
        if result is not None:
            self._verdict(result)

    # ------------------------------------------------------------------ verdict
    def _verdict(self, result) -> None:
        status = result.status.value
        color = {"FINISHED": C["ok"], "FAILED": C["err"], "BLOCKED": C["warn"],
                 "CANCELLED": C["dim"]}.get(status, C["dim"])
        msg = self.app.persona.flavor(result.message)
        if _RICH:
            self.console.print(Panel(msg, title=f"{status}", border_style=color))
        else:
            self.console.print(msg)
        self._show_outputs(self.app.outputs_of(result))
        if self.app.pending_ask is not None:
            opts = self.app.pending_ask.get("options") or []
            hint = ("  choices: " + " / ".join(opts)) if opts else ""
            self.console.print(f"[{C['warn']}]waiting on you → just type the answer on the next "
                               f"line{hint}[/]")

    def _show_outputs(self, outs: list[dict]) -> None:
        for entry in outs[-2:]:
            o = entry.get("outputs") or {}
            if "resources" in o and isinstance(o["resources"], dict):
                r = o["resources"]
                self.console.print(
                    f"[{C['dim']}]cpu {r.get('cpu_percent')}% · ram free "
                    f"{r.get('ram_free_mb')} MB · disk free {r.get('disk_free_gb')} GB[/]")
            elif "windows" in o:
                titles = [w.get("title") if isinstance(w, dict) else w for w in o["windows"]][:8]
                self.console.print(f"[{C['dim']}]windows ({len(o['windows'])}): {', '.join(map(str, titles))}[/]")
            elif "matches" in o:
                self.console.print(f"[{C['dim']}]matches ({o.get('count')}): "
                                   f"{', '.join(map(str, o['matches'][:6]))}[/]")
            elif "content" in o:
                self.console.print(f"[{C['dim']}]{str(o['content'])[:400]}[/]")
            elif "output" in o:
                self.console.print(f"[{C['dim']}]{str(o['output'])[:400]}[/]")
            elif "text" in o:
                self.console.print(f"[{C['dim']}]{str(o['text'])[:400]}[/]")

    # ------------------------------------------------------------------ renderer
    def render(self, event) -> None:
        t, p = event.type, event.payload
        try:
            if t == "TASK_CREATED":
                self.console.print(f"[{C['dim']}]task {event.task_id} · {p.get('planned_by', '')}[/]")
            elif t == "TASK_PLAN_READY":
                steps = p.get("steps") or []
                if _RICH:
                    table = Table(title="PLAN", border_style="cyan", show_lines=False)
                    table.add_column("#", width=3)
                    table.add_column("intent")
                    table.add_column("tool", style="dim")
                    for s in steps:
                        table.add_row(str(s.get("n")), str(s.get("intent")), str(s.get("tool")))
                    self.console.print(table)
                else:
                    for s in steps:
                        self.console.print(f"  {s.get('n')}. {s.get('intent')} [{s.get('tool')}]")
            elif t == "TASK_STEP_START":
                self.console.print(f"[{C['dim']}]→ {p.get('step')}. {p.get('intent')} [{p.get('tool')}][/]")
            elif t == "TASK_STEP_RESULT":
                status = p.get("status")
                if status == "VERIFIED":
                    self.console.print(f"  [{C['ok']}]✓ step {p.get('step')} VERIFIED[/] [{C['dim']}]{p.get('effect', '')}[/]")
                elif status == "SKIPPED_ALREADY_DONE":
                    self.console.print(f"  [cyan]↷ step {p.get('step')} already done (no action)[/]")
                elif status == "FAILED":
                    self.console.print(f"  [{C['err']}]✗ step {p.get('step')} FAILED[/] expected {p.get('expected')} — observed {p.get('observed')}")
                elif status == "BLOCKED":
                    self.console.print(f"  [{C['warn']}]⏸ step {p.get('step')} BLOCKED[/] {p.get('reason', '')}")
            elif t == "TASK_REPLAN":
                self.console.print(f"[{C['warn']}]↻ replan: {p.get('reason')}[/]")
            elif t == "ACTOR_ACTION":
                self.console.print(f"[{C['accent']}]◆ {p.get('kind')}: {p.get('action')}[/]"
                                   f"[{C['dim']}] — {p.get('reason', '')}[/]")
            elif t == "TASK_ESCALATED":
                self.console.print(f"[{C['warn']}]↗ escalating to the actor loop "
                                   f"(was: {p.get('from')})[/]")
            elif t == "RULE_ENFORCED":
                self.console.print(f"[{C['err']}]⛔ rule enforced — {p.get('tool')} is forbidden: "
                                   f"{p.get('instruction')}[/]")
            elif t == "RULE_REMEMBERED":
                self.console.print(f"[{C['ok']}]✦ remembered: {p.get('instruction')}[/]")
            elif t == "RULE_FORGOTTEN":
                self.console.print(f"[{C['dim']}]forgot rule {p.get('rule')}[/]")
            elif t == "RISK_GATE":
                pass  # rendered by the confirm prompt itself
            elif t == "POLICY_REJECTED":
                self.console.print(f"[{C['err']}]⛔ policy rejection: {p.get('reason')}[/]")
            elif t == "TASK_WAITING_USER":
                if _RICH:
                    self.console.print(Panel(f"{p.get('reason', '')}\n[{C['warn']}]{p.get('ask', '')}[/]",
                                             title="NEEDS YOU", border_style="yellow"))
                else:
                    self.console.print(f"NEEDS YOU: {p.get('ask')}")
            elif t == "TASK_FINISHED":
                if p.get("status") == "success":
                    self.console.print(f"[{C['ok']}]■ TASK FINISHED[/]")
            elif t == "TASK_FAILED":
                self.console.print(f"[{C['err']}]■ TASK FAILED[/] {p.get('reason', '')}")
            elif t == "TASK_CANCELLED":
                self.console.print(f"[{C['dim']}]■ TASK CANCELLED[/] {p.get('reason', '')}")
            elif t == "MODEL_ROUTED":
                if not p.get("cached"):
                    self.console.print(f"[{C['dim']}]  model: tier{p.get('tier')} {p.get('provider')} "
                                       f"{p.get('latency_s', '')}s ({p.get('purpose', '')})[/]")
            elif t == "MODE_CHANGED":
                self.console.print(f"[{C['accent']}]mode: {p.get('mode')} ({p.get('reason', '')})[/]")
            elif t == "REMINDER_FIRED":
                self.console.print(f"[{C['accent']}]⏰ REMINDER: {p.get('text')}[/]")
            elif t == "WATCHER_TRIGGERED":
                self.console.print(f"[cyan]👁 new file: {p.get('file')}[/]")
            elif t == "SKILL_DISTILLED":
                self.console.print(f"[{C['ok']}]★ learned skill: {p.get('skill')}[/]")
            elif t == "SKILL_QUARANTINED":
                self.console.print(f"[{C['warn']}]skill quarantined: {p.get('skill')} "
                                   f"(success {p.get('success_rate')})[/]")
            elif t == "OBSERVATION" and self.debug:
                self.console.print(f"[{C['dim']}]  obs {p.get('path')} = {p.get('value')} [{p.get('level')}][/]")
            elif t == "IDEMPOTENT_SKIP" and self.debug:
                self.console.print(f"[{C['dim']}]  dedup: {p.get('tool')} skipped (effect token)[/]")
        except Exception:
            pass

    # ------------------------------------------------------------------ confirm
    async def confirm(self, decision, step, task) -> bool:
        if _RICH:
            self.console.print(Panel(
                f"[{C['warn']}]{decision.level.value.upper()} — {decision.title}[/]\n"
                f"{decision.consequences}\n[{C['dim']}]{decision.reason}[/]",
                title="⚠ CONFIRM", border_style="red"))
        else:
            self.console.print(f"{decision.level.value.upper()}: {decision.consequences}")
        if not sys.stdin.isatty():
            self.console.print(f"[{C['warn']}]non-interactive stdin → fail-safe CANCEL[/]")
            return False
        try:
            ans = await asyncio.wait_for(
                asyncio.to_thread(self.console.input, "[y] confirm   [n] cancel > "),
                timeout=self.app.settings.confirm_timeout_s)
        except (asyncio.TimeoutError, EOFError, KeyboardInterrupt):
            self.console.print(f"[{C['warn']}]no answer → CANCEL (fail-safe)[/]")
            return False
        return str(ans).strip().casefold() in ("y", "yes", "haan", "han", "ok", "confirm")

    # ------------------------------------------------------------------ commands
    async def command(self, line: str) -> None:
        parts = line.split()
        cmd, args = parts[0].casefold(), parts[1:]
        app = self.app
        if cmd == "/help":
            self.console.print("commands: /state /debug /perf /tasks /resume /memory /skills "
                               "/rules /answer /explain /mode /archetype /mood /remind /watch /exit")
        elif cmd == "/rules":
            if args and args[0].casefold() in ("forget", "bhool", "remove"):
                target = " ".join(args[1:])
                if target and app.memory.forget_rule(target):
                    self.console.print(f"forgot rule matching {target!r}")
                else:
                    self.console.print(f"no rule matched {target!r}")
            else:
                rules = app.memory.list_rules()
                if not rules:
                    self.console.print("(no remembered rules yet — say \"from now on ...\")")
                for r in rules:
                    bits = []
                    if r.when_text:
                        bits.append(f"when: {r.when_text}")
                    if r.scope and r.scope != "global":
                        bits.append(f"scope: {r.scope}")
                    if r.prefer_tools:
                        bits.append("prefer: " + ", ".join(r.prefer_tools))
                    if r.forbid_tools:
                        bits.append("never: " + ", ".join(r.forbid_tools))
                    self.console.print(f"  {r.id} · {r.instruction}  [{'; '.join(bits) or 'global'}] "
                                       f"used={r.used_count}")
        elif cmd == "/answer":
            if app.pending_ask is None:
                self.console.print("nothing is waiting for an answer")
            elif not args:
                self.console.print("usage: /answer <text>")
            else:
                result, reply = await app.answer_pending(" ".join(args))
                if reply is not None:
                    self.console.print(app.persona.flavor(reply))
                elif result is not None:
                    self._verdict(result)
        elif cmd == "/state":
            rows = app.perception.explain()[:25]
            for r in rows:
                self.console.print(f"  {r['path']} = {r['value']!r} [{r['level']} {r['age_s']}s]")
            if not rows:
                self.console.print("(no observations cached yet)")
        elif cmd == "/debug":
            self.debug = not self.debug
            self.console.print(f"debug {'on' if self.debug else 'off'}")
        elif cmd == "/perf":
            self.console.print(json.dumps(app.perf(), indent=2, default=str)[:3000])
        elif cmd == "/mode":
            r = app.monitor.report()
            self.console.print(f"mode: {r['mode']} {('- ' + r['reason']) if r['reason'] else ''} · "
                               f"rss {r['rss_mb']} MB · free ram {r['free_ram_mb']} MB")
        elif cmd == "/tasks":
            import time as _t
            for t in app.store.list_open_tasks():
                self.console.print(f"  {t.id} [{t.status.value}] {t.user_request[:70]}  "
                                   f"(resume: /resume {t.id})")
            for j in app.scheduler.list_jobs():
                self.console.print(f"  job {j['id']} [{j['kind']}] next {int(j['next_run_at'] - _t.time())}s")
        elif cmd == "/resume":
            if not args:
                self.console.print("usage: /resume <task_id>  (see /tasks)")
            else:
                try:
                    result = await app.resume_task(args[0])
                    self._verdict(result)
                except KeyError:
                    self.console.print(f"no stored task {args[0]}")
        elif cmd == "/memory":
            for e in app.memory.recent_episodes(8):
                self.console.print(f"  {e.get('outcome')} · {e.get('request', '')[:70]}")
        elif cmd == "/skills":
            for s in app.memory.list_skills():
                self.console.print(f"  {s.name} v{s.version} runs={s.runs} "
                                   f"success={s.success_rate:.2f} [{s.state}]")
        elif cmd == "/explain":
            if app.last_result is None:
                self.console.print("no task ran yet")
            else:
                t = app.last_result.task
                self.console.print(f"task {t.id} [{t.status.value}] mode={t.mode} "
                                   f"planned_by={t.planned_by} decisions={t.counters.actor_calls}")
                self.console.print(f"{t.result_message}")
                for s in t.plan:
                    ev = "; ".join(f"{e.level}:{e.path}" for e in s.evidence[:3])
                    why = f"  · why: {s.intent}" if s.intent else ""
                    self.console.print(f"  {s.n}. {s.tool} → {s.status.value} {ev}{why}")
                if t.failures:
                    for f in t.failures[-3:]:
                        self.console.print(f"  fail step {f.step}: {f.kind} {f.detail[:100]}")
        elif cmd == "/archetype":
            if args and app.persona.set_archetype(args[0]):
                self.console.print(f"archetype: {args[0]}")
            else:
                from ..persona.mood import ARCHETYPES
                self.console.print("choose: " + ", ".join(sorted(ARCHETYPES)))
        elif cmd == "/mood":
            self.console.print(app.persona.mood_line())
        elif cmd == "/remind":
            if len(args) >= 2:
                try:
                    job = app.scheduler.add_reminder(float(args[0]), " ".join(args[1:]))
                    self.console.print(f"reminder set: {job} in {args[0]} min")
                except ValueError:
                    self.console.print("usage: /remind <minutes> <text>")
            else:
                self.console.print("usage: /remind <minutes> <text>")
        elif cmd == "/watch":
            if args and args[0].casefold() in ("on", "off"):
                app.watchers.set_enabled(args[0].casefold() == "on")
                self.console.print(f"watchers {'on' if app.watchers.enabled else 'off'}")
            else:
                rules = app.watchers.list_rules()
                self.console.print(f"watchers enabled={app.watchers.enabled}, rules={len(rules)}")
        elif cmd == "/confirm":
            self.console.print("(confirmations are answered live at the prompt)")
        else:
            self.console.print(f"unknown command {cmd}")


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eva", description="EVA v2 — personal agentic OS")
    parser.add_argument("--explain", action="store_true",
                        help="show each step's reason (per-step narration, with --run)")
    parser.add_argument("--hud", action="store_true",
                        help="full-screen HUD dashboard (eDEX-UI style) instead of the plain prompt")
    parser.add_argument("--run", metavar="GOAL", help="headless one-shot: run one goal and exit")
    parser.add_argument("--json", action="store_true", help="machine-readable output (with --run)")
    parser.add_argument("--yes", action="store_true", help="auto-confirm HIGH/CRITICAL (with --run)")
    parser.add_argument("--voice", action="store_true", help="keep voice ON in headless mode")
    parser.add_argument("--no-voice", action="store_true", help="disable voice in the interactive CLI")
    parser.add_argument("--no-avatar", action="store_true", help="disable avatar bus in the interactive CLI")
    parser.add_argument("--debug", action="store_true", help="start with debug observations on")
    parser.add_argument("--profiles", nargs="?", const="", metavar="QUERY",
                        help="list the machine's real Chrome profiles and exit; optionally resolve QUERY")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.profiles is not None:
        from ..perception.l3_cdp import chrome_profiles, match_profile
        for p in chrome_profiles():
            print(f"{p['dir']:<12} {p['name']:<24} {p['email']}")
        if args.profiles:
            hit = match_profile(args.profiles)
            print(f"\n{args.profiles!r} -> {hit['dir']} ({hit['email']})" if hit
                  else f"\n{args.profiles!r} -> no matching profile")
        return 0

    if args.run:
        from .headless import run_once
        return asyncio.run(run_once(args.run, json_out=args.json, yes_high=args.yes,
                                    voice=args.voice, explain=args.explain))

    app = EvaApp(confirm=None, voice=not args.no_voice, avatar=not args.no_avatar,
                 speak=not args.no_voice)
    if args.hud:
        from .hud import run_hud
        return asyncio.run(run_hud(app))
    return asyncio.run(EdexCLI(app, debug=args.debug).run())
