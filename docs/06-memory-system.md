# 06 — Memory System

> Not one database. **Five memory types** with different lifetimes, different
> trust rules, and different retrieval paths — plus confidence and decay so
> memory can never harden into permanent stupidity.

---

## 1. The five memories

```text
┌──────────────────────────────────────────────────────────────┐
│ MEMORY (service available to ALL layers)                     │
│                                                              │
│  1 WORKING      current task, plan, screen, last actions     │
│  2 EPISODIC     what happened, when, with what result        │
│  3 SEMANTIC     what EVA knows (entities + facts)            │
│  4 PROCEDURAL   how to perform a known task (skills)         │
│  5 PROFILE      who the user is: prefs, accounts, habits     │
└──────────────────────────────────────────────────────────────┘
```

| Type | Content | Lifetime | Written by | Read by |
|---|---|---|---|---|
| **Working** | current task, plan, step, screen snapshot, variables | task/session — dies after | Orchestrator | planner, verifier, context builder |
| **Episodic** | dated records: request → actions → outcome → evidence | permanent, decaying salience | Orchestrator on task end | planner ("how did this go last time?"), discovery |
| **Semantic** | entities & facts: `Deltarq = user's cybersecurity project`, `Chrome profile "Person 1" = AIOU account` | permanent w/ confidence | memory writer (from verified outcomes) | context builder, intent, discovery |
| **Procedural** | verified workflows (skills): step recipes + verification | permanent w/ versioning | skill distiller (only from VERIFIED runs) | fast path (doc 07 §2) |
| **Profile** | user prefs, frequent contacts, tone, working hours, known accounts | permanent w/ confidence | explicit user statements > learned | intent, risk, communication style |

**Key separation:** *semantic* knows `AIOU → Profile 11`; *procedural* knows
`how to open Saad Bhai's archived chat`; *episodic* knows `last time this
worked on Oct 4 with wa.login = true`. Different questions, different stores.

---

## 2. The memory record

Every record across all types carries the same envelope (P8):

```jsonc
Memory {
  id, type: "episodic" | "semantic" | "procedural" | "profile" | "working",
  content: { ... },
  source: "verified_task" | "user_statement" | "inference" | "import",
  created_at, expires_at?, 

  confidence: 0.98,          // 0..1 — see derivation below
  usage_count: 31,
  last_used: t,
  last_verified: t,
  verify_method: "chrome_profile_scan",   // HOW to re-check this cheaply
  invalid: false,            // set by failed verification, kept for history
  tags: ["chrome", "aiou", "profiles"]
}
```

### Confidence rules

```text
source = user_statement           → 0.95
source = verified_task (observed) → 0.90 + 0.02 × corroborating_runs (cap 0.99)
source = inference (LLM)          → ≤ 0.60

decays:  confidence -= λ·Δt   (half-life by type:
          profile/semantic: long (months) · episodic: weeks · inference: days)

usage bumps:   last_used updated; UNUSED high-age records lose salience faster

verification events:
   re-check passes  → confidence ↑, last_verified = now
   re-check fails   → invalid = true (or confidence → ≤0.2) + episode written
```

### Enforcement

- **confidence ≥ 0.8**: may drive autonomous action (e.g., "AIOU = Profile 11"
  → planner may rely on it, verification still runs).
- **0.4–0.8**: may inform the plan, but the planner must add a cheap
  confirm-step first (e.g., `chrome.profiles` scan).
- **< 0.4**: never enters context as fact; may appear as a *hint* only.
- An `invalid` memory is excluded from retrieval and triggers a re-learn.

---

## 3. Storage: SQLite first (not a vector DB)

```text
eva.db  (SQLite, WAL)
  working_state      — current task snapshot (overwritten)
  episodes           — episodic records
  facts              — semantic entities/relations (the "knowledge graph", table form)
  profiles_kv        — user profile fields
  skills             — procedural recipes (versioned)
  skill_runs         — execution history per skill (success/fail counts)
  memories_meta      — envelope fields above for all durable records
  embeddings         — (Phase 5, optional) vectors for fuzzy retrieval
  tasks / checkpoints— doc 03
  llm_cache          — doc 07
  tool_journal       — doc 05 idempotency
```

- **Phase 1–4: no embeddings.** Retrieval = tags + FTS5 full-text + recency +
  type filtering. That covers "find my Deltarq proposal context" on a personal
  machine easily.
- **Embeddings added only when a real query fails** ("find that doc I mean
  but can't name") and must fit doc 08 budget (small local model or cheap
  API, one-time batch indexing — never per-keystroke).
- **Knowledge graph** = the `facts` table with typed relations
  (`Sohaib -project→ Deltarq`, `Deltarq -has→ website, documents`). Queries
  are SQL joins. A graph DB is a non-goal until SQL demonstrably hurts.

---

## 4. Episodic memory — the WhatsApp example

