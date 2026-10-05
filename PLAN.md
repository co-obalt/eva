# EVA v2 — Accepted Build Plan (executed autonomously)

> Read from: docs 01–13, EVA_INTELLIGENT_RESOURCES, EVA_VRM_INTEGRATION_PLAN, .env.example.
> Decision owner for this build: agent (user asleep, instructed: "decide yourself, build the whole system").
> Status of this document: **the accepted plan**. Every phase below is implemented in this tree.

---

## 1. What we are building

EVA v2: a resource-efficient personal agentic OS for one Windows machine that
*understands state before acting*, *verifies effects before claiming success*,
*uses the cheapest reliable perception/action method first*, *learns skills*,
and *escalates to expensive reasoning/vision only when necessary* — per docs 01–13.

## 2. Phase mapping (doc 10) → artifacts in this tree

| Phase | Deliverable | Where |
|---|---|---|
| 1 — Agent Core | Task/Plan/Step, predicates (3-valued), events, contracts, risk gate, recovery, checkpoints, orchestrator loop, effect journal | `eva/core/*.py` |
| 2 — State + Perception | Policy gate (INV-1..4), observation cache, ladder L0–L4 real (L1 Win32/psutil, L3 CDP+profiles, L4 DOM/WhatsApp), L5–L7 gated stubs, state diffing | `eva/perception/*.py` |
| 3 — Tools | Contract-validated registry, UI lock, idempotency classes, OS · Files · Browser · Terminal · Apps · Documents tools, app registry | `eva/tools/*.py`, `eva/apps/*.py` |
| 4 — Memory | SQLite WAL store (episodes/facts/profile/skills/journal/cache), confidence envelope + decay, bounded retrieval, skill distiller + skill planner | `eva/memory/*.py`, `eva/tasks/skills.py` |
| 5 — Intelligence | Tier 0–3 router, Groq→APINEX→local failover + circuit breakers, response cache, ledger memo, context builder with hard budgets, LLM planner with structural validation, degraded mode, `/perf` telemetry | `eva/models/*.py`, `eva/runtime/budget.py` |
| 6 — Interfaces | eDEX-style rich CLI (renderer only), headless one-shot runner, avatar agent-bus client (event projector, breaker), edge-tts voice, mood/archetype persona | `eva/ui/*.py`, `eva/avatar/*.py`, `eva/voice/*.py`, `eva/persona/*.py` |
| 7 — Autonomy | SQLite-backed scheduler + reminders (persist across restart), file watcher rules with kill switch, both LOW-risk-gated | `eva/runtime/scheduler.py`, `eva/runtime/watchers.py` |

## 3. Hard rules accepted verbatim (non-negotiable)

- P1 orchestrator is the controller; LLM output is a suggestion.
- P2 executed ≠ achieved; step success only from fresh observation.
- P3 state before action; already-true effects → `SKIPPED_ALREADY_DONE`.
- P4/INV-1..4: cheapest ladder level; vision/OCR only via `vision_gateway` with
  `reason + exhausted_levels`; coordinates only with recorded justification;
  contracts whose verification names L6/L7 are rejected at registration.
- P5 plans are maps; replans bounded, numbered, reason-logged.
- P6 verdict comes only from the completion condition — never step counts.
- P8 memory carries confidence/source/verify hooks; <0.4 never drives action.
- P9 risk engine classifies; no LLM can set or bypass risk; HIGH timeout → cancel.
- P10 idempotency: journal + effect tokens; rerun never duplicates side effects.
- P11 context assembled per decision with hard budgets.
- P12 everything emits bus events; UI/avatar/voice are pure subscribers.

## 4. Autonomous decisions made (where docs left gaps)

1. **Async core** (doc 12 §8 said "sync in Phase 1, async facade in Phase 2") —
   we build the async loop now since Phase 2/3 are real: `asyncio` host, sync
   SQLite, `asyncio.to_thread` for fs. Tests drive async via `asyncio.run`.
2. **CDP transport**: `aiohttp` (already installed) for HTTP + WebSocket to the
   user's Chrome on ports 9222–9224; attach fails honestly if Chrome runs
   without a debug port (`BLOCKED` with precise ask), never silently.
3. **Unified DB**: one `data/eva.db` (WAL) for tasks/checkpoints/journal/
   memory/cache/skills/scheduler/watchers per doc 08 ("single file, WAL").
4. **Deferred honestly**: embeddings/vector search, Mem0 backend, Browser-Use
   layer, MCP adapter, STT, heavy local models — interfaces exist, defaults are
   the budget-safe ones (doc 08 §7, doc 10 "explicitly deferred").
5. **L2 a11y**: Win32 window/child-control tree via ctypes (real, lightweight);
   full UIA tree documented as an upgrade path. L5 OCR = tesseract if on PATH,
   else honest UNKNOWN; L7 = vision endpoint only if configured, else UNKNOWN.
6. **No new dependencies required**: pydantic, psutil, aiohttp, rich, requests,
   pytest, playwright (optional, not imported unless browser Layer A needs it),
   edge-tts, pillow, pyautogui — all already present on this machine.
7. **Completion honesty on UNKNOWN**: any UNKNOWN completion predicate →
   `BLOCKED: verification gap`, never FINISHED (doc 03 §5).
8. **Persona**: mood vector + 13 archetypes from EVA_INTELLIGENT_RESOURCES,
   implemented as a light text-flavor layer only — it never touches control.

## 5. Definition of done for this build — DONE

1. `[x]` `python -m pytest` — **71 passed** (doc-11-derived suite, fake world),
   covering S1/S1b/S1c/S2/S3/S4/S5/S6/S7 + checkpoints, recipes, real
   contracts, scheduler/watchers.
2. `[x]` Live smoke: `python eva.py --run "cpu usage batao" --json` → FINISHED,
   verified, **0 model calls**; `open notepad` twice → second run is a verified
   no-op (0 actions, ~0.07 s); `open whatsapp` → honest BLOCKED ask (debug
   port); `band karo notepad` → verified termination. All on real hardware.
3. `[x]` CLI boots (`python eva_cli.py`), renders plan/step/state/perf, the
   doc-09 command set works, confirmations fail-safe on non-TTY, RSS ≈ 59 MB idle.
4. `[x]` README + BUILD_REPORT written; this plan is checked phase by phase.

### Phase checklist

* [x] Phase 1 — Agent Core (`eva/core/*`, 34+ tests)
* [x] Phase 2 — State + Perception (`eva/perception/*`, live L1/L3 + gated L5–L7)
* [x] Phase 3 — Tools (30 contract tools, risk engine, idempotency, UI lock)
* [x] Phase 4 — Memory (SQLite WAL, envelope, distiller, bounded retrieval)
* [x] Phase 5 — Intelligence (router, context budgets, validated planner, degraded)
* [x] Phase 6 — Interfaces (eDEX CLI, headless, avatar bus, voice, persona)
* [x] Phase 7 — Autonomy (persistent scheduler, watchers, kill switches)
