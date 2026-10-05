# 02 — System Architecture

---

## 1. The full picture

```text
                            ┌───────────────┐
                            │     USER      │
                            └───────┬───────┘
                    voice / eDEX CLI / VRM / dashboard
                                    │
                                    ▼
                     ┌──────────────────────────┐
                     │     EVA INTERFACE LAYER  │  (doc 09)
                     │  input · progress · logs │
                     │  state view · avatar     │
                     └────────────┬─────────────┘
                                  │  events out / user input in
                                  ▼
                     ┌──────────────────────────┐
                     │       TASK MANAGER       │  (doc 03)
                     │  intent · goals          │
                     │  constraints · risk      │
                     │  completion criteria     │
                     └────────────┬─────────────┘
                                  │
                                  ▼
                     ┌──────────────────────────┐
                     │       ORCHESTRATOR       │  (doc 03)
                     │  Planner                 │
                     │  State Manager           │
                     │  Executor                │
                     │  Verifier                │
                     │  Recovery                │
                     │  Checkpoints             │
                     └────────────┬─────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          │                       │                       │
          ▼                       ▼                       ▼
   ┌─────────────┐        ┌──────────────┐        ┌──────────────┐
   │ TOOL SYSTEM │        │  PERCEPTION  │        │    MEMORY    │
   │ OS · Browser│        │ a11y · DOM   │        │ working      │
   │ Files · Term│        │ OCR · vision │        │ episodic     │
   │ Apps · Docs │        │ OS APIs      │        │ semantic     │
   └──────┬──────┘        └──────┬───────┘        │ procedural   │
          │                      │                │ skills       │
          │                      │                └──────┬───────┘
          └───────────┬──────────┴───────────────────────┘
                      │
                      ▼
             ┌─────────────────┐
             │  MODEL ROUTER   │  (doc 07)
             │ local / cheap   │
             │ strong / vision │
             └────────┬────────┘
                      │
                      ▼
             ┌─────────────────┐
             │    EVENT BUS    │  (doc 09)
             └───┬─────┬────┬──┘
                 │     │    │
                 ▼     ▼    ▼
              voice   VRM   eDEX / logs / telemetry
```

**Two cross-cutting services, available to the entire core — not "layers after
the system":**

1. **MEMORY** — every layer reads/writes it under the rules of doc 06.
2. **MODEL ROUTER** — every layer that needs intelligence asks the router, never
   a provider directly (doc 07).

And one backbone: the **EVENT BUS**, the only channel through which anything
reaches the UI, avatar, voice, or logs (doc 09).

---

## 2. Layer responsibilities (one sentence each)

| Layer | Responsibility | Must NOT do |
|---|---|---|
| **Interface** | Render state/events; capture user input | Contain any intelligence or decision logic |
| **Task Manager** | Turn a request into a Task object with completion condition | Decide how to achieve it |
| **Orchestrator** | Own the loop: plan, dispatch, verify, recover, checkpoint | Call LLM providers directly; bypass risk engine |
| **Tool System** | Execute contracted actions against the world | Decide *whether* an action should run |
| **Perception** | Produce observations at the cheapest sufficient ladder level | Mutate the world |
| **Memory** | Store/retrieve/decay knowledge under confidence rules | Silently overwrite or "fix" itself |
| **Model Router** | Select tier, assemble cost cap, route, cache | Hold business logic |
| **Event Bus** | Deliver ordered events to subscribers | Block the producer (async, lossy-safe) |

---

## 3. Canonical request lifecycle

```text
USER TEXT / VOICE
   │
   ▼
[1] INTENT        classify + extract constraints            (may be no-LLM)
   │
   ▼
[2] FAST PATH?    skill match? deterministic recipe?        (doc 06/07)
   │              yes → jump to [6] with a scripted plan
   │              no  ↓
   ▼
[3] DISCOVER      cheap perception sweep → initial STATE    (doc 04)
   │
   ▼
[4] PLAN          LLM (right tier) proposes plan +          
   │              completion condition, validated structurally
   ▼
[5] RISK GATE     each step classified LOW/MED/HIGH/CRIT    (doc 05)
   │              HIGH/CRIT → ask user BEFORE execution
   ▼
[6] EXECUTE STEP  precondition check → action → effect
   │
   ▼
[7] VERIFY        cheapest sufficient observation vs        
   │              expected effect (doc 04/05)
   │        ┌─────┴──────┐
   │     VERIFIED     NOT VERIFIED
   │        │             │
   │        ▼             ▼
   │   next step      RECOVERY policy (doc 03 §6)
   │                      │
   └──────────────────────┘   (bounded retries — no infinite loops)
   │
   ▼
[8] COMPLETION     completion condition checked against STATE
   │               true  → FINISHED (+ memory write, skill learn)
   │               false → honest FAILED / BLOCKED report
   ▼
[9] EMIT           task result event → CLI / avatar / voice / log
```