```jsonc
{
  "date": "2026-10-04",
  "request": "open WhatsApp in AIOU profile, open Saad Bhai archived chat",
  "outcome": "SUCCESS (v2) / FAILED (v1 log referenced)",
  "discovery": { "chrome_profile": "Profile 11", "identity": "aiou...@gmail.com",
                 "wa_login": true, "chat_location": "archived" },
  "plan_summary": [...], "steps": 6, "replans": 0,
  "lessons": ["wa needs ~3s render before a11y is trustworthy",
              "'Saad Bhai' lives under Archived, not chat list"]
}
```

Next time the user says *"WhatsApp mein Saad Bhai kholo"*:
intent hits episodic + semantic → discovery short-circuits (profile known,
archived location known) → plan is smaller and cheaper → possibly a
procedural skill fires and no planning LLM runs at all.

---

## 5. Procedural memory & skill distillation

Skills are how EVA becomes **faster and cheaper over time**.

```text
task FINISHED (verified) 
  → skill distiller asks:
      • ≥ 2 similar episodic records? (same intent signature)
      • plan stable across runs? (low plan entropy)
      • all steps contract-verified, no manual interventions?
      • deterministic enough? (same inputs → same recipe)
  → YES: distill → Skill
  → NO:  keep as episodic only
```

```jsonc
Skill {
  name: "open_whatsapp_archived_chat",
  intent_signature: ["whatsapp", "archived", "contact:*"],
  params: [contact],
  steps: [
    { tool: "chrome.open_profile", args: {profile: "$AIOU"}, verify: "cdp.url" },
    { tool: "wa.navigate_archived", verify: "wa.view == archived" },
    { tool: "wa.search", args: {q: "$contact"}, verify: "wa.results contains $contact" },
    { tool: "wa.open_chat", args: {q: "$contact"}, verify: "wa.selected_chat == $contact" },
  ],
  success_rate: 0.96, runs: 24, last_run, version: 3,
  fallback: "replan_with_llm",     // skill failure → normal planning path
  confidence: 0.95
}
```

**Runtime routing:** matching skill + confidence ≥ threshold + fresh
preconditions → execute directly (0 planning tokens). Skill miss, low
confidence, or a failed step → fall back to the full LLM planning path.

**Skill lifecycle:** versioned; a failing run drops success_rate; below
threshold → skill auto-quarantined (confidence ↓) → back to LLM path →
distiller may re-learn. Skills never lock EVA into a broken recipe.

> v1's `skill_synthesizer` matched intent by vector similarity — kept as the
> *matching* mechanism. What's new: matched skills now execute **verified
> contracts**, not synthesized free-form code.

### The learning curve this creates

```text
1st occurrence : LLM-heavy (discover → plan → verify every step)
5th occurrence : skill candidate distilled
20th occurrence: 0 planning tokens, deterministic, millisecond routing,
                 LLM only as fallback when verification fails
```

---

## 6. Semantic memory & the knowledge graph (humble edition)

```text
Sohaib
 ├─ project → Deltarq     (cybersecurity; dir=...; docs=proposal.*)
 ├─ project → Salesforge  └─ lead → UrbanArc (furniture AR)
 ├─ account → Chrome "Profile 11" = AIOU Gmail
 ├─ contact → Saad Bhai (WhatsApp, chats often PDF attachments)
 └─ uses often → Antigravity, WhatsApp, VS Code, Chrome(Profile 29)
```

- Populated from **verified task outcomes** and explicit user statements.
- Every edge has confidence + last_verified (an AIOU profile rename must be
  discoverable: `verify_method: chrome_profile_scan` runs when the memory is
  stale and the task touches it → mismatch → invalidate + re-learn).
- This is the answer to "stale memory becomes permanent stupidity": **memory
  used by a task is verified by that task's discovery pass (P8).**

---

## 7. Memory ↔ Context Builder contract

Memory never dumps itself into prompts. The context builder (doc 07) asks
for a **bounded, task-relevant slice**:

```text
memory.relevant(task, budget_tokens=800):
   working:   current plan/step/state pointers         (always)
   procedural: top matching skill (if confidence ≥ 0.8)
   episodic:  1–3 most relevant episodes (same intent / recent)
   semantic:  entities mentioned or referenced by the task (≤ 10 facts)
   profile:   only fields this task can affect (e.g., contacts for messaging)
```

Out: full histories, stale/invalid records, anything below confidence floor,
anything not touching the task. Target: **~1–3 KB, not megabytes.**

---

## 8. Acceptance criteria

- [ ] Second run of a task uses ≤ 1 LLM call thanks to skill/episodic hit
      (measurable in doc 11 §S3).
- [ ] Stale fact (`AIOU → Profile X` after profile rename) is detected during
      discovery, invalidated, task still succeeds, memory self-healed.
- [ ] Inference-sourced memory (< 0.6 confidence) never drives an autonomous
      action without a confirm-step.
- [ ] Working memory is wiped at task end (no cross-task leakage).
- [ ] All retrieval respects the 800-token budget; oversized results are
      truncated by relevance, not chopped arbitrarily.
