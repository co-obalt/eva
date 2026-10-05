# 09 — Interfaces & Events

> eDEX/CLI, VRM avatar, voice, dashboard: **renderers only.** All of them
> consume the same event bus. No interface contains intelligence, and the
> avatar never needs an LLM to know what face to make.

---

## 1. Interface layer responsibilities

```text
EVA INTERFACE LAYER
 ├── eDEX CLI (primary)   input · plan view · step table · state · logs
 ├── VRM avatar            expression/animation driven by events
 ├── Voice                 mic → VAD → STT → (router) → TTS → lip sync
 └── (optional) web dashboard — same bus, later
```

**Rule:** the interface *displays* what the core *believes and does*. If the
CLI says "completed", the core's completion condition verified. If the avatar
smiles, a `TASK_FINISHED / success` event fired. One source of truth.

---

## 2. eDEX CLI — the control surface

```text
EVA ❯ open whatsapp in aiou profile, saad bhai ka archived chat kholo

[INTENT]   browser.navigation · risk: low
[PLAN]     ✓1 locate AIOU profile          SKIPPED (known: Profile 11)
           ✓2 open WhatsApp Web            VERIFIED  (cdp.url, 1.2s)
           ✓3 open Archived                VERIFIED  (wa.view, 0.8s)
           ✓4 search "Saad Bhai"           VERIFIED  (wa.results)
           →5 open conversation            VERIFYING…
           ○6 confirm completion condition

[STATE]    fg: Chrome · profile: Profile 11 · wa: archived/SAAD · cpu 14% ram 5.8G
[MODE]     normal · tier: skill+planner · calls: 1 · tokens: 2.1K

EVA ❯ /debug        → toggles: ladder levels, freshness, raw observations,
                       confidence values, token ledger
EVA ❯ /state        → current WORLD_STATE (human-readable)
EVA ❯ /perf         → RSS, CPU, calls, tokens, cache hits (doc 08 §6)
EVA ❯ /tasks        → running/queued/checkpointed tasks (resume/cancel)
EVA ❯ /memory       → recent episodes/facts + confidence
EVA ❯ /skills       → learned procedures + success rates
EVA ❯ /explain last  → why the last action was chosen & what verified it
```

CLI commands: `/debug /state /perf /tasks /memory /skills /explain /help
/clear /exit` (+ `/confirm y|n` for non-interactive-ish flows).
Progress renders from `TASK_STEP_*` events; the CLI holds no logic of its own
(v1's `eva_cli.py` becomes a renderer of these events).

---

## 3. Event bus catalogue

```text
Envelope { id, ts, type, task_id?, step?, payload }

TASK_*     TASK_CREATED · TASK_PLAN_READY · TASK_STARTED
           TASK_STEP_START · TASK_STEP_RESULT (VERIFIED|SKIPPED|FAILED|BLOCKED)
           TASK_REPLAN (reason) · TASK_WAITING_USER
           TASK_FINISHED (status, evidence summary) · TASK_FAILED · TASK_CANCELLED
TOOL_*     TOOL_START · TOOL_RESULT (ok/fail, duration)
PERCEPTION OBSERVATION (level, question, value) · STATE_CHANGED (branch, diff)
RISK       RISK_GATE (level, action, consequences) · RISK_ANSWER
MEMORY     EPISODE_WRITTEN · SKILL_DISTILLED · MEMORY_INVALIDATED
SYSTEM     MODE_CHANGED (normal|degraded) · MODEL_ROUTED (tier, provider)
           BUDGET_WARN (tokens/rss)
AVATAR-*   derived high-level states (§4) — computed by a bus projector,
           NOT emitted ad hoc
VOICE      VAD_SPEECH_START · STT_FINAL · TTS_DONE
```

Delivery rules: async fan-out, per-subscriber buffering with drop+counter
(slow UI never blocks the core), ordered per task, optionally appended to a
disk log for post-mortems. PII/redaction policy applied at emit time
(message contents in evidence only when task risk allowed).

---

## 4. VRM avatar — pure event mapping (no LLM per expression)

A small **projector** reduces core events to avatar states:

```text
EVA_IDLE           → idle breathing, ambient blink
EVA_THINKING       (TASK_PLANNING, MODEL_ROUTED)      → thoughtful pose
EVA_TOOL_START     (TOOL_START)                       → focused expression
EVA_TOOL_SUCCESS   (TASK_STEP_RESULT=VERIFIED)        → slight nod
EVA_TOOL_FAILED    (TASK_STEP_RESULT=FAILED)          → concerned frown
EVA_WAITING        (TASK_WAITING_USER / RISK_GATE)    → questioning, look at user
EVA_CONFUSED       (TASK_REPLAN ×2, BUDGET_WARN)      → puzzled
EVA_HAPPY          (TASK_FINISHED=success)            → smile, celebratory
EVA_SAD            (TASK_FAILED)                      → disappointed
EVA_SPEAKING       (TTS playing)                      → lip sync
EVA_QUESTION       (RISK_GATE / CLARIFY)              → hand gesture, await
```

- Same pattern as v1's `avatar_bus`/desktop pet, but fed **exclusively** by
  the v2 bus; expressions are pre-baked, zero tokens, zero latency beyond the
  bus hop (< 100 ms).
- Lip sync from TTS audio frames; visemes can be approximated from phonemes.

---

## 5. Voice pipeline

```text
MIC → VAD (voice activity detection) → wake/intent gate
    → STT (local when possible; cloud fallback) 
    → Task Router (doc 07) → core loop
    → TTS (edge-style neural voice, v1 kept) → avatar lip sync
```

Rules (doc 08):
- **No continuous audio to any model.** VAD decides when there's speech;
  wake word or push-to-talk gates the rest.
- STT results enter the same intent path as CLI text — one entry point.
- Confirmations (HIGH risk) are voice-answerable: *"Send 'phir kayaa?' to
  Saad Bhai. Confirm?" → "haan"* → `RISK_ANSWER` event.
- Latency target: speech end → first feedback < 1.5 s for Tier 0/1 intents.

---

## 6. HITL surfaces across interfaces

| Core event | CLI | Avatar | Voice |
|---|---|---|---|
| `RISK_GATE` | ⚠ prompt `[y/e/n]` | questioning face | spoken ask |
| `TASK_WAITING_USER` | blocked message + instructions | concerned, idle | spoken ask |
| `TASK_FINISHED` | verdict + evidence | smile/frown | short spoken summary |
| `MODE_CHANGED` | banner `mode: degraded (reason)` | neutral notice | silent |

Same event, three renderings — behavior parity across interfaces is a test
assertion (doc 11).

---

## 7. Acceptance criteria

- [ ] Killing/restarting the CLI while a task runs loses no events already
      emitted (bus log replay) and the new CLI re-renders current state.
- [ ] Avatar expression changes derive ONLY from bus events (no direct
      core→avatar calls in code review).
- [ ] `TASK_FINISHED(status=success)` in CLI ⇔ completion condition verified
      in core ⇔ `EVA_HAPPY` on avatar ⇔ spoken success — always in sync.
- [ ] Voice path works offline for wake + Tier 0 commands (doc 08 degraded).
