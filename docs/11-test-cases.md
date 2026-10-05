# 11 — Test Cases & Acceptance Suite

> The v1 WhatsApp failure is our **first engineering test case.** The
> question is never "let's add more AI" — it is *"why did EVA fail, and which
> architectural mechanism makes that failure impossible or detectable now?"*

---

## 0. Autopsy: the 12-step WhatsApp log

What v1 did (abridged):

```text
launch Chrome → focus WhatsApp → wait → focus WhatsApp → Alt+Tab
→ focus Chrome → focus WhatsApp → launch Chrome AGAIN → launch Chrome AGAIN
→ /archive URL → … → ⚠ REPLAN → … → "Completed 12 execution steps."
```

### Failure categories → v2 countermechanisms

| # | Why it failed (v1) | Mechanism in v2 | Doc |
|---|---|---|---|
| 1 | Poor environment discovery | Discovery pass (L1/L3/L4) before any plan; lazy state branches filled | 04 |
| 2 | No reliable Chrome profile identification | `chrome.profiles` tool + semantic memory `AIOU → Profile 11` with `verify_method` | 05, 06 |
| 3 | No persistent state | World-state model with freshness + three-valued predicates | 04 |
| 4 | No WhatsApp UI perception | Ladder L2/L4 (a11y/DOM) predicates: `wa.view`, `wa.login`, `wa.selected_chat` | 04 |
| 5 | Tool verification inadequate | Mandatory contract `verification` field; "function returned" ≠ success | 05, 03 §5 |
| 6 | Planner allowed redundant actions | State-before-action no-op + no-effect-delta rejection + UI lock | 03 §4.1, 05 §4 |
| 7 | Replanning had no failure policy | Bounded replans with reasons, zero-progress counter, step budget | 03 §6 |
| 8 | "Executed" treated as "successful" | VERIFIED/SKIPPED/FAILED/BLOCKED — four outcomes only; P2/P6 | 03 |
| 9 | No task completion condition | Machine-checkable `completion_condition` drives the verdict | 03 §1 |
| 10 | `/archive` assumed to be a valid route | Planner may only emit registered, contract-validated actions | 05 §1 |

**Traceability rule:** every row above maps to at least one test below that
FAILS if the mechanism is removed.

---

## S1 — WhatsApp archived chat (the flagship)

```text
REQUEST : "open whatsapp in aiou profile, saad bhai ka archived chat kholo"
PRE     : Chrome profile AIOU known; WhatsApp logged in; Saad Bhai archived
EXPECT  :
  • completion: chrome.profile=AIOU ∧ url=web.whatsapp.com
                ∧ wa.view=archived ∧ wa.selected_chat="Saad Bhai"
  • redundant actions = 0 (no repeated launches, no focus/Alt+Tab cycling)
  • no screenshot / no vision-model call
  • model calls ≤ 1 (skill) or ≤ 2 (planned run)
  • verdict derives from completion condition, not step count
  • every step shows VERIFIED evidence (level + timestamp)
```

Variants:
- **S1b** already-open/logged-in → 0 actions, 0 calls (no-op report).
- **S1c** QR login screen → `BLOCKED: scan QR`, exactly one user-facing ask,
  no retry storm.
- **S1d** AIOU profile renamed → discovery detects mismatch, memory
  invalidated, task still succeeds, memory self-healed.
- **S1e** crash at step 5 → restart → resume from step 6, no duplicated
  effects, completion still verified.

## S2 — No-op / idempotency

```text
S2a "open chrome" while Chrome open            → SKIPPED_ALREADY_DONE, 0 actions
S2b run task twice (full rerun)                → second run: no duplicate side
                                                 effects (file created once,
                                                 message sent once)
S2c resume from checkpoint re-executes step 3  → effect_token dedup asserts
                                                 single world mutation
```

## S3 — Learning curve (memory)

```text
S3a run WhatsApp workflow 3×:
    run1: N planning calls · run2: fewer · run3: skill path, ≤1 call,
    wall-clock(run3) < wall-clock(run1)   [measured]
S3b intent matches skill with confidence < threshold → falls back to LLM
    planning (negative test: no blind skill execution)
S3c skill's steps start failing → success_rate drops → quarantined →
    next run uses LLM path (no lock-in to broken recipe)
```

## S4 — Risk gating

```text
S4a "delete this folder" (2.4 GB)   → CRITICAL confirm w/ exact size+path;
                                        decline → task CANCELLED, files intact
S4b "message saad bhai 'phir kayaa?'" → HIGH confirm showing recipient+text;
                                        timeout → CANCELLED (fail-safe)
S4c LLM output attempting to self-approve a HIGH action
                                        → rejected: risk set by engine only (P9)
S4d "open downloads"                 → LOW, no confirmation, <300 ms
```

