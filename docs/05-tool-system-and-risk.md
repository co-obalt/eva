# 05 — Tool System & Risk Engine

> Tools have **contracts**, not just signatures. An action without
> preconditions, expected effects, and a verification method does not exist
> in v2.

---

## 1. The action contract

Every tool registers a contract. The Orchestrator refuses to execute any
action whose contract is missing or whose preconditions fail.

```jsonc
Tool {
  name: "chrome.open_profile",
  category: "browser" | "os" | "files" | "terminal" | "apps" | "documents",
  description: "Launch Chrome with a specific profile and URL",
  params: { profile_query: "string", url: "string" },     // typed schema

  preconditions: [                    // checked BEFORE execution (P3)
    "chrome.binary_found",
    "profile_exists(profile_query)"
  ],
  effects: [                          // what the world should look like AFTER
    "chrome.running == true",
    "url_matches(url) in active tab"
  ],
  verification: {                     // cheapest sufficient check (P4)
    method: "L1_process + L3_cdp_url",
    timeout_s: 15,
    on_timeout: "FAILED"
  },

  idempotency: "naturally_idempotent" | "check_before_create" | "effect_token" | "destructive",
  risk: "low" | "medium" | "high" | "critical",           // static ceiling…
  risk_context: fn(args) → level,                          // …+ dynamic override
  ui_lock: true|false,                 // does it drive mouse/keyboard/focus?
  cost_hint: { time_s, tokens },
  parallel_safe: false                 // may it run alongside other actions?
}
```

### Contract examples

```text
TOOL: launch_chrome_profile
PRE : chrome available · target profile identified
EFF : chrome process exists · target profile active · URL open
VER : window exists (L1) AND profile identity confirmed (L3 CDP)

TOOL: click(target)
PRE : target exists AND visible AND enabled (L2/L4)
EFF : UI transition specific to that target
VER : expected UI change observed (state diff) — "function returned" alone
      is explicitly NOT verification

TOOL: send_whatsapp_message(contact, text)
PRE : wa.login == true AND selected_chat == contact AND text matches user's words
EFF : message appears in transcript
VER : transcript contains text + timestamp (L4)
RISK: HIGH  (outbound communication — always gated)
DEDUP: effect_token (task_id + hash) — never sends twice

TOOL: delete_path(path)
PRE : path exists · path resolves inside allowed roots · size computed
EFF : path gone
VER : not exists (L1 fs)
RISK: CRITICAL if > 100 MB or contains user media; HIGH otherwise
```

### Why contracts matter (mapping to the v1 failure)

| v1 failure | Contract mechanism that prevents it |
|---|---|
| `launch_chrome` ran twice+ | effect already true → precondition/state check → `SKIPPED_ALREADY_DONE`; no-effect-delta actions rejected |
| focus/Alt+Tab thrash | `ui_lock` + planner marks focus steps redundant; state check shows focus already correct |
| `/archive` URL attempted as navigation | tool registry has no such action; planner can only emit registered tools with valid params |
| "clicked successfully" without UI change | `verification.method` mandatory; no contract → no execution |
| task reported complete while failing | completion condition checked against state, not step count (doc 03) |

---

## 2. Tool inventory (phased — quality over count)

Phase 3 scope (doc 10). Each tool ships with contract + tests + a doc-11
scenario or is not shipped.

| Domain | First tools | Notes |
|---|---|---|
| **OS** | list_windows, focus_window, launch_app, close_app, get_resources | L1-based, near-free |
| **Browser** | chrome.profiles (scan/identify), open_profile, cdp.tabs, cdp.eval, navigate, a11y.find, click, type | CDP-first; pyautogui only as last resort with L2 verify |
| **Files** | stat, read, write, mkdir, move, copy, delete, search, organize | all `check_before_create` / destructive-gated |
| **Terminal** | run (allowlisted), capture output | sandboxed, timeout, no shell injection; risk ≥ MEDIUM for mutating commands |
| **Apps** | launch/quit/focus per known app; app-specific adapters (WhatsApp, Explorer) | adapters expose structured state → cheap perception |
| **Documents** | create/convert/render (HTML→PDF, DOCX), extract text | used by proposal/report tasks |
| **System info** | cpu/ram/disk/process query | pure L1, zero LLM |
| **Comms (later)** | send message/email | always HIGH/CRITICAL risk gate |

**No tool without a contract. No contract without a verification method.**

### Tool discovery & MCP
External tools (MCP or otherwise) are imported **with contracts translated**:
if a remote tool cannot declare effects + verification, it runs only in
LOW-risk contexts with LLM-assisted verification flagged as `WEAK_VERIFY`
in the report. Weak verification can never alone satisfy a completion
condition marked `requires_strong_evidence`.

