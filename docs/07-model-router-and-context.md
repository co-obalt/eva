# 07 — Model Router & Context Builder

> Two ideas: **(1)** not every thought deserves a frontier model — route by
> task difficulty; **(2)** never send the whole world to the LLM — assemble
> only what this decision needs.

---

## 1. The reasoning ladder (mirrors the perception ladder)

```text
                 USER REQUEST
                      │
                 TASK ROUTER
        ┌─────────────┼──────────────────┐
        ▼             ▼                  ▼
   TIER 0         TIER 1             TIER 2
   NO LLM         LOCAL / CHEAP      STRONG API
   deterministic  small model        big model
        │             │                  │
        │             │            TIER 3 (subset)
        │             │            VISION — only when
        │             │            perception hit L7
        ▼             ▼                  ▼
   "Open Chrome"  classify intent    design full website
   "CPU usage?"   pick tool          analyze contract
   "Move file"    extract entities   recover broken workflow
   known skill    light extraction   ambiguous goal → plan
   state checks   title suggestions  complex replan
```

| Tier | Used for | Examples |
|---|---|---|
| **0 — no LLM** | deterministic actions, state checks, known skills, simple templating | open/close/focus apps, CPU/RAM/disk, file ops, skill execution, completion-condition checks, verification (never an LLM) |
| **1 — small/cheap** (local small model or cheap API) | classification, extraction, matching, small rewrites | intent class, entity extraction (contact name, profile, path), tool choice among known candidates, summarizing one observation |
| **2 — strong API** | planning, decomposition, recovery reasoning, generation | new-task planning, complex replan, contract analysis, writing documents |
| **3 — vision** | only after L7 perception | "what is this dialog?" when no structural signal exists |

### Routing rules

1. **Skills and state checks short-circuit before any model** (fast path,
   doc 06 §5).
2. Router picks a tier from **task class + complexity features** (steps
   needed, novelty vs episodic memory, domain, risk). Ambiguous → start at
   Tier 1, escalate on failure.
3. **Escalation ladder:** Tier 1 fails/refuses/low-confidence twice → Tier 2.
   Never silently degrade output quality.
4. **De-escalation is the goal:** every successful novel task is a candidate
   to become deterministic/skill-able (doc 06 §5). Over weeks, the workload
   slides from right to left on the ladder.
5. **Provider failover** (within a tier): primary → secondary → local, each
   with timeout + circuit breaker (v1's router behavior is kept: Groq /
   APINEX / Ollama-style failover), plus response cache with TTL.
6. **Verification and risk classification are never outsourced to an LLM**
   (P4, P9).

---

## 2. Deterministic fast path

```text
request
  → normalize (typos, Roman Urdu — keep v1's vector matcher for this)
  → exact/near skill match (confidence ≥ threshold)?
       yes → skill executes (Tier 0)
  → fully deterministic recipe? (single obvious tool + args)
       yes → execute directly
  → else → Task Router → Tier 1/2 planning
```

Commands like `open chrome`, `organize downloads (known policy)`,
`what's cpu usage`, `open antigravity` run with **0 LLM calls**.

---

## 3. Context Builder — dynamic assembly

**Bad (v1 habits):** every call ships full conversation + all memory + all
files + whole screenshot + all tool descriptions + all past actions.

**v2:** per-decision assembly:

```text
              CURRENT TASK (goal, step, constraints, completion condition)
                           │
     ┌─────────────────────┼──────────────────────────┐
     ▼                     ▼                          ▼
 RELEVANT STATE      RELEVANT MEMORY            RELEVANT TOOLS
 (only branches the   (≤800 tokens from         (only tools legal for
  step's predicates    doc 06 §7 slice)           this step's domain +
  depend on, with                             risk level — NOT the whole
  freshness + level)                            registry)
     └─────────────────────┼──────────────────────────┘
                           ▼
              LATEST OBSERVATION(s) (the 1–3 that matter, not the log)
                           │
                           ▼
                  TOKEN-BUDGETED PROMPT
```

### Assembly rules

1. **Tools:** ship tool contracts relevant to the current step/domain only.
   Full registry lives in code; the model sees a filtered menu.
2. **State:** predicate-relevant branches with `observed_at` + ladder level;
   `UNKNOWN` branches explicitly marked (so the model can ask to perceive,
   not guess).
3. **Memory:** bounded slice per doc 06 §7, confidence-tagged.
4. **History:** not the raw conversation — the *task ledger* (what was
   attempted, what was verified, what failed and why), capped at the last
   K entries + plan.
5. **Never:** screenshots (unless Tier 3), full file dumps, full DOM, whole
   tool list, full episodic history.
6. **Hard budget per call** (see §4) enforced by the builder; overflow drops
   in priority order: old history → low-relevance memory → verbose tool
   descriptions. The prompt never silently exceeds budget.

### Example

> "Open my Deltarq proposal."

```text
context = ~1.4 KB:
  task: open file matching "Deltarq proposal"
  state: files branch (last search results, Desktop+Docs indices)
  memory: [semantic] Deltarq = project, dir = C:\...\Deltarq\docs
          [episodic]  Oct 2: opened Deltarq_Proposal_v3.html
  tools: files.stat, files.search, files.open, apps.launch
  history: (empty — new task)
```
Not: all projects, all conversations, all documents, all tools.

---

## 4. Token & latency budgets

(Aligned with doc 08; enforced by the builder + router.)

| Call type | Target | Hard cap |
|---|---|---|
| Tier 1 classify/extract | ≤ 500 in / ≤ 150 out | 1 K |
| Tier 2 plan (new task) | ≤ 4 K in / ≤ 800 out | 8 K |
| Tier 2 replan | ≤ 3 K in / ≤ 600 out | 6 K |
| Tier 2 generation (doc/website) | streaming, per-section | as needed — this IS the product |
| Tier 3 vision | 1 image (cropped), ≤ 1 K out | 2 images |

| Metric | Target |
|---|---|
| Fast-path response (Tier 0) | < 300 ms |
| Tier 1 response | < 2 s (local: < 1 s) |
| Tier 2 first token | < 2 s |
| Redundant LLM calls per task | 0 (same question never re-asked — answers cached in task ledger) |

### Caching

```text
1. response cache     — identical (model, prompt-hash) → reuse (TTL)
2. task-ledger memo   — "already answered" facts per task (no repeat calls)
3. embedding cache    — batch, Phase 5 only
4. observation cache  — perception results by question+generation (doc 04 §3)
```

---

## 5. What the model is actually FOR (division of labor)

```text
LLM recommends:      plan steps, classify intent, extract entities,
                     explain, generate content, propose recovery

SYSTEM decides:      whether recommendation is valid (preconditions, state),
                     whether it may run (risk gate), whether it worked
                     (verifier), when to stop (completion condition)

TOOLS execute · PERCEPTION observes · VERIFIER checks ·
MEMORY learns · RECOVERY fixes
```

The model never holds authority — only influence, filtered through contracts.

---

## 6. Acceptance criteria

- [ ] `open chrome` / `cpu usage` / known-skill runs produce **0** provider
      calls (logged).
- [ ] Measured prompt size for a typical plan call ≤ 4 K tokens with the
      full repo, memory, and history EXCLUDED (doc 11 instrumentation).
- [ ] Provider outage on Tier 2 → automatic failover to next provider/local;
      task completes or reports BLOCKED with reason — never a silent hang.
- [ ] Same question asked twice in one task = one API call (ledger memo).
