# 12 — Technical Plan: Stack Selection & Project Skeleton

> **Input:** Sohaib's capability-donor shortlist (OpenHands SDK, Mem0, Letta,
> Browser Use, Playwright/CDP, open-browser-use, Pydantic, MCP + concepts:
> skills, tool registry, cost metadata, context compression, observation
> cache, eval/replay engines, deterministic fallback).
>
> **Method:** every candidate is judged against doc 08 (resource budget),
> doc 05 (tool contracts), doc 06 (memory design), license hygiene, and the
> capability-donor principle: **existing projects donate capabilities — none
> of them becomes EVA's brain. EVA's orchestrator stays custom (doc 03).**

---

## 1. The stack principle (agreed)

```text
WRONG:  EVA = Letta + Mem0 + OpenHands + LangGraph + CrewAI + BrowserUse
        + 15 MCP servers                       → 100× more confusing

RIGHT:  EVA CORE (custom orchestrator, state, verifier, risk, contracts)
          ├── Mem0-ideas / optional backend    → memory (behind interface)
          ├── Playwright/CDP                    → deterministic browser hands
          ├── Browser Use (optional, Layer B)   → hard-site escalation
          ├── MCP (adapter)                     → external tools, contract-gated
          └── Pydantic                          → every schema
```

**No framework holds authority. Only EVA's orchestrator does (P1).**

---

## 2. Verdict table

| Candidate | Verdict | Role in EVA | License | Budget (doc 08) check |
|---|---|---|---|---|
| **Custom EVA Orchestrator** | ✅ **BUILD** | Doc 03 loop — the controller. Nothing replaces it | — | fits (single process) |
| **Pydantic v2** | ✅ **ADOPT** | Task/Step/Predicate/ToolContract schemas; malformed plan/tool = rejected at parse | MIT | zero cost; already in env (2.13.5) |
| **Playwright / CDP** | ✅ **ADOPT (Phase 3)** | Browser Layer A: deterministic hands — `connect_over_cdp` to the **user's real Chrome profile** (logged-in sessions incl. WhatsApp) | Apache-2.0 | OK if we attach to existing Chrome — **no bundled browser download**; dep footprint checked at Phase 3 |
| **Browser Use** | 🔶 **OPTIONAL plugin (Phase 3+, Layer B)** | Escalation only: "DOM/API can't solve this site" → agent reasoning over the page. Behind `browser_tool` interface; never owns EVA | MIT | ⚠ heavy dep tree; known transitive AGPL (pymupdf via markdown-pdf) → install with optional extras pinned, or skip; needs model calls = Tier 2 cost, so gated by task router |
| **open-browser-use** | 🔸 **STUDY ONLY** | Overlaps with what CDP-attach already gives us; re-evaluate only if Playwright-attach fails on a real need | check at adoption | n/a |
| **Mem0** | 🔶 **IDEAS + OPTIONAL BACKEND (Phase 4 decision gate)** | See §3 — not the default | Apache-2.0 | ⚠ fails *by default*: every `add()` fires an LLM extraction call (token cost), self-host stack ≈ FastAPI + Postgres + vector store (RAM ≫ 250 MB idle). Default stays SQLite FTS5 (doc 06) |
| **Letta** | 🔸 **STUDY ONLY** | Stateful-agent/context-management ideas | **source-available (not OSI)** → no dependency | n/a |
| **OpenHands SDK** | 🔸 **PATTERN DONOR** | Event/tool/workspace/conversation separation validates doc 02 | MIT | not imported |
| **MCP** | 🔶 **ADAPTER (Phase 3+)** | External tools enter **only** through contract translation (doc 05 §2); weak contracts → LOW-risk only, `WEAK_VERIFY` flag | MIT | adapter off until used |
| **LangGraph / CrewAI / 15-framework stack** | ❌ **REJECTED** | Violates the stack principle + doc 01 non-goals | — | — |

Legend: ✅ ships as dependency · 🔶 optional/gated · 🔸 read & learn only · ❌ rejected.

---

## 3. Mem0 — the careful verdict

Mem0 maps cleanly onto doc 06's goals (working vs long-term separation,
retrieval instead of full-history prompts). But against our own rules:

| Mem0 behavior | Conflict | Resolution |
|---|---|---|
| `memory.add()` → one LLM extraction call per stored turn | Token budget (doc 08 §4) — would fire on every interaction | Writes only on **task boundaries** and only for distilled facts — never per message |
| Default self-host = server + Postgres + vector store | RAM envelope (doc 08 §2: 250 MB idle) | Not acceptable as a *required* process |
| Retrieval is generic (semantic + BM25) | Doc 06 wants confidence/decay/verify hooks | Our confidence envelope must sit **on top** regardless |

**Decision:** implement `MemoryBackend` interface now (Phase 4) with two
implementations:

```text
SqliteFtsBackend   — DEFAULT (doc 06 schema, confidence envelope native)
Mem0Backend        — OPTIONAL (lazy import, opt-in config, benchmarked
                      against doc 08 before it may become default)
```

