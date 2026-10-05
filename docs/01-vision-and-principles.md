# 01 — Vision & Design Principles

---

## 1. What EVA is

> **EVA is a resource-efficient personal agentic OS for one human's computer.**
>
> It understands the machine as a **stateful environment**, uses the **cheapest
> reliable perception/action method available**, **learns reusable workflows**
> from experience, and **escalates to expensive reasoning/vision only when
> necessary**.

Not a chatbot with Python functions. Not a demo that works once on a lucky
run. A dependable daily driver that Sohaib can point at real work.

---

## 2. The experience target

When EVA is working correctly, the user should observe:

| Observable | Meaning underneath |
|---|---|
| Instant response to simple commands | Deterministic fast-path, no LLM call for `open Chrome` |
| No repeated/duplicate actions | State is checked *before* acting, idempotency enforced |
| Honest progress reporting | Steps report `VERIFIED`, not merely `EXECUTED` |
| It stops and asks instead of guessing | Risk engine + BLOCKED state, no blind retries |
| It gets faster and cheaper over time | Procedural memory turns LLM-heavy flows into scripts |
| It never "forgets" a half-finished job | Task checkpoints + resumable state |
| It feels like an OS companion, not a webpage | Event bus drives CLI, avatar, voice from one source |

**Anti-target:** a system that looks impressive in a scripted demo and then
loops uselessly on a real screen. That is exactly the v1 WhatsApp log.

---

## 3. The twelve hard principles

These are non-negotiable. Any design or code that violates one is wrong by
definition, regardless of how well it works in testing.

### P1 — The orchestrator is the controller
The LLM proposes; the system disposes. The LLM never directly executes
anything. Its output is a *suggestion* which must pass precondition checks,
risk checks, and state relevance checks before any tool runs.

### P2 — Executed ≠ Succeeded
Two distinct events:
```
ACTION_EXECUTED     — the function ran and returned
ACTION_ACHIEVED     — the intended world effect was observed by perception
```
Only the second marks a step complete. A step that is executed but not
achieved is a **failed step**, full stop.

### P3 — State before action
Every action is preceded by a state query: *is this already true?* If the
desired state already exists, the action is skipped (`SKIPPED_ALREADY_DONE`),
not repeated. This alone kills the v1 "launch Chrome → launch Chrome again"
class of bug.

### P4 — Verify with the cheapest sufficient signal
Verification uses the same ladder as perception (doc 04): process list before
screenshot, URL before vision model. Never spend a vision-model call to
confirm what a window title already proves.

### P5 — Plans are maps, not contracts
Plan BEFORE execution (no per-step improv), but a plan is re-derived whenever
reality contradicts it. Blocked ≠ failed ≠ replan-everything: each has a
distinct recovery policy (doc 03 §6).

### P6 — Fail loudly, never fake completion
`Completed 12 execution steps` while the goal is unmet is a lie. Task status
is derived from the **completion condition**, never from step count. If the
completion condition is not satisfied, the task result is `FAILED` or
`BLOCKED`, and the report says so.

### P7 — Cheapest method first (perception AND reasoning)
Two parallel ladders:
- **Perception ladder** (doc 04): internal state → OS APIs → a11y tree → app
  APIs → DOM/CDP → OCR → screenshot → vision LLM. Climb only as far as needed.
- **Reasoning ladder** (doc 07): deterministic code → small local model →
  strong API model → vision model. Climb only as far as needed.