**Key property:** every arrow going *down* is a decision validated by state,
and every arrow coming *back up* is an observation, not an assumption.

---

## 4. Process & concurrency model (design intent — exact tech TBD)

```text
EVA Core (single host process, asyncio)
 ├── Orchestrator task loop        (one active foreground task)
 ├── Perception workers            (bounded pool, results carry timestamps)
 ├── Tool executor                 (serializes conflicting actions on same target)
 ├── Model Router client           (async, cancellable, cached)
 ├── Event bus                     (async fan-out, slow consumer = drop+count)
 └── Persistence (SQLite WAL)      (checkpoints, memory, skills)

Background layer (Phase 7):
 ├── Scheduler (cron-like jobs)
 ├── Watchers  (file/window/email triggers)
 └── Background task slots (resource-gated, doc 08)
```

Rules:
- **One foreground task at a time** (v1's interleaved focus-fighting came from
  uncoordinated concurrent UI actions). Background tasks may run only in
  Phase 7 and only for non-UI domains (files, network, compute).
- **UI-affecting actions take a global UI lock.** Only one action may
  drive mouse/keyboard/window focus at a time.
- **All long operations are cancellable**; the user can always interrupt.

---

## 5. Data ownership

| Data | Owner | Store |
|---|---|---|
| Task/plan/checkpoint | Orchestrator | SQLite `tasks` table (doc 03) |
| World state | State Manager (fed by Perception) | in-memory + last snapshot persisted |
| Memory of all kinds | Memory subsystem | SQLite (doc 06) |
| Skills / learned workflows | Memory subsystem | SQLite + versioned script bodies |
| Tool contracts | Tool registry | code + JSON manifest (doc 05) |
| Events | Event bus | in-flight only; optional append-only log for debugging |
| LLM cache | Model Router | SQLite/disk, TTL (doc 07) |

**Single-writer rule:** only the owning subsystem writes its data. Everyone
else reads or emits events.

---

## 6. Failure containment

| Failure | Containment |
|---|---|
| LLM unreachable / malformed reply | Router falls to next tier (doc 07); if no tier can plan → task `FAILED` with reason, state preserved |
| Tool throws | Step `FAILED` → recovery policy; never crashes the loop |
| Vision/OCR unavailable | Perception degrades down the ladder; if the needed level is unavailable → `BLOCKED: perception gap` |
| UI lock contention / stuck window | Timeout → `BLOCKED`, report which resource is stuck |
| Crash mid-task | Checkpoint on disk → resume from last verified step (doc 03 §7) |
| Slow subscriber on event bus | Drop-with-counter for that subscriber; core never blocks |

---

## 7. What carries over from v1 (informative)

v1 modules mapped to v2 homes (mechanisms reused, control model replaced):

| v1 | v2 home | Verdict |
|---|---|---|
| `core/agent_loop.py` step loop | Orchestrator (doc 03) | rewritten — new control model |
| `core/agent_loop.py` ActionVerifier | Verifier (doc 03 §5) | idea kept, contract-driven rewrite |
| `core/perception.py` | Perception (doc 04) | extended into L0–L7 ladder |
| `core/llm_router.py` | Model Router (doc 07) | tier model added, caching kept |
| `core/memory.py` / `memgpt_memory.py` | Memory (doc 06) | unified into one subsystem with confidence/decay |
| `skills/*` | Tool registry (doc 05) | wrapped with preconditions/effects/verification |
| `eva_cli.py` / `eva.py` | Interface (doc 09) | kept as renderer; all logic removed |
| `core/avatar_*.py` / VRM plan | Event subscribers (doc 09) | kept, driven purely by bus events |
| `core/skill_synthesizer.py` | Skill memory (doc 06) | re-scoped: matches only, no codegen without verification |

Nothing is sacred; v1 is a parts donor, not a foundation.
