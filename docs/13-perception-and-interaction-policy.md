# 13 — Perception & Interaction Policy

> **The rules that keep EVA from accidentally becoming an Open-Claw-style
> screenshot→vision→click loop.** Ten hard rules, each mapped to its
> invariant ID, its future enforcing module, and its test assertion.
> These are **spec-level law** from Phase 2 onward: violating code is wrong
> by definition even when tests are green (same standard as doc 01's
> twelve principles).

Status: **policy frozen before Reality Bridge implementation** — written so
the Phase-2 perception layer is built *against* these rules, not retrofitted
to them.

---

## The ten rules

```text
 1. Never use vision when structured state is available.
 2. Never use coordinates when a semantic target is available.
 3. Never screenshot merely to verify something that can be verified
    through state/API/filesystem.
 4. Never call an LLM when deterministic logic can answer.
 5. Never send the entire world state to the model.
 6. Every expensive observation must have a reason.
 7. Every action must have a verification strategy.
 8. Reuse valid observations instead of re-observing.
 9. Vision is an escalation mechanism, not a default sensor.
10. Mouse coordinates are last-resort interaction.
```

---

## Rule → enforcement map

| # | Rule | Existing anchor | Enforcing module (Phase 2+) | Test assertion |
|---|---|---|---|---|
| 1 | No vision when structured state available | doc 04 **INV-1**, P7b (doc 01) | `perception/policy.py` — L5/L6/L7 requests rejected unless `exhausted_levels` recorded | S6e, S6g |
| 2 | No coordinates when semantic target available | doc 04 **INV-2**, doc 05 executor rule 7 | `perception/policy.py` — `click(x,y)` requires structural-UNKNOWN justification | S6f |
| 3 | No screenshot as a verification method | P4 (doc 01), doc 04 §4 state-diffing | contract `verification.method` must name a structural level; L6/L7 method rejected at registration | S6a, S6h |
| 4 | No LLM when deterministic logic can answer | P7 reasoning ladder, doc 07 Tier 0 | `models/router.py` — Tier 0 checked first; Tier >0 requires a registered escalation reason | S7a (0 provider calls) |
| 5 | Never send entire world state to model | P11 (doc 01), doc 07 §4 context rules | `models/context_builder.py` — context assembled per-decision from named keys only | S7b (prompt ≤ 4K) |
| 6 | Every expensive observation has a reason | doc 04 **INV-3** | `perception/policy.py` — `reason` + `exhausted_levels` mandatory on L5+ | S6g |
| 7 | Every action has a verification strategy | doc 05 contracts (`_require_verification` — **already enforced in code**) | `core/contracts.py` (done) + registry tests | existing contract tests |
| 8 | Reuse valid observations | doc 04 §4 observation cache | `perception/engine.py` — cache by question+branch, `ttl`/`max_age`, invalidate-on-action | S6h, cache-hit assertion |
| 9 | Vision = escalation, not default sensor | P7b, doc 04 **INV-1/INV-4** | `perception/vision_gateway.py` — the ONLY module allowed to call a vision model | S6c, S6e |
| 10 | Mouse coordinates = last resort | doc 04 **INV-2** (ACT hierarchy) | `perception/policy.py` + tool contracts flagged `structural_target: true` | S6f |

Rules 1/2/6/9/10 all funnel through **one gate**: `perception/policy.py`
(the "Perception Policy gate" named in doc 04). Rules 3/7 live in the
contract layer (mostly enforced today). Rules 4/5 live in the model router
(Phase 5). Rule 8 lives in the observation engine (Phase 2).

---

## Non-negotiables restated (the short form)

1. **Structured before pixels.** a11y / DOM / API / filesystem before OCR,
   screenshot, or vision — every time, no exceptions without a recorded
   justification.
2. **Semantic before geometric.** `focus_window("WhatsApp")` and
   `click(control="Saad Bhai")` — never `click(x=721, y=418)` while any
   structural target resolution exists.
3. **State before pixels (verification).** A downloaded file is verified by
   the filesystem, a navigation by the URL, a window by enumeration —
   a screenshot is a *last-resort evidence artifact*, not a check.
4. **Deterministic before statistical.** Tier 0 code answers first; the LLM
   is consulted only when Tier 0 declines (registered reason), never as the
   default reader of the situation.
5. **Assembled before dumped.** The model receives the named facts this
   decision needs — never the whole `WorldState`.
6. **Justified before paid.** L5/L6/L7, vision, OCR: `reason` +
   `exhausted_levels` or the gate rejects the request pre-execution.
7. **Verified before succeeded.** Unchanged from Phase 1: contract or it
   didn't happen.
8. **Cached before re-observed.** Fresh within `ttl` → reuse. Action
   invalidates only the branches its `effects` touch.
9. **Escalation, not eyes.** Vision runs behind `vision_gateway.py` alone —
   no tool may embed its own screenshot call.
10. **Coordinates are a smell.** A coordinate action in a task log where a
    structural path existed is a defect, regardless of outcome.

---

## Relationship to existing docs

- Doc 01 **P7b** — the principle (why).
- Doc 04 **Hard invariants INV-1..INV-4** + dual hierarchy + observation
  cache (what, in context of the ladder).
- Doc 05 executor rule 7 (how actions comply).
- Doc 11 S6e–S6h + anti-regression watchlist (how we prove it).
- Doc 10 Phase 2 exit criteria (when it must be live).
- **This doc** — the single enforceable checklist tying all of the above
  together, so no future contributor needs to reconstruct the rule set from
  five places.

---

## Acceptance criteria for this doc

- [ ] Phase-2 `perception/policy.py` implements rules 1/2/6/9/10 and its
      tests reference these rule numbers.
- [ ] Every L5+ call in any log carries `reason` + `exhausted_levels`
      (rule 6) — asserted, not eyeballed.
- [ ] S6e–S6h green (doc 11) = rules 1/2/3/8 proven in the fake world.
- [ ] Live "Open Notepad." run records `l5=l6=l7=coord_actions = 0`
      (rule 1/2/9/10 on real hardware).