### P7b — Vision is escalation, never the default interaction
This is the invariant EVA was *built* to protect (doc 04 "Hard
invariants"): **vision-first interaction is forbidden unless structured
perception is unavailable or insufficient for the current decision**, and
**coordinate-based mouse control is last-resort, not default**. The default
loop is `ACT → EVENT → STATE UPDATE → VERIFY` against a cached observation —
not `screenshot → vision → click(x,y) → repeat`. A "computer-use" loop that
screenshots every step is the Open-Claw anti-pattern: it is slow, fuzzy, and
destroys the token economics that make EVA viable (doc 08). EVA is an
intelligent system that *understands* the machine and operates it through
structured APIs — not "another AI that moves a mouse."

### P8 — Memory must be able to be wrong
Every memory carries `confidence`, `source`, `usage_count`, `last_verified`.
Low-confidence memories inform but never drive autonomous action. Memories
that can be verified cheaply are re-verified; stale ones decay or are
invalidated. Memory is a hypothesis store, not gospel.

### P9 — Dangerous actions are gated by a risk engine
LOW → act. MEDIUM → act + log. HIGH → confirm with the user. CRITICAL →
explicit confirmation immediately before execution, with the concrete
consequences stated ("will permanently delete 2.4 GB from X"). No LLM can
bypass the gate.

### P10 — Idempotency by default
Creating files, sending messages, downloading, submitting, deleting: all
operations check whether the intended effect already exists before acting.
Re-running a task must be safe.

### P11 — Only necessary context reaches the LLM
Context is *assembled per decision* (doc 07): current task + relevant state +
relevant memory + currently-allowed tools + latest observation. Never the
whole conversation, whole memory, whole screen, whole tool list.

### P12 — Everything emits events
Every meaningful transition (task start, tool start/finish/fail, question
asked, verification result) goes on the event bus. CLI, VRM avatar, voice,
logs, telemetry all *subscribe*. No subsystem polls another, and the avatar
never needs an LLM to know it should look concerned.

---

## 4. Non-goals (things we are explicitly NOT building)

| Non-goal | Why |
|---|---|
| Multi-user / SaaS deployment | EVA is a *personal* agent for one machine and one user profile |
| Giant vector DB + knowledge graph on day one | Start with SQLite; add embeddings/graph only when a real query demands it (doc 06 §7) |
| 50 tools at launch | Tool quality + verification >> tool count. Start with the tools the test cases need |
| Always-on screen recording / continuous vision | Violates P7/P7b and the resource budget (doc 08) |
| Screenshot-first "computer use" (vision → coordinates → click → repeat) | Violates P7b (doc 04 hard invariants) — exactly the v1/Open-Claw anti-pattern EVA exists to avoid |
| Full autonomy without HITL | Risk engine makes human-in-the-loop a permanent feature, not a bug |
| One monolithic "do everything" LLM prompt | That is v1's failure mode |
| Replacing the user's judgment on HIGH/CRITICAL actions | The gate exists precisely because the LLM's confidence is not evidence |

---

## 5. Success criteria for v2 (how we know it worked)

Quantitative — measured by the regression suite (doc 11):

1. **WhatsApp scenario**: passes completion condition, **0 redundant actions**
   (no repeated launches, no pointless focus/Alt+Tab cycling).
2. **Re-report honesty**: when blocked (e.g., QR login screen), task reports
   `BLOCKED` with a precise user-facing ask — never `Completed`.
3. **Repeat-task speedup**: the 2nd execution of a learned workflow uses
   ≤ 1 LLM call (or 0) versus N calls on the 1st.
4. **Token budget** per ordinary task within doc 08 limits.
5. **RAM footprint** of the EVA core process within doc 08 limits on a normal
   8–16 GB machine.

Qualitative:

6. Simple commands (`open Chrome`, `what's my CPU usage`) never touch an LLM.
7. The user can always see *what EVA believes the state is* and *why it chose
   the last action* in the CLI.

---

## 6. Vocabulary (used consistently across all docs)

| Term | Definition |
|---|---|
| **Task** | A user request turned into a structured object with goal, constraints and completion condition |
| **Plan** | Ordered list of steps derived before execution; revisable |
| **Step** | One planned unit; ends in `VERIFIED` / `FAILED` / `BLOCKED` / `SKIPPED` |
| **Action** | A single tool invocation with a contract (doc 05) |
| **State** | The system's model of world truth (doc 04); updated only by perception |
| **Observation** | One perception result, tagged with its ladder level and freshness |
| **Verifier** | Component that checks an action's intended effect against fresh observation |
| **Recovery** | Policy-driven response to FAILED/BLOCKED (retry, alternative, re-perceive, ask user) |
| **Skill** | A learned, executable workflow distilled from verified successful task runs |