## S5 — Honest reporting & recovery

```text
S5a page never loads (injected failure)  → step FAILED after 2 attempts,
                                           task BLOCKED/FAILED with reason —
                                           NEVER "completed N steps"
S5b 4 consecutive non-verified steps     → forced REPAIR PLAN (reason logged)
S5c 2 failed repairs                     → BLOCKED with full history
S5d same action + same state twice       → attempt #2 max, then escalate
                                           (anti-loop assertion)
```

## S6 — Perception ladder discipline

```text
S6a browser task                         → uses L1/L3/L4 only; assert 0 L5–L7
S6b non-cooperative app dialog           → answered at L2/L5; L7 only if L2/L5
                                           return UNKNOWN twice
S6c vision case (explicit)               → L7 invoked, cost recorded, evidence
                                           stored with TTL policy
S6d all observations in /debug           → show level + observed_at + confidence
S6e vision/coordinate invariants (P7b)   → any structurally-solvable scenario
                                           (S1, S2, S6a, "open notepad") ends
                                           with task counters l5=l6=l7=
                                           coord_actions == 0; a single pixel
                                           call FAILS the run even if the task
                                           completed
S6f unjustified coordinate click         → policy gate REJECTS click(x,y) with
                                           no exhausted-levels justification
                                           (INV-2); same action + justification
                                           accepted
S6g vision request without justification → REJECTED pre-execution (INV-3);
                                           no screenshot enters LLM context
                                           outside Tier 3 (INV-4)
S6h event-driven verify                  → browser.download verified via fs
                                           event + state check: 0 screenshots
                                           during the whole step
```

## S7 — Model router & context economy

```text
S7a "cpu usage" / "open chrome"          → 0 provider calls, <300 ms
S7b typical plan call                    → measured prompt ≤ 4K tokens;
                                           asserts absence of full history,
                                           full memory, full tool list
S7c provider outage mid-task             → failover; task completes or
                                           BLOCKED w/ reason — no hang
S7d same question twice in one task      → 1 API call (ledger memo)
```

## S8 — Resource budget (doc 08)

```text
S8a idle 10 min                          → EVA core RSS ≤ 250 MB, CPU < 2%
S8b active task                          → peak RSS ≤ 600 MB, CPU avg < 15%
S8c free RAM forced < 1.5 GB             → degraded mode engages + CLI banner,
                                           core loop still functions
S8d offline                              → Tier 0/1 local paths work;
                                           novel Tier-2 task → honest BLOCKED
```

## S9 — Interface & event integrity (doc 09)

```text
S9a avatar expression change             → only from bus events (no direct calls)
S9b TASK_FINISHED(success) ⇔ completion verified ⇔ avatar happy ⇔ spoken
    success                              → all four in sync, always
S9c CLI restart mid-task                 → state re-rendered from bus log
S9d voice "open chrome"                  → Tier 0, <2 s, VAD-gated (no
                                           continuous audio)
```

---

## Suite mechanics

```text
Tiers of testing:
  UNIT        contracts, predicates, anti-loop rules, confidence math
  SCENARIO    the S1–S9 suite against a FAKE WORLD harness
              (simulated windows/CDP/a11y — deterministic, CI-able)
  LIVE        same scenarios on the real Windows machine (manual/nightly)
              + resource assertions from S8

Evidence rule: every scenario records the event log + token/RSS counters,
so "passes" means the NUMBERS passed — not vibes.

Definition of Done for v2 (overall):
  1. S1–S9 all green in FAKE WORLD, S1/S4/S8 green LIVE.
  2. Zero P6 violations possible: task verdict can only come from
     completion condition (enforced by code path, asserted by test).
  3. Doc 10 Phase 1–5 exit criteria all checked.
```

---

## Anti-regression watchlist (v1 bugs that must stay dead)

- [ ] Chrome launched twice for one task → `launch_count == 1` assertion.
- [ ] Focus/Alt+Tab cycling → `ui_lock` telemetry shows no redundant switches.
- [ ] `/archive`-style invalid navigation → planner emits only registered tools.
- [ ] "Completed 12 execution steps" on failed task → impossible by code path.
- [ ] Vision call for a question L1/L3 could answer → ladder-policy assertion.
- [ ] Screenshot-first creep: any task whose counters show L6/L7 or
      coordinate actions while a structural path existed → S6e fails.
- [ ] "Click where I think it is" → coordinate action with no recorded
      structural-UNKNOWN justification → gate rejects it (S6f).
