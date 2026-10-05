# 04 — State & Perception

> **State is everything.** This is the single biggest architectural upgrade
> from v1: EVA always maintains a model of the world, and actions are
> *conditional on that model* — not on what the LLM last guessed.

---

## 1. The world-state model

One live object, owned by the State Manager, updated **only** by Perception
(never by an action's own return value):

```jsonc
WORLD_STATE {
  observed_at: { "system": t0, "chrome": t1, "whatsapp": t1, ... },  // per-branch freshness
  confidence:  { ... per-branch, inherited from ladder level },

  system: {
    os, foreground_app, active_window: { title, pid },
    windows: [ { title, app, pid, rect } ],
    resources: { cpu, ram, disk }                    // cheap, sampled on demand
  },

  chrome: {
    running: true,
    profiles: [ { dir: "Profile 11", identity: "aiou...@gmail.com", matched: true } ],
    active_profile: "Profile 11",
    tabs: [ { url, title, active } ],
    cdp_available: true
  },

  whatsapp: {
    loaded: true, login: true,
    view: "archived" | "chatlist" | "qr_login" | "unknown",
    selected_chat: "Saad Bhai",
    search_query: null
  },

  files: { ... targeted subtree facts, populated lazily },
  apps:  { ... running/installed facts, populated lazily }
}
```

Design rules:

1. **Lazy & targeted.** State contains what the current task needs. Discovery
   fills branches on demand (opening WhatsApp → populate `whatsapp` branch).
   Never snapshot the entire machine.
2. **Freshness per branch.** Every branch has `observed_at`. A step's
   preconditions demand a max age (`chrome: ≤ 2s`, `files: ≤ 30s`); stale
   branches are re-perceived before the check (P4).
3. **Three-valued logic.** Predicates evaluate to `TRUE / FALSE / UNKNOWN`.
   `UNKNOWN` is never treated as `TRUE` — and never silently as `FALSE`
   either; it triggers a perception query at the needed ladder level.
4. **Provenance.** Every value knows its ladder level; higher levels don't
   override cheaper ones arbitrarily — cheaper + fresher wins.
5. **Derived, not declared.** Actions never write "done: true". Only an
   observation may.

### State-driven no-op (the core behavior change)

```text
action requested
  → evaluate its expected_effect against STATE
      TRUE      → skip (SKIPPED_ALREADY_DONE)
      FALSE     → execute
      UNKNOWN   → perceive first, then decide
```

This is what eliminates the v1 pattern:

```text
launch Chrome → focus WhatsApp → wait → focus WhatsApp → Alt+Tab
→ focus Chrome → focus WhatsApp → launch Chrome AGAIN → launch Chrome AGAIN
```

Every one of those actions, checked against state, was either already
satisfied or had no expected-effect delta. In v2 they cannot run.

---

## 2. The perception ladder (L0 → L7)

EVA always uses the **cheapest reliable method first** and climbs only when
the current level cannot answer the question.

| Level | Source | Typical cost | Answers | Latency |
|---|---|---|---|---|
| **L0** | Internal state / task context | ~0 | what am I doing, what did I just do, what do I expect | 0 |
| **L1** | OS APIs — process list, window enumeration, foreground, resources | ~0 | is Chrome running? which window is front? CPU? | 1–10 ms |
| **L2** | Accessibility tree (a11y / UI Automation) | very low | buttons, labels, focus, tree structure of any app | 10–100 ms |
| **L3** | Application APIs — Chrome CDP, app IPC/CLI, DBs | low | exact URL, DOM, tab list, app-internal truth | 10–200 ms |
| **L4** | DOM / browser inspection (via CDP), file metadata | low | element presence, text, attributes | 10–100 ms |
| **L5** | OCR on a (cropped) screenshot | medium | text rendered by non-cooperative apps | 0.2–2 s local |
| **L6** | Screenshot (structural, no model) | medium | pixel truth, image saved as evidence | ~100 ms + storage |
| **L7** | Vision LLM | **expensive** | "what is this weird screen?" semantic interpretation | 1–10 s + tokens |

### Ladder-selection policy

```text
question asked
  → cheapest level that CAN answer it   (registry per question type, examples:)
       "is app X running / window Y front?"      → L1
       "what's the current URL / is page loaded?"→ L3 (CDP)  ← browser work
       "does button 'Archived' exist & is it
        visible / enabled?"                      → L2 or L4
       "what does this dialog say?" (cooperative)→ L2/L4
       "what does this dialog say?" (not co-op)  → L5 OCR
       "what kind of screen is this at all?"     → L6 → L7 only if still unclear
  → if level returns UNKNOWN / unavailable → climb one level
  → if answer still unknown at L7 → BLOCKED: perception gap (never guess)
```

**Vision-on-demand.** A screenshot/vision call happens only when:
- a structural level returned `UNKNOWN` twice for the same question, or
- the user explicitly asked about pixels ("what's on my screen?"), or
- a verification *requires* visual evidence and no a11y/DOM path exists.

Never: "took a screenshot so the LLM can see what's happening" as the default.

### Hard invariants (runtime-enforced policy, not prose)

The ladder above is not a suggestion. The following invariants are checked
by a **Perception Policy gate** in code — a request that violates them is
rejected *before* any tool runs, and the rejection is emitted as an event.

```text
INV-1  VISION-FIRST INTERACTION IS FORBIDDEN unless structured
       perception (L0–L5) is unavailable or insufficient for the
       CURRENT decision. The default interaction loop contains
       zero screenshots and zero vision-model calls.

INV-2  COORDINATE-BASED MOUSE CONTROL IS LAST-RESORT, NOT DEFAULT.
       A pointer action must first attempt structural targeting
       (a11y handle → DOM/CDP selector → named control → keyboard/
       shortcut). click(x, y) is permitted only with a recorded
       justification: structural targeting returned UNKNOWN at
       every available level.

INV-3  NO PIXELS WITHOUT A RECORDED JUSTIFICATION. Every L5/L6/L7
       request carries `reason` + `exhausted_levels` (which cheap
       levels already failed). Missing justification → rejected.

INV-4  SCREENSHOTS ARE EVIDENCE, NOT CONTEXT. Images are stored in
       the task's evidence area; they never enter an LLM prompt
       unless Tier-3 vision routing (doc 07) was explicitly chosen
       under INV-1's escape clause.
```

**Enforcement mechanics (Phase 2):**

- The task object carries counters: `l5_calls, l6_calls, l7_calls,
  coord_actions`. Every perception/act request passes through the policy
  gate, which validates justification and increments counters.
- Doc-11 **S6e** asserts: any scenario solvable structurally finishes with
  `l5_calls == l6_calls == l7_calls == coord_actions == 0`. A single pixel
  call in a browser/OS task fails the suite even if the task "succeeded".
- Cost ledger (doc 07) records each vision call with its justification, so
  token spend attributable to vision is always auditable.

### The dual hierarchy: UNDERSTAND vs ACT

Perception (understanding the world) and interaction (changing it) are two
separate hierarchies, each climbed cheapest-first:

```text
        EVA
         │
    ┌────┴─────┐
    │          │
UNDERSTAND    ACT
    │          │
    ↓          ↓
 OS/API     API / tool action
 state         ↓
    ↓       DOM / CDP target
Acc. tree      ↓
    ↓       Accessibility control
 App state     ↓
    ↓       Keyboard / shortcut
 DOM/CDP       ↓
    ↓       Mouse WITH target
 OCR/text      ↓
    ↓       Coordinates   ← last resort (INV-2)
 Screenshot
    ↓
 Vision LLM   ← last resort (INV-1)
```

Notes:
- Keyboard/shortcut is ranked *above* mouse-with-target: a reliable shortcut
  or focus path (e.g. Ctrl+L in Chrome) is more deterministic than any click.
- Understanding can stop at any level; escalation is per-question, not
  per-task.
- An action may only target a structure that UNDERSTAND already resolved
  (INV-2): "click the thing I identified" — never "click where I think
  it is".

### Chrome/WhatsApp example (v1's failure, corrected)

```text
v1:  screenshot → vision model → "I think this is Chrome"        (slow, fuzzy, costly)

v2:  L1: chrome.exe present? window title?               ← 5 ms
     L3: CDP: active tab URL = web.whatsapp.com?         ← 50 ms
     L4: DOM: login screen or chat list?                 ← 30 ms
     vision: not needed at all
```

---

## 3. Perception outputs (Observations)

```jsonc
Observation {
  question: "is_wa_logged_in",          // what was asked
  level: "L4",                          // how it was answered
  value: true,
  raw: { selector: "#pane-side", note: "chat list rendered" },
  observed_at: t,
  ttl: 2000,                            // validity window in ms
  evidence_ref: "ev_0042"               // stored for the final report
}
```

- Observations are **append-only evidence**; state is the folded current view.
- Verification (doc 03 §5) consumes observations; it never fabricates them.
- Expensive observations (L6/L7) are cached by question+region until the
  underlying UI generation changes (invalidated by a21y/DOM hash or action).

---

## 4. Subsystem interfaces (design-level)

```text
Perception
  ├─ snapshot(task)            → initial state for discovery (cheap levels only)
  ├─ query(predicates, max_age)→ answers TRUE/FALSE/UNKNOWN with provenance
  ├─ verify(effect, timeout)   → polling-until-deadline using cheapest method
  ├─ explain(predicates)       → human-readable "how I know this" (CLI /debug)
  └─ watch(predicates, cb)     → Phase 7: watcher primitives (doc 10)

State Manager
  ├─ get(branch) / get_all()   → read view (never mutated by callers)
  ├─ invalidate(branch)        → force re-perception before next read
  └─ diff(old, new)            → drives "did the UI actually change?" checks
```

**State diffing as a verification tool.** For many UI actions the cheapest
strong verification is: *observe before → act → observe after → require a
non-empty diff on the relevant subtree*. This detects both "nothing happened"
and "something else happened".

### Observation cache & event-driven perception

Acting should not imply re-perceiving. The default loop is event-driven:

```text
ACT  →  OS/APP EVENT (foreground change, download done, DOM mutation,
                       fs event, process exit)  →  STATE UPDATE  →  VERIFY
```

not `act → screenshot → look → act → screenshot`.

Rules:

1. **Cache by question + branch.** An observation is reused while within
   its `ttl` and the branch's `observed_at` is younger than the step's
   `max_age`. Re-asking a fresh question is a cache miss, not a reflex.
2. **Invalidate on action.** Executing a tool invalidates only the branches
   its `effects` touch (`files.downloads` for a download; `chrome.tabs` for
   a navigation) — other branches keep their cached value. No blanket
   "state is stale, re-observe everything".
3. **Prefer waiting on an event over polling.** `verify()` should block on
   the expected event/state-change (with deadline) rather than loop with
   screenshots. Polling a *cheap* level (L1/L3) is acceptable; polling L6+
   is an INV-1 violation.
4. **No change → no observation.** If no relevant event fired and the
   cached branch is within freshness bounds, the answer is "state
   unchanged" — at zero cost.

Example: `browser.download(pdf)` → filesystem event `Downloads/x.pdf`
→ `files` branch update → verify exists — **zero screenshots, zero vision
calls**. Likewise `window.focus(WhatsApp)` → `foreground-window-changed`
event → verify foreground — again zero images.

---

## 5. Perception vs. risk

Observing is always LOW risk; **acting** is where risk lives (doc 05). But
note: capturing screenshots/OCR keeps pixels — screenshots containing
WhatsApp content are treated as **sensitive evidence**: stored only in the
task's evidence area, retained per a short TTL, never sent to an LLM unless
the task's risk disclosure allowed it (P9/P11 interplay).

---

## 6. Acceptance criteria for this subsystem

From doc 11 test cases:

- [ ] `open whatsapp web` on an already-open, logged-in session performs
      **zero actions** and reports the no-op (L1+L3 only, no screenshot).
- [ ] Verification of a click on a non-cooperative app succeeds via L2
      without ever invoking L7.
- [ ] QR-login screen is detected at L4 and yields `BLOCKED: scan QR` — not
      a retry loop, not a completed task.
- [ ] All state answers in `/debug` show level + timestamp + confidence.
- [ ] **Invariant suite (S6e–S6h):** a structurally solvable task ends with
      task counters `l5_calls == l6_calls == l7_calls == coord_actions == 0`;
      an unjustified `click(x, y)` is rejected by the policy gate; an L5/L6/L7
      request without `reason + exhausted_levels` is rejected pre-execution;
      a download task is verified via filesystem event with 0 screenshots.
