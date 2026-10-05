# 03 — Orchestrator & Task Model

> **This is the heart of EVA.** Not the LLM, not the VRM, not the CLI.

The Orchestrator is EVA's executive system. Its permanent question list:

```text
What did the user ask?        What is the goal?
What has already happened?    What is the current state?
What should happen next?      Did the previous action WORK?
What if it failed?            How will I know the goal is actually reached?
```

---

## 1. From text to Task object

Every request becomes a **Task** before anything is planned or executed:

```jsonc
Task {
  id: "tsk_20261004_001",
  created_at, source: "cli" | "voice" | "schedule" | "event",
  user_request: "open whatsapp in aiou profile, open Saad Bhai archived chat",

  goal: "Saad Bhai archived conversation visible on screen",
  constraints: [                    // extracted, never invented
    "use the Chrome profile containing the AIOU Gmail account",
    "do not send any message",
  ],
  priority: "normal",
  risk_level: "low",                 // task-level ceiling; steps may be higher

  completion_condition: {            // MACHINE-CHECKABLE, not prose
    all_of: [
      { observe: "chrome.active_profile_matches", value: "AIOU*" },
      { observe: "page.url_matches",  value: "*web.whatsapp.com*" },
      { observe: "wa.view",           value: "archived" },
      { observe: "wa.selected_chat",  value: "Saad Bhai" },
    ]
  },

  context:   { ...relevant facts, pointers to memory, not full history },
  state:     { ...live, owned by State Manager },
  plan:      [ Step, Step, ... ],    // filled at PLANNING time
  current_step: 0,
  evidence:  [ ...observations proving each completed step ],
  failures:  [ ...attempt records with reasons ],
  checkpoint: { verified_steps: n, snapshot_ref: ... },
  status: "INTENT" | "DISCOVERING" | "PLANNING" | "EXECUTING"
        | "BLOCKED" | "RECOVERING" | "FINISHED" | "FAILED" | "CANCELLED"
}
```

**The completion condition is the contract.** The Task Manager extracts it at
intent time (LLM at the right tier, validated structurally). If it cannot be
expressed as checkable predicates, the task is clarified with the user rather
than run blind.

> **Task status is derived ONLY from the completion condition — never from
> how many steps ran.** (`Completed 12 execution steps` is not a result.)

---

## 2. Planning happens BEFORE execution

```text
BAD  (v1):  think → act → think → act → think → act      (improv)
GOOD (v2):  request → memory → discovery → PLAN → execute the plan
```

The plan is produced in one shot (right-tier LLM + structural validation) and
looks like:

```text
PLAN — "Saad Bhai archived chat"
1. Locate Chrome profile whose identity matches AIOU email      [LOW]
2. Ensure WhatsApp Web open in that profile                     [LOW]
   └ expect: page.url = web.whatsapp.com, wa.login = true
3. Open Archived folder                                         [LOW]
   └ expect: wa.view = archived
4. Search contact "Saad Bhai"                                   [LOW]
   └ expect: search results contain exact match
5. Open conversation                                            [LOW]
   └ expect: wa.selected_chat = "Saad Bhai"
6. Verify completion condition → finish                         [LOW]
```

Properties of a valid plan (structurally enforced — invalid plans are
rejected before execution):
- Every step names **one** action candidate and its **expected effect**.
- Every step has a **verification predicate** (what observation proves it).
- Steps that depend on prior steps say so; independent steps are marked
  **parallel-safe** (only non-UI steps may ever be parallel).
- No step is redundant with an already-satisfied state predicate (checked
  against the discovery snapshot).
- Total risk of the plan ≤ task risk ceiling; otherwise re-route through
  the risk gate.

**Plans are maps, not contracts.** Execution may revise the tail of the plan
whenever state contradicts it — but only the orchestrator revises it, and
every revision is recorded as an event with a reason.

---

## 3. Step lifecycle

```text
                    ┌────────────────────────────────────────┐
                    │              STEP LIFECYCLE             │
                    └────────────────────────────────────────┘

   PENDING ──► PRECHECK ──► EXECUTE ──► VERIFY ──► VERIFIED ──► next step
                   │           │          │
                   │           │          └─ NOT_VERIED ──► RECOVERY
                   │           └─ ERROR ───────────────────► RECOVERY
                   └─ precondition false / state already true
                        │
                        ├─ state ALREADY satisfies step → SKIPPED_ALREADY_DONE (counts as verified)
                        └─ precondition cannot be met  → BLOCKED
```

