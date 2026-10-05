# 10 — Roadmap: Build Phases & Exit Criteria

> Discipline: **do NOT start with memory, VRM, voice, or 50 tools.**
> Each phase must pass its exit criteria before the next begins. Phases are
> ordered so the WhatsApp scenario (doc 11) passes as early as possible —
> the core loop is proven before anything is layered on top.

---

## Phase map

```text
Phase 1  Agent Core            Task · Plan · Execute · Verify · Recover
Phase 2  State + Perception    OS → app → browser → a11y/UI → vision fallback
Phase 3  Tool Ecosystem        OS · Files · Browser · Terminal · Apps · Documents
Phase 4  Memory                working · episodic · semantic · procedural · skills
Phase 5  Intelligence          routing · local models · context compression ·
                                caching · parallelism · checkpoints polish
Phase 6  Voice + VRM           STT · TTS · lip sync · expressions · events
Phase 7  Autonomy              scheduled tasks · background agents · watchers
```

Dependency order is strict left-to-right except Phase 6, which depends only
on Phases 1–2 (events exist) and can start once 1–2 pass.

---

## Phase 1 — Agent Core

**Goal:** the control model exists and cannot lie about success.

Deliverables:
- Task object + completion condition (machine-checkable predicates)
- Planner (structure-validated plans, plan-before-execute)
- Step lifecycle: PRECHECK → EXECUTE → VERIFY → VERIFIED/FAILED/BLOCKED/SKIPPED
- State-before-action no-op check (P3), bounded recovery ladder + anti-loop
  rules (doc 03 §6), honest final reporting (P6)
- Verifier component with evidence capture
- Checkpoint-after-every-verified-step + resume
- Minimal event bus + CLI rendering of events (logic-free)

**Exit criteria:**
- [ ] WhatsApp scenario (doc 11 §S1) passes with a *mocked* world: 0
      redundant actions, completion condition drives the verdict.
- [ ] A deliberately failing task reports FAILED/BLOCKED — never "completed N steps".
- [ ] Kill the process mid-task → restart → resume from first unverified step.
- [ ] Unit tests for anti-loop rules and completion-condition evaluation.

---

## Phase 2 — State + Perception

**Goal:** the ladder is real; vision is the exception.

Deliverables:
- World-state model with freshness + three-valued predicates (doc 04)
- L1 (processes/windows/resources), L3 (Chrome CDP), L4 (DOM) implemented;
  L2 (accessibility tree) for key apps; L5/L6 stubs behind the on-demand gate
- Ladder-selection policy + observation cache + state diffing
- `UNKNOWN` propagation (never silently true/false)

**Exit criteria:**
- [ ] `open whatsapp web` when already open → **0 actions, 0 model calls** on
      real hardware.
- [ ] QR-login screen detected → `BLOCKED: scan QR` (no retries, no success claim).
- [ ] No screenshot/vision call occurs across the whole doc-11 suite except
      the explicitly vision-marked case.
- [ ] Policy gate live: unjustified `click(x, y)` and unjustified L5/L6/L7
      requests rejected pre-execution (doc 04 hard invariants INV-1..3);
      S6e counters (`l5/l6/l7/coord_actions == 0`) asserted structurally
      solvable scenarios.
- [ ] Observation cache + invalidate-on-action + event-driven verify
      demonstrated: download task verified with 0 screenshots (S6h).
- [ ] `/debug` shows level + timestamp + confidence for every observation.

---

## Phase 3 — Tool Ecosystem

**Goal:** contract-driven tools, not loose functions.

Deliverables:
- Tool registry with contract validation (no contract → not registrable)
- First inventory: OS · Browser (CDP-first) · Files · Terminal (allowlisted)
  · Apps · Documents (per doc 05 §2)
- Risk engine (4 levels, dynamic overrides, confirm UX)
- Idempotency classes + effect-token journal
- UI lock for mouse/keyboard/focus tools

**Exit criteria:**
- [ ] All tools in inventory pass contract tests (preconditions, effects,
      verification, risk, idempotency).
- [ ] CRITICAL delete confirmation shows real size/path; declining cancels cleanly.
- [ ] Same message send attempted twice (simulated resume) → exactly one send.
- [ ] Focus-fight test: scripted 10-step UI task never has two ui_lock holders
      overlapping (asserted by lock telemetry).