Mem0 is adopted **as a candidate backend and as idea source** (working/long-
term split, BM25+entity+temporal retrieval), not as EVA's memory core on
day one. Decision gate: Phase 4 exit must show measured numbers both ways.

---

## 4. Concepts accepted from the shortlist (all map into existing docs)

| Concept | Where it lands | Change made |
|---|---|---|
| **Skills taxonomy** (`skills/whatsapp/…`) | doc 06 §5 | skill directories organized by domain; skills = verified procedures only |
| **Tool registry** (not 100 tools in every prompt) | doc 05 + doc 07 §3 | registry filters per step — already specified; registry now formal |
| **Tool cost metadata** | doc 05 contract | `cost_hint {tokens, latency_ms, compute}` **required field** — router optimizes cheapest-reliable-path |
| **Context compression** (6 verbose steps → task-state summary) | doc 07 §3 (task ledger) | ledger keeps last-K raw results, older → compact summary |
| **Observation cache + invalidation** | doc 04 §3 | confirmed as specced (action → invalidate branch → re-observe) |
| **Deterministic fallback** (`application_registry`: VS Code → path) | doc 07 Tier 0 | new tool `apps.launch_registry` — "what not how" |
| **Evaluation engine** (TEST-001…004) | doc 11 | *is* the doc-11 suite; S1–S9 = runnable tests |
| **Replay engine** (task/plan/actions/observations/errors → replay) | new: doc 11 §suite mechanics | event log + tool journal = replay fixture; **v1's 12-step WhatsApp log = fixture #1** |
| **Two-layer browser** (Playwright deterministic / Browser-Use escalation) | doc 05 §2 + doc 04 ladder | `browser.*` tools Layer A; Layer B behind router escalation |
| **MCP as adapter, not core** | doc 05 §2 | confirmed as specced |
| **Directory structure** | §6 below | merged: user's layout + doc 02 data-ownership |

---

## 5. Dependency list

### Now (Phase 1 — core, fake-world harness)

```text
pydantic >= 2.13        MIT     already installed  ✅
pytest                  MIT     dev/test only      ✅ (installed for suite)
stdlib only otherwise   —       sqlite3, dataclasses, enum, json, time
```

**Zero runtime third-party deps in Phase 1.** The control model must be
provable without touching the machine or the network.

### Phase 2–3 (add when their phase starts)

```text
(playwright, CDP attach)   Phase 3 — after budget check (doc 08 §6 gate)
(browser-use, optional)    Phase 3+ Layer B — optional extra, pinned deps
(mem0, optional)           Phase 4 — behind MemoryBackend, benchmarked
(MCP client)               Phase 3+ — adapter, contract translation
```

Every addition must re-answer doc 08 §6: RAM? CPU? offline? license?

---

## 6. Project skeleton

Merged from the shortlist's layout and doc 02 data ownership:

```text
eva_v2/
├── README.md
├── docs/                     # 01–12 (this set)
├── eva/                      # the package
│   ├── core/                 # ← PHASE 1 (implemented now)
│   │   ├── task.py           #   Task/Step/Plan/CompletionCondition (pydantic)
│   │   ├── state.py          #   WorldState, Predicate, ternary eval, Observation
│   │   ├── events.py         #   Event + EventBus (replay log)
│   │   ├── contracts.py      #   ToolContract + ToolRegistry (validation)
│   │   ├── risk.py           #   RiskLevel + RiskGate + confirmation interface
│   │   ├── recovery.py       #   anti-loop policy, bounded retries, decisions
│   │   ├── checkpoint.py     #   SQLite checkpoints + effect journal (idempotency)
│   │   └── orchestrator.py   #   THE controller: precheck→gate→execute→verify→checkpoint
│   ├── runtime/              # Phase 2: state manager, async fan-out bus, schedulers
│   ├── memory/               # Phase 4: backends (sqlite default, mem0 optional), distiller
│   ├── tools/                # Phase 3: os, files, browser, terminal, apps, documents
│   ├── skills/               # Phase 4: learned procedures (domain-organized)
│   ├── models/               # Phase 5: task router, providers, context builder
│   ├── perception/           # Phase 2: L1–L7 ladder implementations
│   ├── voice/                # Phase 6
│   ├── avatar/               # Phase 6 (event subscribers)
│   └── ui/edex/              # Phase 1 CLI renderer / Phase 6 polish
├── tests/                    # ← PHASE 1: the doc-11 suite, fake-world harness
│   ├── conftest.py
│   ├── fakes.py              #   FakeWorld + tool handlers + plan builders
│   ├── test_contracts.py
│   ├── test_task_state.py
│   ├── test_orchestrator_whatsapp.py   # S1/S1b/S1c
│   ├── test_recovery.py                 # S5 (honest failure, anti-loop, budget)
│   ├── test_risk_idempotency.py         # S4 + S2/S2c (effect_token, resume)
│   └── test_checkpoint.py               # crash → resume, no re-execution
└── (eva_v2.py, config files as needed later)
```