Step record:

```jsonc
Step {
  n: 2, intent: "open WhatsApp Web in AIOU profile",
  action: { tool: "chrome.open_profile", args: {...} },
  preconditions:  [ "chrome.binary_found", "profile.AIOU_found" ],
  expected_effect:{ "page.url_matches": "*web.whatsapp.com*" },
  verification:   { method: "cdp.url", timeout_s: 15 },   // cheapest sufficient (doc 04)
  risk: "low",
  status: PENDING|PRECHECK|EXECUTING|VERIFYING|VERIFIED|FAILED|BLOCKED|SKIPPED,
  attempts: 0, max_attempts: 2,
  evidence: [ { at: t, level: "L4", data: {...} } ],
  timings: { started, ended }, cost: { tokens, ms }
}
```

Step outcomes — exactly four, no fifth "kind of done":

| Outcome | Meaning |
|---|---|
| `VERIFIED` | expected effect observed |
| `SKIPPED_ALREADY_DONE` | state already satisfied precondition of success (P3) |
| `BLOCKED` | cannot proceed; needs external change (user, another app, login) |
| `FAILED` | attempted, effect not observed, retries exhausted |

---

## 4. The execution loop (pseudocode)

```python
async def run(task):
    task.status = DISCOVERING
    state = await perception.snapshot(task)          # cheap ladder first
    if task.completion_condition.satisfied(state):
        task.status = FINISHED; return               # nothing to do at all

    if not task.plan:
        task.status = PLANNING
        task.plan = await planner.propose(task, state, memory.relevant(task))
        validate_structure(task.plan)                # reject malformed plans
    task.status = EXECUTING

    while (step := task.next_step()) and not cancelled:
        emit(TASK_STEP_START, step)

        # --- P3: state before action ---
        obs = await perception.query(step.preconditions, state)
        if step.expected_effect.already_true(obs):
            step.status = SKIPPED_ALREADY_DONE; record(step, obs); continue
        if not step.preconditions.satisfied_by(obs):
            step.status = BLOCKED; record(step, obs)
            await recovery.on_blocked(task, step); continue        # §6

        # --- P1: risk gate (no LLM bypass) ---
        if risk.gate(step) is CONFIRMATION_REQUIRED:
            await ui.confirm(step, consequences)       # may cancel task
        if risk.gate(step) is CRITICAL:
            await ui.confirm_immediately_before(step)

        # --- execute ---
        result = await executor.run(step.action, ui_lock)          # §4.1
        emit(TOOL_RESULT, result)

        # --- P2: executed ≠ achieved ---
        fresh = await perception.verify(step.expected_effect, timeout=step.verification.timeout_s)
        if fresh.satisfies(step.expected_effect):
            step.status = VERIFIED; record(step, fresh)
            checkpoint.persist(task)                  # §7
            if task.completion_condition.satisfied(perception.refresh(state)):
                break                                 # goal reached — stop early
        else:
            step.attempts += 1
            record_failure(step, expected=..., observed=fresh)
            await recovery.on_failed(task, step)       # §6

    # --- P6: honest reporting ---
    final = await perception.verify(task.completion_condition)
    task.status = FINISHED if final.ok else (BLOCKED if any_blocked else FAILED)
    emit(TASK_RESULT, status=task.status, evidence=final, failures=task.failures)
    if task.status is FINISHED:
        await memory.record_episode(task)              # doc 06
        await skills.consider_distill(task)            # doc 06 §5
```

### 4.1 UI lock
Any action that drives mouse/keyboard/focus/window placement acquires the
**global UI lock**. This makes v1's "focus WhatsApp → Alt+Tab → focus Chrome →
focus WhatsApp" fight structurally impossible — the orchestrator, not the LLM,
decides what holds focus and when.

---

## 5. Verification is a first-class subsystem

The Verifier is not an `if` inside the executor. It is a component with its
own policy, inputs, and evidence trail.

```text
ACTION EXECUTED                ACTION ACHIEVED ITS EFFECT
(function returned)     vs     (fresh observation matches expectation)
        │                               │
     means almost nothing          means STEP SUCCESS
```

Design rules:
1. **Freshness**: verification observes *after* the action, with its own
   timestamp; cached observations older than the action are invalid.
2. **Independent channel where possible**: verification must not rely on the
   same mechanism that performed the action when a cheaper independent one
   exists (e.g., after `pyautogui.click`, verify via a11y tree/DOM — not via
   "the click function returned true").