---

## Phase 4 — Memory

**Goal:** the agent remembers correctly and forgets safely.

Deliverables:
- SQLite store (doc 06 §3 schema) + working/episodic/semantic/profile writers
- Confidence envelope, decay, verification hooks (invalidate on discovery mismatch)
- Skill distiller (≥2 similar verified runs → skill) + skill fast path in router
- Bounded retrieval for the context builder (800-token slice)

**Exit criteria:**
- [ ] WhatsApp workflow: 3rd execution runs as a skill with **≤ 1 model call**
      and fewer wall-clock seconds than run #1 (measured).
- [ ] Renaming the AIOU Chrome profile invalidates the stale fact on next use
      and the task still succeeds (self-healing test).
- [ ] Working memory is empty after task end.
- [ ] Low-confidence inference memory cannot trigger an action without a
      confirm-step (negative test).

---

## Phase 5 — Intelligence Optimization

**Goal:** cheap, fast, and still correct.

Deliverables:
- Full task router (Tier 0–2 + escalation/de-escalation rules)
- Deterministic fast path for common commands (0 LLM)
- Context builder with hard budgets + ledger memo (no repeat questions)
- Provider failover + response cache + circuit breakers
- Parallel-safe step execution for non-UI steps (gated by resource budget)
- Telemetry: `/perf` (RSS, CPU, tokens, calls, cache hits) + budget alarms
- Degraded mode (doc 08 §5)

**Exit criteria:**
- [ ] Fast-path commands: 0 provider calls, < 300 ms.
- [ ] Ordinary task model-call count ≤ budget (doc 08 §4); instrumented.
- [ ] Pull the network mid-task → failover/degraded behavior, honest report.
- [ ] Free-RAM-low simulation → degraded mode engages and is shown in CLI.
- [ ] Idle RSS within budget (≤ 250 MB) measured over 10 minutes.

---

## Phase 6 — Voice + VRM

**Goal:** EVA feels alive — without putting intelligence in the interface.

Deliverables:
- Event-bus-driven avatar states (doc 09 §4) + lip sync + expressions
- Voice: VAD → STT → router → TTS, voice-answerable confirmations
- eDEX CLI final polish (progress tables, /explain, /state, /perf)

**Exit criteria:**
- [ ] Every avatar expression maps to a bus event (no direct calls; code review
      + test).
- [ ] Speak "open chrome" → Tier 0, avatar reacts, < 2 s total.
- [ ] HIGH-risk voice confirmation works; silence/timeout cancels (fail-safe).
- [ ] Interface parity: same task via CLI and voice → same core behavior.

---

## Phase 7 — Autonomy

**Goal:** OS-companion behavior — EVA acts between conversations.

Deliverables:
- Scheduler (reminders, cron-like jobs), background task slots (resource-gated)
- Watchers (file/window/event triggers → rule → action)
- Event-driven rules engine on the bus
- Long-running workflows with checkpoints across sessions

**Exit criteria:**
- [ ] "Remind me Tuesday to follow up with the restaurant" persists across
      restarts and fires (no LLM conversation held open).
- [ ] Background tasks never violate UI lock or CPU/RAM budget (stress test).
- [ ] Watcher rule (e.g., new file in Downloads) triggers a LOW-risk action
      and logs an event — with kill switch.

---

## Explicitly deferred (not in any phase until a real need appears)

| Item | Deferred because |
|---|---|
| Embeddings / vector search | SQLite FTS5 suffices until a failing query proves otherwise (doc 06) |
| Graph DB | `facts` table + SQL joins first (doc 06 §3) |
| Multi-monitor/multi-OS support | Windows-first; abstraction layer only when needed |
| Heavy local models (7B+) | Violates doc 08 RAM/latency |
| Web dashboard | CLI + avatar are the primary surfaces |
| Plugin marketplace / remote skills | Contract + risk story must mature first |

---

## What Phase N must never do

- Add a subsystem that has no test in doc 11.
- Add a dependency that breaks doc 08.
- Mark a milestone "done" while its exit checkboxes are open.
- Report task success without a verified completion condition (P6) —
  at any phase, this is the one bug that is never acceptable.
