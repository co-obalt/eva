# EVA v2 — Build Report (autonomous overnight build)

> Requests: read all docs · accept a plan · divide into phases · build the whole
> system at peak level · decide everything myself. This is the delivery report.

## Phase status vs doc 10

| Phase | Status | Evidence |
|---|---|---|
| 1 — Agent Core | ✅ | `eva/core/*` (task/state/events/contracts/risk/recovery/checkpoint/orchestrator). Tests: S1 full flow, S1b no-op, S1c QR-block, S5a-c honest failures, S4 risk gates, S2 idempotency, checkpoint resume — all green. |
| 2 — State + Perception | ✅ real | `perception/`: PolicyGate (INV-1..4), engine with cache/invalidate/verify-polling, L1 (psutil+Win32), L2 (Win32 control tree), L3 (Chrome CDP over aiohttp websocket, profile scan via Local State), L4 (WhatsApp DOM state + actions), L5/L6/L7 behind the vision gateway. **Live on this machine** (see below). |
| 3 — Tool Ecosystem | ✅ | 30 contract tools: os, files, browser (CDP-first), terminal (allowlist, metachar-rejecting, no filesystem tunnel), apps (registry + `data/apps.json`), documents (HTML→PDF via headless Chrome), scheduler. Registry rejects missing/invalid contracts and unknown args. |
| 4 — Memory | ✅ | SQLite WAL (`data/eva.db`), confidence envelope + decay, FTS backend, episodes/facts/profile/skills, distiller (≥2 stable verified runs), quarantine on failures, bounded 800-token retrieval, working-memory wipe. Tests: S3a-c, corroboration, invalidation, budget. |
| 5 — Intelligence | ✅ | Provider pool Groq→APINEX(RPM-aware)→local with circuit breakers; router with response cache + per-task ledger memo (same question = 1 call); context builder with hard 4K budget + priority trimming; LlmPlanner with structural validation (registered tools only, typed args), bounded retries, repair; degraded mode + `/perf`. Tests: S7a-d, offline honesty. |
| 6 — Interfaces | ✅ | eDEX rich CLI (renderer only) with the doc-09 command set and fail-safe confirmations; headless `--run/--json/--yes`; AvatarBus projector (queue + 3-fail/30 s breaker); edge-tts voice with playback fallback; persona mood vector + 13 archetypes (flavor only). |
| 7 — Autonomy | ✅ core | Persistent scheduler (reminders fire as bus events, survive restarts), file watchers with kill switch, interval jobs. Tests + live reminder row persisted. |

## Verification evidence (numbers, not vibes)

```
python -m pytest -q      →  71 passed
```

Live on the real machine (this laptop):

* `cpu usage batao` → FINISHED, verified, **0 model calls**, real CPU/RAM/disk.
* `open notepad` → launched once, verified via L1 process list.
* `open notepad` (2nd run) → **"Already done — nothing to do", 0 actions, 0.07 s**
  — the exact v1 anti-pattern (double launch) is structurally dead.
* `open whatsapp` → **BLOCKED** with the precise "Chrome is running without a
  debug port…" ask (no retry storm; no fake success).
* `band karo notepad` → terminated 1 process, verified via L1.
* `remind me in 30 minutes to check the build` → FINISHED, job persisted in
  `data/eva.db` (`scheduled_jobs`).
* CLI boots (`python eva_cli.py`), renders the eDEX banner, `/perf` shows
  RSS ≈ 59 MB (budget ≤ 250 MB idle), providers honestly marked unusable
  without keys.

## Key autonomous decisions (user asleep)

1. **Async core now** (doc 12 had staged it): real CDP/subprocess I/O is
   async; loop logic unchanged from the doc-03 pseudocode.
2. **CDP attach policy**: operate the user's real Chrome; never silently launch
   a second browser; BLOCKED with the flag instruction when unattachable.
3. **ToolResult.blocked** mechanism: user-fixable blockers (login, debug port,
   unknown app) end in `BLOCKED` + exact ask, instead of burning retries.
4. **Terminal hardening**: filesystem mutations removed from the allowlist so
   nothing can tunnel under the risk gate (`del`/`move` belong to `files.*`).
5. **Read-only tools** carry an explicit L0 `reads.*` completion plus real
   payload in outputs — the weakest acceptable evidence, only for no-mutation
   queries.
6. **No new dependencies**; vision/OCR/voice/Mem0/Browser-Use stay optional and
   degrade to UNKNOWN/BLOCKED honestly.

## Deliberately deferred (doc 10 "explicitly deferred", unchanged)

Embeddings/vector search · Mem0 backend (interface reserved) · Browser-Use
Layer B · MCP adapter · STT/whisper input · multi-monitor abstraction ·
web dashboard · heavy local models. Each has a documented seam; none blocks the
core loop or the budget.

## How to resume work

1. `python -m pytest -q` — keep it green.  Every doc-11 mechanism has at least
   one test that fails if the mechanism is removed.
2. Put real keys in `.env` (`GROQ_API_KEY`) to exercise the Tier-2 planner and
   the full WhatsApp LLM path end-to-end.
3. For daily WhatsApp use, add `--remote-debugging-port=9222` (plus your AIOU
   profile's `--profile-directory`, printed by `python eva.py --profiles`) to
   the Chrome shortcut; then
   `python eva.py --run "open whatsapp in aiou profile, saad bhai ka archived chat kholo"`.
4. Open items with precise locations: WA selector drift (`perception/l4_dom.py`),
   L2 UIA upgrade (`perception/l2_a11y.py`), OCR availability (`l5_vision.py`).