3. **Bounded wait**: expected effects get a timeout (`until` semantics) —
   WhatsApp needs a render delay; polling until deadline, not fixed sleeps.
4. **Negative verification**: some expectations are *absences* ("QR screen
   gone", "no second Chrome window"). Explicitly supported.
5. **Evidence stored** on the step; the final task report links it. The user
   can always ask: *why do you believe it succeeded?* → answer with
   observations, not confidence.

If the Verifier cannot observe the needed signal at any ladder level, the
step is `BLOCKED: verification gap`, never `VERIFIED`.

---

## 6. Failure policy (Recovery)

Recovery is a **policy table**, not free-form LLM improvisation. The LLM may
be consulted for hard cases, but the ladder runs deterministically first.

```text
ON FAILED (attempt < max_attempts):
   1. RE-PERCEIVE      cheap fresh observation — maybe effect is just slow
   2. RETRY            same action, bounded (default max_attempts = 2)
   3. ALTERNATIVE      second-ranked action for the same step intent
   4. REPAIR PLAN      remaining plan tail re-derived from new state
   5. ESCALATE         → BLOCKED report to user with precise ask

ON BLOCKED:
   1. CLASSIFY blocker  (missing login · missing permission · missing resource
                         · contradictory state · perception gap · user decision)
   2. AUTO-FIXABLE?  → insert corrective step (e.g., try another known profile)
   3. USER-FIXABLE?  → BLOCKED + exact instruction ("scan the QR code")
   4. UNACHIEVABLE?  → FAILED with reason

ANTI-LOOP RULES (hard-coded, apply to every task):
   • same action + same observed state → max 2 attempts, then policy escalates
   • zero-progress counter: N consecutive non-VERIFIED steps (default 4) →
     force REPAIR PLAN; 2 failed repairs → BLOCKED
   • actions with no expected-effect delta are rejected by the executor
     (this alone stops "launch Chrome" × 3)
   • overall step budget per task (default 20) → force planner reassessment
```

**Replan has a failure policy.** v1's log showed `⚠ REPLAN` with no
termination story. v2: replans are numbered, bounded, each requires a stated
*reason* (what state observation contradicted the plan), and exhaustion ends
the task in BLOCKED/FAILED with that history attached.

---

## 7. Checkpoints & resumption

```text
checkpoint written after EVERY verified step:
   { task_id, step_index, verified_state_predicates,
     evidence_refs, plan_tail_hash, timestamp }
```

- On crash/restart: open tasks are listed; user (or policy for LOW-risk
  tasks) resumes from **first unverified step** — verified steps are NOT
  re-executed (their effects still hold or will be re-checked at resume).
- On resume, all completion predicates are **re-verified first** — maybe the
  world already satisfies them.
- Checkpoints live in SQLite (doc 08: single file, WAL, negligible RAM).

---

## 8. Idempotency

Every mutating tool declares its idempotency class (doc 05):

| Class | Behavior before executing |
|---|---|
| `naturally_idempotent` (open, focus, navigate, read) | still state-checked (P3), cheap |
| `check_before_create` (mkdir, write file, download) | existence + identity check → `SKIPPED_ALREADY_DONE` |
| `effect_token` (send message, submit form, purchase, email) | durable dedup key: task_id + action hash; second attempt returns prior result |
| `destructive` (delete, overwrite, kill) | risk gate + explicit target verification |

Rule: **re-running a task from a checkpoint must never duplicate a side
effect.** This is testable (doc 11 §idempotency).

---

## 9. Task Manager duties (vs Orchestrator)

| Task Manager | Orchestrator |
|---|---|
| parse intent → goal/constraints/completion condition | produce and revise plans |
| assign priority + risk ceiling | run the step loop |
| route through fast path (skill/deterministic) first | drive perception/verifier/recovery |
| queue, cancel, resume, list tasks | write checkpoints |
| final honest reporting | emit step-level events |

The Task Manager never touches tools; the Orchestrator never invents goals.

---

## 10. Worked micro-example (state-dependent no-op)

```text
USER: "open whatsapp web"

DISCOVER  → L1 process list: chrome running; L4 CDP: tab web.whatsapp.com open,
            wa.login = true, chat list rendered
CHECK     → completion_condition ALREADY TRUE
RESULT    → FINISHED, 0 actions, 0 LLM calls, ~50 ms
            report: "WhatsApp Web was already open in Profile 11 — nothing to do."
```

v1 would have launched Chrome. v2 doesn't — and that is the whole point.