---

## 7. Priority tiers → roadmap mapping

The shortlist's Tier 1–4 restated against doc 10 phases (no new phases):

| Shortlist tier | Maps to | Notes |
|---|---|---|
| Tier 1: orchestrator, state, registry, Pydantic, verifier, recovery, checkpoints, OS tools | **Phases 1–3** | Tier 1 items 1–7 = Phase 1 exactly; deterministic OS tools = Phase 3 start |
| Tier 2: Mem0, skill memory, Browser Use + Playwright, MCP, context builder, router, observation cache | **Phases 4–5** (+3 for browser/MCP) | Mem0 gated by Phase 4 decision |
| Tier 3: background, scheduler, event bus, voice, VRM, proactive | **Phases 6–7** (bus events already in Phase 1) | as planned |
| Tier 4: self-generated skills, multi-agent, replay/debug, self-eval | **Phase 7+ / deferred** | replay engine's minimal form (event log) ships in Phase 1 because tests need it |

Nothing in the shortlist forces a reorder — it *confirms* doc 10's order and
adds concreteness (Playwright instead of "browser tools", Mem0 as candidate
instead of building retrieval blind).

---

## 8. Phase 1 — staging decisions (honest deviations, logged here)

| Design intent (docs) | Phase 1 reality | Why |
|---|---|---|
| asyncio host (doc 02 §4) | **synchronous core** now | Phase 1 runs against a fake world; async facade lands in Phase 2 with real I/O (CDP, subprocess). Loop logic identical either way |
| Event bus async fan-out (doc 09 §3) | **sync dispatch + bounded replay log** | real consumers (CLI/avatar) arrive Phase 1-end/6; log exists now because the replay engine & tests need it |
| L2–L7 perception (doc 04) | **single `Perception.snapshot()` seam** | ladder implementations are Phase 2; the *contract* (ternary answers with freshness) is proven now |
| Planner = Tier-2 LLM (doc 07) | **`Planner` protocol + scripted planners in tests** | LLM comes Phase 5; orchestration must be testable without it — that's the whole point |

Each row: staging, not scope-cut. The doc-11 assertions must hold unchanged
when the real implementations drop in.

---

## 9. Phase 1 definition of done (what I'm building now)

- [x] This doc (stack verdicts + skeleton)
- [x] `eva/core/` modules per §6 with pydantic contracts
      (task, state, events, contracts, risk, recovery, checkpoint, orchestrator)
- [x] Fake-world harness + doc-11 subset runnable:
      S1 (0 redundant actions), S1b (no-op + partial-skip), S1c (QR → BLOCKED),
      S5a (honest FAILED, never "completed N steps"), S5 anti-loop/budget,
      S4 risk gate, S2c effect-token dedup, checkpoint crash-resume
- [x] `python -m pytest` green from `eva_v2/` — **50 passed, 1 skipped** (1 Reality Bridge skip: notepad already open, no-op path covered by S1b)
- [x] README updated (done)

## 9b. Frontend wiring (option A — connect, don't move)

- [x] `eva/adapters/engine.py` — `EvaV2Engine`, JarvisEngine-compatible façade
      (`process_goal` / `log` / `state` / `memory.data` / `agent_loop` hook)
- [x] `eva/adapters/perception.py` — real perception starter: lazy filesystem +
      L1 system facts (Chrome/a11y rungs = Phase 2)
- [x] `eva/adapters/tool_specs.py` — real contracts with **arg-bound
      `effects_for(args)`** (registry + orchestrator extended)
- [x] `eva/adapters/procedures.py` — Tier-0 deterministic procedures
      (status query, create folder, folder+status combo) — **0 model calls**
- [x] `eva/adapters/llm_planner.py` — Tier-2 planner (router → JSON →
      structural validation; honest `PlannerError` on any failure)
- [x] `eva_cli.py` + `eva_desktop_pet.py` — one-line import swap each;
      v2 bus events forwarded to `log()` so CLI phases + avatar still light up
- [x] **P9 confirm seam closed** — `EvaV2Engine(confirm=...)` → orchestrator;
      `eva_cli` renders the y/n panel (live paused during the ask),
      non-TTY → fail-safe **cancel** (never hangs, never silent-approves);
      3 engine-level tests (decline/approve/missing)
- [x] **Headless one-shot mode** — `python eva_cli.py --run "<goal>"` runs one
      goal without the TUI, prints `{status, verified, message, steps, provider}`
      JSON, exit 0 iff completed/success → the frontend is scriptable/e2e-able
- [x] Verification: 50/50 tests (49 passed + 1 skipped RealMachine no-op) · `import eva_cli` + engine construct OK ·
      `import eva_desktop_pet` OK · frontend e2e (`process_goal("create folder …")`
      → completed + verified history + real dir on disk, exit 0) ·
      **live API path**: `--run "hello"` → `status=success, provider=Groq
      Neural Engine`, exit 0 (real .env key, not a mock)
