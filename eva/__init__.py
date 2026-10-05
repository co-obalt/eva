"""EVA v2 — resource-efficient personal agentic OS.

Package layout (docs 02/12):
  core/        orchestrator, task model, state, events, contracts, risk, recovery, checkpoints
  perception/  L0-L7 ladder, policy gate, observation cache
  tools/       contract-driven tool inventory
  memory/      episodic/semantic/procedural/profile stores + distiller
  models/      tiered model router, context builder, planner
  runtime/     budget/degraded mode, scheduler, watchers
  ui/          eDEX CLI renderer + headless runner (no intelligence here)
"""

__version__ = "2.0.0"