---

## 3. Risk engine

### Levels

```text
LOW      → act automatically; log event
MEDIUM   → act automatically; log event + include in task report
HIGH     → CONFIRM with user before execution (one click / one voice "haan")
CRITICAL → explicit confirmation IMMEDIATELY before execution,
           showing concrete consequences (count, size, target, recipient)
```

### Classification

```text
static_class (from tool contract, e.g. delete = HIGH)
  combined with
dynamic_class (from arguments):
    size > threshold, path outside allowed roots,
    recipient not in known-contacts, irreversible vs recoverable,
    target is running process with unsaved work, purchase/password/shutdown…
  → final = max(static, dynamic)
  → task-level ceiling: task.risk_level can RAISE a step's gate, never lower it
  → LLM output cannot set risk; only the engine classifies (P9)
```

### Confirmation UX (CLI / voice / avatar)

```text
⚠ HIGH RISK — send_whatsapp_message
   To   : Saad Bhai
   Text : "phir kayaa?"
   This sends an outbound message the user will see immediately.
   [y] confirm   [e] edit   [n] cancel task
```

- Confirmation events go on the bus → avatar shows a questioning expression,
  voice asks, CLI waits (doc 09).
- Timeout policy: HIGH times out to **cancel** (fail-safe), never to confirm.
- CRITICAL additionally requires the task's original request to plausibly
  cover this action; mismatch → escalate as a new explicit ask.

### Risk examples

| Request | Engine says |
|---|---|
| "open Downloads" | LOW → instant |
| "organize my downloads" | MEDIUM (moves files; preview + log) |
| "delete this folder" | HIGH/CRITICAL → "will permanently delete 2.4 GB from X. Continue?" |
| "message Naeem 'phir kayaa?'" | HIGH → show recipient + exact text |
| "buy the domain", "change password", "shutdown PC" | CRITICAL → immediate confirm, no batching |
| "read my CPU usage" | LOW → instant, no LLM |

---

## 4. Executor rules

1. **Precondition check is mandatory** and happens against fresh state, not
   cached plan-time state.
2. **No-effect-delta rejection**: if `effects` are already true, return
   `SKIPPED_ALREADY_DONE` without running the tool (P3).
3. **UI lock**: `ui_lock: true` tools serialize globally (doc 03 §4.1).
4. **Timeouts** are part of the contract; a hung tool = `FAILED(timeout)`.
5. **Every execution emits** `TOOL_START` / `TOOL_RESULT` events with args
   (redacted per policy), duration, and outcome — the CLI's activity feed and
   the avatar's reactions are pure subscribers (doc 09).
6. **Side effects recorded** in an append-only journal feeding idempotency
   dedup keys and the final report.
7. **Structural targeting before pointer** (P7b, doc 04 hard invariants):
   UI tools resolve their target structurally first — a11y handle → DOM/CDP
   selector → named control → keyboard/shortcut — and a coordinate-based
   input (`click(x, y)`, `move(x, y)`) is accepted only when the contract
   call site supplies a justification that every structural level returned
   `UNKNOWN`. The policy gate rejects unjustified coordinate actions and
   any vision request lacking `reason + exhausted_levels`. No tool may
   *require* a screenshot as its normal input; screenshots arrive only via
   the L5/L6/L7 escalation path as evidence (INV-3/INV-4).

---

## 5. HITL (human-in-the-loop) surfaces

| Surface | Used for |
|---|---|
| `CONFIRM(step)` | HIGH/CRITICAL risk gates |
| `CLARIFY(question, options)` | intent ambiguity, missing completion criteria |
| `BLOCKED message` | precise instruction for the user to fix something (QR scan, login) |
| `PROPOSE(plan)` | optional plan preview mode for HIGH-risk tasks ("plan shown before execution") |

All are ordinary events on the bus; the CLI, voice, and VRM render the same
request in their own way (doc 09).

---

## 6. Acceptance criteria

- [ ] A tool with a missing/invalid contract cannot be registered (registry
      test), let alone executed.
- [ ] `delete_path` on a 2.4 GB folder triggers CRITICAL confirmation showing
      exact size and path; declining cancels the task cleanly.
- [ ] Sending the same message twice from a resumed checkpoint results in
      **one** actual send (effect_token test).
- [ ] Re-running an already-satisfied action yields `SKIPPED_ALREADY_DONE`
      and zero world mutations.
- [ ] An unjustified `click(x, y)` (no exhausted structural levels) is
      rejected by the policy gate; the same action with a recorded
      justification is accepted (INV-2 test).
- [ ] A structurally solvable task finishes with 0 L5/L6/L7 calls and
      0 coordinate actions, asserted from task counters (S6e).
