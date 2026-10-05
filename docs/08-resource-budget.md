# 08 — Resource Budget

> **The constraint that shapes every technical choice.** EVA is NOT running
> on a 36 GB workstation. It must be **peak-level powerful on a normal PC in
> normal use** — and every proposed library, model, or subsystem from here on
> must fit this budget or be made optional.

---

## 1. Target machine (the "normal PC" baseline)

```text
WINDOWS 10/11 · 8 CORES (4C/8T acceptable) · 16 GB RAM (8 GB minimum) · SSD
NO DISCRETE GPU required · modest iGPU · residential internet (possible 30–60 s outages)
```

- **16 GB** is the design target; everything must still *run* (degraded) at
  **8 GB**, where EVA + Chrome + VS Code are the reality.
- No assumption of always-on broadband: local fallbacks must exist for the
  core loop (doc 07 failover).

---

## 2. The RAM envelope

| Budget headroom | Reserved for | EVA may use |
|---|---|---|
| OS + background | ~3 GB | — |
| Chrome (user's real workload, 10–30 tabs) | ~4 GB | — |
| User's apps (VS Code, Office…) | ~2–4 GB | — |
| **Free target** | **≥ 4 GB** | — |
| **EVA CORE (idle)** | — | **≤ 250 MB RSS** |
| **EVA CORE (active task)** | — | **≤ 600 MB RSS** |
| EVA peak (with local small model loaded, Tier 1) | — | **≤ 1.5 GB RSS** |
| Local model resident (optional, Tier 1) | — | ≤ 1 GB, on-demand load, unload after N min idle |
| SQLite / caches / evidence on disk | — | ≤ 500 MB (rolling, TTL-cleaned) |

**Rules:**
1. EVA core must never be the reason the machine swaps. If free RAM
   < 1.5 GB → **degraded mode**: no local model, fewer perception samples,
   smaller caches (§5).
2. Big local models (7B+ on CPU) are **out of scope for the core loop** —
   they fail both RAM and latency budgets. Local = small tier-1 class models
   only (quantized, few-hundred-MB), loaded on demand.
3. Screenshots/evidence: cropped, compressed, TTL'd — never a video stream.
4. One process (asyncio), not a swarm of services. The event bus is
   in-process. Extra processes allowed only for an isolated local model host.

---

## 3. CPU envelope

| Situation | EVA CPU budget |
|---|---|
| Idle (watching, no task) | < 2% avg, near-zero wakeups |
| Active task (typical) | < 15% avg — the machine stays usable |
| Perception sampling | no polling loops faster than 1 Hz; event-driven where possible |
| OCR / vision (when invoked, short bursts) | < 100% of 2 cores, bounded duration |
| Model routing / API calls | network-bound, negligible CPU |

Forbidden by default: continuous screen capture, busy-wait loops, unbounded
parallel tool fan-out (doc 10 parallelism is gated by this budget).

---

## 4. Latency & cost budgets

### Latency (perceived speed — "peak" is also about feel)

| Interaction | Target |
|---|---|
| Tier 0 command (open app, CPU query, skill) | < 300 ms to first feedback |
| Any task | < 500 ms to first visible progress event (plan/discovery streaming) |
| Tier 1 classify | < 2 s |
| Tier 2 plan | first token < 2 s, plan < 15 s |
| Verification (L1–L4) | < 500 ms typical |
| Avatar/CLI event render | < 100 ms from emit |

### Token cost (monthly, normal usage = 20–40 tasks/day)

```text
Assumption: ~90% tasks Tier 0/1 (fast path + skills), ~10% Tier 2 planning.

Tier 1 : ~300 in / ~100 out  ×  ~30 calls/day  ≈ 400 K tokens/day  (negligible cost)
Tier 2 : ~4 K in / ~800 out  ×  ~4 calls/day   ≈ 20 K tokens/day
         + occasional generation tasks (docs/websites) — the big-ticket items,
           but these are the tasks the user *wants* to spend on.

TARGET: ≤ 1 M tokens/day for ordinary operation (well under $1/day on
        current cheap-API pricing), generation tasks metered separately.
HARD RULE: no task may make > 20 model calls; replan storms (doc 03 §6)
           cap it structurally.
```

---

## 5. Degraded mode (small machine / no internet)

```text
TRIGGER: free RAM < 1.5 GB  ·  CPU idle < 10% for 60 s  ·  no network
BEHAVIOR:
  • unload local model → Tier 1 goes to cache or waits for network
  • Tier 2 unavailable → skill/episodic paths preferred; if none and no
    network → BLOCKED: "offline, can't plan this novel task" (HONEST, not fake)
  • perception: drop L5/L6 sampling rates, keep L1–L4 (cheap, local)
  • caches shrink; evidence retention shortens
  • background slots (Phase 7) paused
RECOVERY: conditions clear → normal mode, resume from checkpoint
```

Degradation is **explicit in the CLI** (`mode: degraded, reason: …`) — the
user always knows why EVA is slower/dumber right now.

---

## 6. How this budget is ENFORCED (not just aspirational)

1. **Budget-gate in technical planning:** any proposed dependency answers:
   RAM? CPU? offline behavior? license? — measured against this doc before
   adoption (doc README rule 2).
2. **Runtime telemetry:** EVA tracks its own RSS, CPU, tokens, calls,
   cache hits; `/perf` in the CLI shows the live numbers (doc 09).
3. **Perf test in the suite (doc 11):** the WhatsApp scenario asserts:
   EVA core peak RSS ≤ 600 MB, model calls ≤ N, wall-clock ≤ target,
   0 redundant actions.
4. **Regression alarm:** CI (or local pre-commit perf check) flags any
   change that grows idle RSS or per-task token count beyond tolerance.
5. **Optional-by-default:** anything heavy (vision, local model, embeddings,
   OCR) is a plugin with lazy load — core loop runs without it.

---

## 7. Design decisions this budget forces (summary)

| Tempting choice | Budget verdict |
|---|---|
| Run a 7B local model for everything | ❌ RAM + latency — local = small Tier 1 only |
| Screenshot every step "for safety" | ❌ CPU + disk + tokens — vision on demand only (doc 04) |
| Separate microservice per subsystem | ❌ process overhead — single asyncio host + event bus |
| Vector DB from day one | ❌ RAM + complexity — SQLite FTS5 first (doc 06) |
| Always-listening voice with cloud STT | ❌ network + CPU — VAD-gated, local wake (doc 09, Phase 6) |
| Parallel fire of 5 browser actions | ❌ UI chaos + CPU — UI lock, parallel only for non-UI (doc 03) |
| Embed everything in memory all the time | ❌ — retrieval on demand, bounded slices (doc 07) |

**North star:** *normal machine, peak behavior.* Efficiency is not a later
optimization — it is a requirement that kills bad designs at planning time.
