# EVA v2 — a stateful, verification-first personal agentic OS

> Built from `docs/01–13` + `EVA_INTELLIGENT_RESOURCES.md` + `EVA_VRM_INTEGRATION_PLAN.md`.
> The accepted build plan and autonomous decisions are in [PLAN.md](PLAN.md);
> the delivery report is in [BUILD_REPORT.md](BUILD_REPORT.md).

EVA understands the machine as a **stateful environment**, acts through the
**cheapest reliable method**, **verifies every effect before claiming success**,
**learns skills** from repeated work, and **escalates to vision only when
necessary**. The LLM proposes; the system disposes.

---

## Quickstart

```bash
# 1) Python deps (already present on this machine): pydantic aiohttp psutil rich pytest
python -m pip install -r requirements.txt

# 2) Model keys — copy the example and fill what you have:
cp .env.example .env        # GROQ_API_KEY=...  (APINEX_API_KEY=... optional)

# 3) Three simple ways to run it (each is a separate process):
python eva_cli.py          # interactive eDEX CLI (+ speech)
python eva_cli.py --hud    # full-screen HUD (eDEX-UI style: system + EVA + feed)
python eva_dashboard.py    # local web dashboard → http://127.0.0.1:8765
python eva_pet.py          # desktop avatar window showing EVA's live state

# 4) Headless one-shot (scriptable, JSON):
python eva.py --run "cpu usage" --json
python eva.py --run "open whatsapp in aiou profile, saad bhai ka archived chat kholo"
python eva.py --run "open notepad"          # run twice → second is a verified no-op

# 5) Tests (fake-world doc-11 suite):
python -m pytest -q
```

### Chrome / WhatsApp setup (important, honest attach policy)

EVA attaches to **your real Chrome session** over CDP — it never silently
launches a second profile:

* Keep one Chrome shortcut with `--remote-debugging-port=9222`
  (add `--profile-directory="<your AIOU profile dir>"` — `python eva.py
  --profiles` prints it; EVA resolves `aiou` / any `"<X> profile"` against the
  real Chrome profile list at runtime, nothing is baked in), **or**
  close Chrome and let EVA launch it with the flag when a browser task starts.
* If Chrome is already running without the flag, EVA reports
  `BLOCKED` with that exact instruction — never a fake success.

---

## What it does

| Command examples | Path | LLM calls |
|---|---|---|
| `open chrome` · `chrome kholo` · `band karo notepad` | Tier 0 recipe | **0** |
| `cpu usage batao` · `disk status` | Tier 0 recipe (L1) | **0** |
| `list windows` · `screenshot lo` (gated L6) | Tier 0 recipe | **0** |
| `create folder "AIOU Reports" on desktop` | files.mkdir + fs verify | **0** |
| `organize my downloads` | files.organize (MEDIUM) | **0** |
| `run git status` | allowlisted terminal | **0** |
| `remind me in 30 minutes to check the build` | persistent scheduler | **0** |
| `open whatsapp in aiou profile, saad bhai ka archived chat kholo` | Chrome CDP + WhatsApp DOM (L3/L4) | **0** (recipe) |
| novel task (e.g. `design …`, multi-app flows) | Tier-2 plan + verify | 1–2 |
| repeat of a novel task ×2 | **distilled skill** | **0** (Tier 0) |

House rules enforced in code — not prose:

* **P2/P3** executed ≠ achieved; already-true effects are `SKIPPED_ALREADY_DONE`
  (run `open notepad` twice and watch it do nothing the second time).
* **P6** verdicts come from the machine-checkable **completion condition**;
  "done N steps" is impossible by code path.
* **INV-1..4** L5/L6/L7 (OCR/screenshot/vision) and coordinate clicks are
  rejected pre-execution without `reason + exhausted_levels` / structural
  justification, and are counted per task.
* **P9** risk engine: HIGH → confirm, CRITICAL → exact consequences + confirm,
  timeout/decline → **cancel** (fail-safe). LLMs cannot set or bypass risk.
* **P10** effect tokens: a resumed task can never double-send a message.
* **P8** memory carries confidence/decay/verify hooks; quarantined skills and
  <0.4-confidence memories never drive action.

---

## Architecture (docs 02/03, doc 12 skeleton)

```
eva/
├── core/         task · state(3-valued predicates) · events · contracts · risk
│                 recovery · checkpoints+journal · ORCHESTRATOR (the controller)
├── perception/   policy gate (INV-1..4) · engine(cache/verify) · L1 Win32/psutil
│                 L2 a11y · L3 Chrome CDP · L4 WhatsApp DOM · L5-7 gated
├── tools/        os · files · browser · terminal · apps · documents · scheduler
├── apps/         deterministic app launch registry (data/apps.json overrides)
├── memory/       SQLite WAL store · confidence envelope · FTS backend · distiller
├── models/       providers(Groq→APINEX→local+breakers) · router(cache/ledger)
│                 context builder (hard budgets) · LlmPlanner(validated plans)
├── tasks/        Tier-0 recipes (EN+Roman Urdu) · skills · TaskManager
├── runtime/      budget/degraded mode · scheduler · watchers
├── persona/      mood vector + 13 archetypes (flavor only)
├── avatar/       AVATAR agent-bus projector (queue + 3-fail/30s breaker)
├── voice/        edge-tts + playback fallback chain
└── ui/           eDEX CLI (renderer) · headless runner
```

Single process, single SQLite file (`data/eva.db`, WAL): tasks, checkpoints,
effect journal, cache, episodes, facts, profile, skills, scheduler, watchers.

## CLI commands

`/state` `/debug` `/perf` `/tasks` `/memory` `/skills` `/explain` `/mode`
`/archetype` `/mood` `/remind <min> <text>` `/watch on|off` `/help` `/exit`

## Resource budget (doc 08, measured on this machine)

* idle RSS ~60 MB (budget ≤ 250 MB), active well under 600 MB
* Tier-0 responses ~0.07–3 s incl. process start; 0 JVM/LLM calls
* one foreground task at a time; UI lock serializes pointer/focus actions

## Honest limitations (by design, not bugs)

* **WhatsApp DOM selectors** track a volatile app; a mismatch yields `UNKNOWN`
  → `BLOCKED`, never a guess. Update `eva/perception/l4_dom.py` when WA ships
  a redesign.
* **L2** covers Win32-classic controls (a11y handles); full UI Automation is
  the documented upgrade path.
* **OCR (L5)** needs `tesseract` on PATH; **vision (L7)** needs `VISION_MODEL`
  + `EVA_ALLOW_VISION=1` — both stay UNKNOWN otherwise.
* **STT** (voice input) and the Mem0 backend are deferred interfaces, per docs.
* Cloud providers require keys in `.env`; without them novel tasks report
  honestly instead of hallucinating a plan.

## Tests

```bash
python -m pytest -q        # 71 tests: S1-S8 assertions, fake-world harness
```

Covers: WhatsApp S1/S1b/S1c + continuation, honest failures S5, risk S4,
idempotency S2/S2c, checkpoints/resume, INV suite S6, router economy S7,
learning curve S3, recipes, real registry contracts, scheduler/watchers.
"# eva" 
