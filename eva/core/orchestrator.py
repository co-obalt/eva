"""Orchestrator — the controller (doc 03).

Loop: DISCOVER -> PLAN (if needed) -> PRECHECK -> RISK GATE -> EXECUTE ->
VERIFY -> CHECKPOINT -> (RECOVERY | next step) -> COMPLETION VERDICT.

Guarantees enforced here in code (not prose):
  P2  executed != achieved: step success only from fresh observation
  P3  state before action: already-true effects SKIPPED_ALREADY_DONE
  P6  verdict derives ONLY from the completion condition
  P9  risk gate cannot be bypassed; decline/timeout cancels (fail-safe)
  P10 effect-token tools never double-execute across retries/resumes
  INV-2 coordinate actions rejected without recorded structural-UNKNOWN justification
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .checkpoint import CheckpointStore
from .contracts import IdempotencyClass, ToolContext, ToolRegistry, ToolResult
from .events import (ACTOR_ACTION, EventBus, IDEMPOTENT_SKIP, POLICY_REJECTED, RISK_ANSWER,
                     RISK_GATE, RULE_ENFORCED, TASK_CANCELLED, TASK_ESCALATED, TASK_FINISHED,
                     TASK_FAILED, TASK_PLAN_READY, TASK_REPLAN, TASK_STARTED, TASK_STEP_RESULT,
                     TASK_STEP_SKIPPED, TASK_STEP_START, TASK_WAITING_USER, TOOL_RESULT, TOOL_START)
from .recovery import BlockerClass, RecoveryAction, RecoveryPolicy
from .risk import RiskDecision, RiskGate
from .state import Observation, ObservationRequest, Predicate, Truth
from .task import (Evidence, FailureRecord, RiskLevel, Step, StepStatus, Task, TaskStatus)


class PlannerLike(Protocol):
    async def plan(self, task: Task, *, state: Any = None, memory_slice: dict | None = None) -> list[Step]: ...
    async def repair(self, task: Task, *, reason: str, state: Any = None) -> list[Step] | None: ...


class PerceptionLike(Protocol):
    async def observe(self, requests: list[ObservationRequest], max_age: float = 5.0) -> dict[str, Observation]: ...
    async def verify(self, predicates: list[Predicate], timeout_s: float = 8.0) -> dict[str, Observation]: ...


class MemoryLike(Protocol):
    def record_episode(self, task: Task) -> None: ...
    def clear_working(self, task_id: str) -> None: ...


ConfirmFn = Any  # async (RiskDecision, Step) -> bool


@dataclass
class RunResult:
    task: Task
    status: TaskStatus
    message: str
    observations: dict[str, Observation] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is TaskStatus.FINISHED


class Orchestrator:
    def __init__(self, *,
                 registry: ToolRegistry,
                 perception: PerceptionLike,
                 bus: EventBus,
                 store: CheckpointStore,
                 gate: RiskGate,
                 confirm: ConfirmFn | None = None,
                 planner: PlannerLike | None = None,
                 actor: Any = None,
                 recovery: RecoveryPolicy | None = None,
                 memory: MemoryLike | None = None,
                 distiller: Any = None,
                 settings: Any = None,
                 ui_lock: asyncio.Lock | None = None,
                 max_steps: int = 20,
                 completion_timeout_s: float = 3.0):
        self.registry = registry
        self.perception = perception
        self.bus = bus
        self.store = store
        self.gate = gate
        self.confirm = confirm
        self.planner = planner
        self.actor = actor          # M1: one-action-per-cycle loop (see _actor_loop)
        self.recovery = recovery or RecoveryPolicy()
        self.memory = memory
        self.distiller = distiller
        self.settings = settings
        self._provided_lock = ui_lock
        self.ui_lock = ui_lock
        self.max_steps = max_steps
        self.completion_timeout_s = completion_timeout_s
        self._cancel = False
        self.current: Task | None = None

    # ------------------------------------------------------------------ public
    async def run(self, task: Task) -> RunResult:
        task.validate()
        self._cancel = False
        return await self._run(task)

    async def resume(self, task_id: str) -> RunResult:
        task = self.store.load_task(task_id)
        if task is None:
            raise KeyError(f"no stored task {task_id!r}")
        task.resumed = True
        if task.status is TaskStatus.FINISHED:
            return RunResult(task, task.status, task.result_message or "task already finished")
        return await self._run(task)

    def cancel(self) -> None:
        self._cancel = True

    # ------------------------------------------------------------------- inner
    async def _run(self, task: Task) -> RunResult:
        self._cancel = False
        if self.ui_lock is None:
            self.ui_lock = asyncio.Lock()
        try:
            return await self._run_inner(task)
        finally:
            if self._provided_lock is None:
                self.ui_lock = None

    async def _run_inner(self, task: Task) -> RunResult:
        self.current = task
        self._bind(task)
        self.bus.emit(TASK_STARTED, task_id=task.id, goal=task.goal)
        self.store.save_task(task)

        # --- discovery (cheap ladder first; doc 03 §4) -------------------------
        task.status = TaskStatus.DISCOVERING
        hints = self._discovery_requests(task)
        await self.perception.observe(hints, max_age=30.0)

        # --- is it already done? (P3 at task level; doc 03 §10) ---------------
        early = await self._completion_observations(task)
        if early == Truth.TRUE:
            return self._finish_noop(task)

        # --- resume: re-verify done steps, reset unfinished ones ---------------
        if task.resumed:
            await self._revalidate_done_steps(task)
            self._prepare_resume(task)

        # --- actor mode (M1): decide one action per cycle for the live situation --
        if task.mode == "actor":
            actor_outcome = await self._actor_loop(task)
            if actor_outcome is not None:
                return actor_outcome
            return await self._final_verdict(task)

        # --- planning (plan before execution; doc 03 §2) -----------------------
        if not task.plan:
            task.status = TaskStatus.PLANNING
            if self.planner is None:
                return await self._finish_failed(task, "no planner available for this task")
            try:
                plan = await self.planner.plan(task, state=None, memory_slice=self._memory_slice(task))
            except Exception as exc:  # honest failure, never fake completion
                return await self._finish_failed(task, f"planning failed: {exc}")
            task.plan = plan
            task.planned_by = task.planned_by or "planner"
            self.bus.emit(TASK_PLAN_READY, task_id=task.id,
                          steps=[{"n": s.n, "intent": s.intent, "tool": s.tool} for s in plan])
            self.store.save_task(task)

        task.status = TaskStatus.EXECUTING

        # --- step loop ----------------------------------------------------------
        while True:
            if self._cancel:
                return await self._finish_cancelled(task, "cancelled by user")
            if task.counters.actions_run >= self.max_steps:
                repaired = await self._try_repair(task, reason=f"step budget {self.max_steps} reached")
                if not repaired:
                    return await self._finish_failed(task, f"step budget exhausted ({self.max_steps})")
                continue

            step = task.next_step()
            if step is None:
                break

            outcome = await self._run_step(task, step)

            if outcome is TaskStatus.CANCELLED:
                return RunResult(task, TaskStatus.CANCELLED, task.result_message)
            if outcome is TaskStatus.BLOCKED:
                break
            if outcome is TaskStatus.FAILED:
                break

            # early exit: completion condition satisfied mid-plan (doc 03 §4)
            if await self._completion_observations(task) == Truth.TRUE:
                break

        # --- fast-lane escalation (M1): a frozen step list that failed is not a
        # verdict — the general actor loop takes over from the live state.
        if self._should_escalate(task):
            previous = task.planned_by or "plan"
            task.mode = "actor"
            task.context["escalated_from"] = previous
            task.planned_by = f"actor(escalated from {previous})"
            task.counters.escalations += 1
            self.store.save_task(task)
            self.bus.emit(TASK_ESCALATED, task_id=task.id,
                          **{"from": previous, "reason": "fast lane did not finish the task"})
            escalated = await self._actor_loop(task)
            if escalated is not None:
                return escalated

        return await self._final_verdict(task)

    # ------------------------------------------------------------------ helpers
    def _bind(self, task: Task) -> None:
        for target in (self.perception, getattr(self.perception, "policy", None)):
            binder = getattr(target, "bind_task", None)
            if callable(binder):
                try:
                    binder(task)
                except Exception:
                    pass

    def _discovery_requests(self, task: Task) -> list[ObservationRequest]:
        seen: dict[str, ObservationRequest] = {}
        for pred in task.completion.predicates():
            seen[pred.key] = ObservationRequest(path=pred.path, args=pred.args)
        for step in task.plan:
            for pred in step.preconditions + step.expected_effect:
                seen[pred.key] = ObservationRequest(path=pred.path, args=pred.args)
        return list(seen.values())

    def _memory_slice(self, task: Task) -> dict | None:
        retrieve = getattr(self.memory, "retrieve", None) if self.memory else None
        if callable(retrieve):
            try:
                return retrieve(task)
            except Exception:
                return None
        return None

    async def _completion_observations(self, task: Task, *,
                                       timeout_s: float | None = None) -> Truth:
        preds = task.completion.predicates()
        budget = self.completion_timeout_s if timeout_s is None else timeout_s
        obs = await self.perception.verify(preds, timeout_s=budget)
        return task.completion.evaluate(obs)

    async def _revalidate_done_steps(self, task: Task) -> None:
        """Resume rule (doc 03 §7): verified steps are NOT blindly re-executed,
        but their effects are re-checked; a no-longer-true effect demotes the step."""
        done = [s for s in task.plan if s.status.done]
        if not done:
            return
        preds = [p for s in done for p in s.expected_effect]
        obs = await self.perception.verify(preds, timeout_s=1.5)
        for s in done:
            ok = all(p.matches_observation(obs.get(p.key)) is Truth.TRUE for p in s.expected_effect)
            if not ok:
                s.status = StepStatus.PENDING
                s.note = f"resume: effect no longer holds ({s.effect_describe()})"

    def _prepare_resume(self, task: Task) -> None:
        """Blocked/failed steps become pending again — the world may have changed
        (user scanned QR, fixed permissions...). Verified steps stay verified."""
        for s in task.plan:
            if s.status in (StepStatus.BLOCKED, StepStatus.FAILED):
                s.note = f"resume: reset {s.status.value} -> PENDING"
                s.status = StepStatus.PENDING
                s.attempts = 0

    # --------------------------------------------------------------- step run
    async def _run_step(self, task: Task, step: Step, *,
                        recover: bool = True) -> TaskStatus | None:
        """Execute + verify one step through every gate.

        recover=True keeps the plan-mode behaviour (retry/alternative/repair).
        recover=False is actor mode: a failed precondition or an unverified effect
        is *feedback* for the next decision, not a terminal wall — except for
        user-fixable tool blockers (blocked=True), which always park the task."""
        tool = self.registry.require(step.tool)
        contract = tool.contract
        # contracts are authoritative for effects/preconditions/verification (P1)
        step.expected_effect = contract.resolve_effects(step.args)
        step.preconditions = contract.resolve_preconditions(step.args)
        step.verification_timeout_s = contract.verification.timeout_s
        step.structural_target = not (contract.structural_target and ("x" in step.args or "y" in step.args))
        step.status = StepStatus.EXECUTING
        step.started = time.time()

        self.bus.emit(TASK_STEP_START, task_id=task.id, step=step.n, intent=step.intent, tool=step.tool)

        # -- P3: state before action -------------------------------------------
        reqs = [ObservationRequest(path=p.path, args=p.args)
                for p in (step.preconditions + step.expected_effect)]
        obs = await self.perception.observe(reqs, max_age=5.0)

        if step.expected_effect and all(
                p.matches_observation(obs.get(p.key)) is Truth.TRUE for p in step.expected_effect):
            return self._skipped(task, step, obs)

        failed_pre = [p for p in step.preconditions if p.matches_observation(obs.get(p.key)) is Truth.FALSE]
        unknown_pre = [p for p in step.preconditions if p.matches_observation(obs.get(p.key)) is Truth.UNKNOWN]
        if failed_pre or (unknown_pre and self.gate.classify(contract, step.args, task).level.rank >= RiskLevel.HIGH.rank):
            reason = "precondition not satisfied: " + "; ".join(
                p.describe() for p in failed_pre or unknown_pre)
            if not recover:
                # actor mode: the world isn't as needed yet — the actor may fix it
                return self._failed_step(task, step, reason, kind="precondition",
                                         status="PRECONDITION")
            return await self._blocked(
                task, step, obs, reason=reason,
                login_related=any("login" in p.path for p in failed_pre),
                unknown=bool(unknown_pre and not failed_pre),
            )

        # -- INV-2: coordinate policy gate -------------------------------------
        policy = getattr(self.perception, "policy", None)
        if policy is not None:
            allowed, why = policy.check_coordinate_action(contract, step.args, task)
            if not allowed:
                task.counters.policy_rejections += 1
                self.bus.emit(POLICY_REJECTED, task_id=task.id, step=step.n, reason=why, tool=step.tool)
                step.attempts = step.max_attempts  # illegal action: never retried
                step.status = StepStatus.FAILED
                task.record_failure(FailureRecord(step=step.n, kind="policy_rejection",
                                                  detail=why, attempt=step.attempts))
                task.bump_non_verified()
                if not recover:
                    return self._failed_step(task, step, why, kind="policy_rejection",
                                             status="REJECTED", bump=False)
                return await self._recover_or_fail(task, step, why)

        # -- P9: risk gate (no LLM can bypass) -----------------------------------
        decision = self.gate.classify(contract, step.args, task)
        step.risk = decision.level
        if decision.requires_confirmation:
            self.bus.emit(RISK_GATE, task_id=task.id, step=step.n, level=decision.level.value,
                          action=contract.name, consequences=decision.consequences,
                          title=decision.title)
            approved = await self._ask(decision, step, task)
            self.bus.emit(RISK_ANSWER, task_id=task.id, step=step.n, approved=approved)
            if not approved:
                task.result_message = (f"CANCELLED at step {step.n} ({contract.name}): "
                                       f"user declined — {decision.consequences}")
                task.status = TaskStatus.CANCELLED
                self.store.save_task(task)
                self.bus.emit(TASK_CANCELLED, task_id=task.id, reason="declined", step=step.n)
                return TaskStatus.CANCELLED

        # -- P10: effect-token idempotency ---------------------------------------
        if contract.idempotency is IdempotencyClass.EFFECT_TOKEN:
            prior = self.store.effect_token_get(task.id, step)
            if prior is not None:
                self.bus.emit(IDEMPOTENT_SKIP, task_id=task.id, step=step.n, tool=step.tool,
                              note="effect token already recorded — side effect not repeated")
                step.status = StepStatus.SKIPPED_ALREADY_DONE
                step.ended = time.time()
                step.evidence.append(Evidence(level="L0", path=f"idempotency/{step.tool}",
                                              value=True, note="effect_token dedup"))
                self.store.save_checkpoint(task)
                return None

        # -- execute ---------------------------------------------------------------
        self.bus.emit(TOOL_START, task_id=task.id, step=step.n, tool=step.tool,
                      args=_redact(step.args))
        result = await self._execute(task, step, contract)

        already = self.store.journal_action_count(task.id, step.action_hash)
        self.store.journal_add(task.id, step.action_hash, step.tool, step.args,
                               "ok" if result.ok else "error", result.outputs,
                               reason=step.intent)
        if result.ok and already > 0:
            task.counters.redundant_actions += 1
        self._keep_outputs(task, step, result)

        self.bus.emit(TOOL_RESULT, task_id=task.id, step=step.n, tool=step.tool, ok=result.ok,
                      duration_s=round((step.ended or time.time()) - (step.started or time.time()), 3),
                      note=result.note)
        if result.mutated:
            self._invalidate_for(step)

        # user-fixable blocker reported by the tool itself -> BLOCKED + precise ask
        if not result.ok and result.blocked:
            step.status = StepStatus.BLOCKED
            step.ended = time.time()
            task.bump_non_verified()
            ask = result.ask or "I need you to fix that, then tell me to continue."
            task.record_failure(FailureRecord(step=step.n, kind="blocked", detail=result.note))
            task.status = TaskStatus.BLOCKED
            task.result_message = f"BLOCKED at step {step.n} ({step.intent}): {result.note}. {ask}"
            self.store.save_task(task)
            self.bus.emit(TASK_STEP_RESULT, task_id=task.id, step=step.n, status="BLOCKED",
                          reason=result.note)
            self.bus.emit(TASK_WAITING_USER, task_id=task.id, step=step.n, blocker="user_fixable",
                          ask=ask, reason=result.note)
            return TaskStatus.BLOCKED

        if contract.idempotency is IdempotencyClass.EFFECT_TOKEN and result.ok:
            self.store.effect_token_put(task.id, step, result.outputs)

        # -- P2/P4: verify with cheapest sufficient signal ---------------------------
        step.status = StepStatus.VERIFYING
        fresh = await self.perception.verify(step.expected_effect, timeout_s=step.verification_timeout_s)
        if step.expected_effect and all(
                p.matches_observation(fresh.get(p.key)) is Truth.TRUE for p in step.expected_effect):
            step.evidence.extend(Evidence.from_observation(o) for o in fresh.values())
            task.evidence.extend(Evidence.from_observation(o) for o in fresh.values())
            self._mark_verified(task, step)
            return None

        # -- not verified -> failure + recovery --------------------------------------
        observed_desc = "; ".join(
            f"{p.describe()} -> {fresh.get(p.key).truth.value if fresh.get(p.key) else 'NO OBS'}"
            for p in step.expected_effect
        )
        step.attempts += 1
        task.record_failure(FailureRecord(
            step=step.n, kind="tool_error" if not result.ok else "verification_failed",
            expected=step.effect_describe(), observed=observed_desc,
            detail=result.note, attempt=step.attempts))
        task.bump_non_verified()
        step.status = StepStatus.FAILED
        step.ended = time.time()
        step.note = f"effect not observed: {observed_desc}"
        self.bus.emit(TASK_STEP_RESULT, task_id=task.id, step=step.n, status="FAILED",
                      expected=step.effect_describe(), observed=observed_desc)
        if not recover:
            return TaskStatus.FAILED
        return await self._recover_or_fail(task, step,
                                           f"effect not observed after attempt {step.attempts}: {observed_desc}")

    def _failed_step(self, task: Task, step: Step, reason: str, *, kind: str,
                     status: str = "FAILED", bump: bool = True) -> TaskStatus:
        """Actor-mode feedback: the step failed, the task is not over."""
        step.status = StepStatus.FAILED
        step.ended = time.time()
        step.note = reason
        task.record_failure(FailureRecord(step=step.n, kind=kind, detail=reason,
                                         observed=reason, attempt=step.attempts))
        if bump:
            task.bump_non_verified()
        self.bus.emit(TASK_STEP_RESULT, task_id=task.id, step=step.n, status=status,
                      reason=reason, expected=step.effect_describe())
        return TaskStatus.FAILED

    # ------------------------------------------------------------------ actor loop
    async def _actor_loop(self, task: Task) -> RunResult | None:
        """One action per cycle until the completion condition holds.

        P6 is checked at the top of every cycle, so a `finish` proposal can never
        end a task the world does not agree with. Every budget stop is either a
        single bounded re-prompt or an honest BLOCKED — never an infinite loop.
        """
        if self.actor is None:
            return await self._finish_failed(task, "no actor available for this task")
        task.status = TaskStatus.EXECUTING
        self.store.save_task(task)
        history = self._actor_history(task)
        wall_clock = float(self._setting("actor_wall_clock_s", 900.0))
        deadline = time.time() + wall_clock
        task.context["actor_deadline"] = deadline
        state = {"repairs": int(task.context.get("actor_repairs_used") or 0),
                 "last_key": str(task.context.get("actor_last_key") or ""),
                 "repeats": int(task.context.get("actor_repeats") or 0),
                 "finish_claims": int(task.context.get("actor_finish_claims") or 0)}

        while True:
            if self._cancel:
                return await self._finish_cancelled(task, "cancelled by user")
            # quick re-check per cycle (the loop repeats); _final_verdict is the
            # patient one, so a lagging effect still gets its full timeout there
            if await self._completion_observations(task, timeout_s=0.5) == Truth.TRUE:
                return None                       # P6: the world decides

            stop = self._actor_budget_stop(task, deadline)
            if stop:
                out = await self._actor_try_again(task, history, state, stop)
                if out is not None:
                    return out
                continue

            try:
                proposal = await self._actor_propose(task, history, state, deadline)
            except Exception as exc:                # ActorError / provider failure
                out = await self._actor_try_again(
                    task, history, state,
                    f"your last turn could not be used: {type(exc).__name__}: {str(exc)[:200]}")
                if out is not None:
                    return out
                continue

            # a remembered user rule can veto a tool outright
            veto = self._rule_veto(task, proposal)
            if veto is not None:
                task.counters.rule_rejections += 1
                self.bus.emit(RULE_ENFORCED, task_id=task.id, tool=proposal.tool, rule=veto.id,
                              instruction=veto.instruction[:200], reason=proposal.reason)
                out = await self._actor_try_again(
                    task, history, state,
                    f"a rule you must obey forbids {proposal.tool}: {veto.instruction}")
                if out is not None:
                    return out
                continue

            if proposal.kind == "finish":
                state["finish_claims"] += 1
                if await self._completion_observations(task, timeout_s=0.5) == Truth.TRUE:
                    return None           # the caller records the single final verdict
                out = await self._actor_try_again(
                    task, history, state,
                    "you claimed the task was finished but the completion condition is not "
                    "satisfied — do the missing work or ask the user")
                if out is not None:
                    return out
                continue

            key = proposal.key()
            state["repeats"] = state["repeats"] + 1 if key == state["last_key"] else 0
            state["last_key"] = key
            if state["repeats"] >= 2:
                out = await self._actor_try_again(
                    task, history, state,
                    f"you repeated the same action {state['repeats'] + 1} times without progress "
                    f"({proposal.describe()}) — change approach or ask the user")
                if out is not None:
                    return out
                state["repeats"] = 0
                state["last_key"] = ""
                continue

            step = self._actor_step(task, proposal)
            task.plan.append(step)
            self.store.save_task(task)
            outcome = await self._run_step(task, step, recover=False)
            history.append(self._actor_entry(task, step, outcome))
            self._note_rule_use(task, proposal.tool)
            self._persist_actor(task, history, state)

            if outcome is TaskStatus.CANCELLED:
                return RunResult(task, TaskStatus.CANCELLED, task.result_message)
            if outcome is TaskStatus.BLOCKED:
                return await self._final_verdict(task)
            # FAILED: the actor sees what went wrong and chooses differently

    def _actor_step(self, task: Task, proposal: Any) -> Step:
        if proposal.kind == "ask":
            return Step(n=self._next_step_n(task),
                        intent=proposal.reason or f"ask: {proposal.question}",
                        tool="agent.ask_user",
                        args={"question": proposal.question,
                              "options": "|".join(proposal.options)})
        return Step(n=self._next_step_n(task), intent=proposal.reason or proposal.describe(),
                    tool=proposal.tool, args=dict(proposal.args))

    @staticmethod
    def _next_step_n(task: Task) -> int:
        return (max((s.n for s in task.plan), default=0)) + 1

    async def _actor_propose(self, task: Task, history: list[dict[str, Any]],
                             state: dict[str, Any], deadline: float) -> Any:
        task.counters.actor_calls += 1
        task.counters.actor_tokens = task.counters.tokens_in + task.counters.tokens_out
        self.store.save_task(task)
        proposal = await self.actor.next_action(
            task, state=self._actor_state_slice(), history=history,
            memories=self._actor_memories(task), budget=self._actor_budget(task, deadline))
        if getattr(proposal, "reason", ""):
            self.bus.emit(ACTOR_ACTION, task_id=task.id, action=proposal.describe(),
                          reason=proposal.reason, kind=proposal.kind)
        return proposal

    def _setting(self, name: str, default: Any) -> Any:
        """Explicit budget values win even when they are 0 (0 = no budget at all)."""
        value = getattr(self.settings, name, None)
        return default if value is None else value

    async def _actor_try_again(self, task: Task, history: list[dict[str, Any]],
                              state: dict[str, Any], note: str) -> RunResult | None:
        """Bounded repair: one re-prompt with the reason, then an honest BLOCKED."""
        allowed = int(self._setting("actor_repairs", 1))
        if state["repairs"] < allowed:
            state["repairs"] += 1
            task.counters.repairs += 1
            history.append({"step": "-", "tool": "system", "action": "(system feedback)",
                            "status": "BUDGET", "observed": note})
            self._persist_actor(task, history, state)
            return None
        return await self._actor_blocked(task, note)

    async def _actor_blocked(self, task: Task, reason: str) -> RunResult:
        task.status = TaskStatus.BLOCKED
        task.result_message = (f"BLOCKED (actor loop): {reason}. I stopped instead of guessing — "
                               f"tell me how to continue and I will resume this task.")
        self.store.save_task(task)
        self.bus.emit(TASK_WAITING_USER, task_id=task.id, blocker="actor_stuck",
                      reason=reason, ask=task.result_message)
        self._post_task(task)
        return RunResult(task, TaskStatus.BLOCKED, task.result_message)

    def _actor_budget_stop(self, task: Task, deadline: float) -> str:
        c = task.counters
        if c.actions_run >= self.max_steps:
            return f"action budget reached ({self.max_steps} actions)"
        max_calls = int(self._setting("actor_max_calls", 24))
        if c.actor_calls >= max_calls:
            return f"decision budget reached ({max_calls} decisions)"
        tokens = c.tokens_in + c.tokens_out
        token_budget = int(self._setting("actor_token_budget", 60000))
        if tokens >= token_budget:
            return f"token budget reached ({tokens}/{token_budget})"
        if time.time() >= deadline:
            return ("wall-clock budget reached "
                    f"({int(self._setting('actor_wall_clock_s', 900))}s)")
        return ""

    def _actor_budget(self, task: Task, deadline: float) -> dict[str, Any]:
        c = task.counters
        return {"actions_left": max(0, self.max_steps - c.actions_run),
                "decisions_left": max(0, int(self._setting("actor_max_calls", 24))
                                      - c.actor_calls),
                "tokens_left": max(0, int(self._setting("actor_token_budget", 60000))
                                   - (c.tokens_in + c.tokens_out)),
                "seconds_left": int(max(0.0, deadline - time.time()))}

    def _actor_history(self, task: Task) -> list[dict[str, Any]]:
        history = [dict(h) for h in (task.context.get("actor_history") or [])]
        if not history and task.plan:
            for s in task.plan:            # escalation: show the actor what was already tried
                history.append({"step": s.n, "tool": s.tool,
                                "action": f"{s.tool}({json.dumps(s.args, default=str)[:120]})",
                                "status": s.status.value,
                                "observed": (s.note or s.effect_describe())[:240],
                                "reason": s.intent[:200]})
        return history

    def _persist_actor(self, task: Task, history: list[dict[str, Any]],
                       state: dict[str, Any]) -> None:
        task.context["actor_history"] = history[-40:]
        task.context["actor_repairs_used"] = state.get("repairs", 0)
        task.context["actor_last_key"] = state.get("last_key", "")
        task.context["actor_repeats"] = state.get("repeats", 0)
        task.context["actor_finish_claims"] = state.get("finish_claims", 0)
        self.store.save_checkpoint(task)

    def _actor_state_slice(self) -> list[dict[str, Any]]:
        explain = getattr(self.perception, "explain", None)
        if not callable(explain):
            return []
        try:
            return list(explain())[:40]
        except Exception:
            return []

    def _actor_memories(self, task: Task) -> dict[str, Any]:
        rules: list[dict[str, Any]] = []
        find = getattr(self.memory, "applicable_rules", None) if self.memory else None
        if callable(find):
            try:
                for rule in find(task.user_request):
                    rules.append({"id": rule.id, "instruction": rule.instruction[:200],
                                  "when": rule.when_text, "scope": rule.scope,
                                  "prefer": list(rule.prefer_tools),
                                  "forbid": list(rule.forbid_tools)})
            except Exception:
                rules = []
        return {"rules": rules}

    def _rule_veto(self, task: Task, proposal: Any) -> Any | None:
        """A remembered rule is not advice: forbidden tools are rejected pre-execution."""
        if getattr(proposal, "kind", "") != "tool" or not proposal.tool:
            return None
        find = getattr(self.memory, "applicable_rules", None) if self.memory else None
        if not callable(find):
            return None
        try:
            rules = find(task.user_request)
        except Exception:
            return None
        for rule in rules:
            if proposal.tool in (rule.forbid_tools or []):
                return rule
        return None

    def _note_rule_use(self, task: Task, tool: str) -> None:
        find = getattr(self.memory, "applicable_rules", None) if self.memory else None
        record = getattr(self.memory, "record_rule_use", None) if self.memory else None
        if not (callable(find) and callable(record)):
            return
        try:
            for rule in find(task.user_request):
                if tool and tool in (rule.prefer_tools or []):
                    record(rule.id)
        except Exception:
            pass

    @staticmethod
    def _actor_entry(task: Task, step: Step, outcome: TaskStatus | None) -> dict[str, Any]:
        observed = step.note
        if not observed:
            for failure in reversed(task.failures):
                if failure.step == step.n:
                    observed = f"{failure.kind}: {failure.detail or failure.observed}"
                    break
        if not observed and step.evidence:
            observed = "; ".join(f"{e.level}:{e.path}={str(e.value)[:60]}" for e in step.evidence[-2:])
        return {"step": step.n, "tool": step.tool,
                "action": f"{step.tool}({json.dumps(step.args, default=str)[:140]})",
                "status": step.status.value if outcome is None else outcome.value,
                "observed": str(observed or step.effect_describe())[:300],
                "reason": step.intent[:200]}

    def _should_escalate(self, task: Task) -> bool:
        """A frozen fast-lane plan that failed escalates once to the actor loop."""
        if self.actor is None or not getattr(self.settings, "actor_escalate", True):
            return False
        if task.mode != "plan" or task.counters.escalations > 0:
            return False
        if not (task.planned_by.startswith("recipe") or task.planned_by.startswith("skill")):
            return False
        if any(s.status is StepStatus.BLOCKED for s in task.plan):
            return False           # user-fixable: keep the precise ask, don't spin
        return any(s.status is StepStatus.FAILED for s in task.plan) or bool(task.failures)

    async def _execute(self, task: Task, step: Step, contract) -> ToolResult:
        ctx = ToolContext(bus=self.bus, perception=self.perception, store=self.store,
                          settings=self.settings, task=task)
        timeout = contract.verification.timeout_s + 10.0
        try:
            if contract.ui_lock:
                t0 = time.time()
                async with self.ui_lock:
                    task.counters.ui_lock_waits += time.time() - t0
                    result = await asyncio.wait_for(
                        self.registry.require(step.tool).handler(step.args, ctx), timeout=timeout)
            else:
                result = await asyncio.wait_for(
                    self.registry.require(step.tool).handler(step.args, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            result = ToolResult(ok=False, note=f"tool timed out after {timeout:.0f}s (FAILED(timeout))")
        except Exception as exc:  # a tool can never crash the loop
            result = ToolResult(ok=False, note=f"tool raised {type(exc).__name__}: {exc}")
        task.counters.actions_run += 1
        step.ended = time.time()
        return result

    def _keep_outputs(self, task: Task, step: Step, result: ToolResult) -> None:
        """Keep the last few tool outputs on the task so reports can surface the
        answer (e.g. CPU usage) without an extra model call."""
        if not result.outputs:
            return
        small: dict[str, Any] = {}
        for k, v in result.outputs.items():
            text = str(v)
            small[k] = v if len(text) <= 4000 else (text[:4000] + "…")
        bucket = task.context.setdefault("tool_outputs", [])
        bucket.append({"step": step.n, "tool": step.tool, "outputs": small})
        task.context["tool_outputs"] = bucket[-6:]

    def _invalidate_for(self, step: Step) -> None:
        invalidate = getattr(self.perception, "invalidate_for", None)
        if callable(invalidate):
            try:
                invalidate(step.expected_effect)
            except Exception:
                pass

    def _mark_verified(self, task: Task, step: Step) -> None:
        task.bump_verified(step)
        self.store.save_checkpoint(task)
        self.bus.emit(TASK_STEP_RESULT, task_id=task.id, step=step.n, status="VERIFIED",
                      effect=step.effect_describe(),
                      evidence=[f"{e.level}:{e.path}" for e in step.evidence[-3:]])

    def _skipped(self, task: Task, step: Step, obs: dict[str, Observation]) -> None:
        step.status = StepStatus.SKIPPED_ALREADY_DONE
        step.ended = time.time()
        step.evidence.extend(Evidence.from_observation(o) for o in obs.values())
        task.bump_verified(step)
        self.store.save_checkpoint(task)
        self.bus.emit(TASK_STEP_SKIPPED, task_id=task.id, step=step.n,
                      reason="state already satisfies the expected effect (P3)")
        self.bus.emit(TASK_STEP_RESULT, task_id=task.id, step=step.n, status="SKIPPED_ALREADY_DONE",
                      effect=step.effect_describe())
        return None

    async def _blocked(self, task: Task, step: Step, obs: dict[str, Observation], *,
                       reason: str, login_related: bool, unknown: bool) -> TaskStatus:
        step.status = StepStatus.BLOCKED
        step.ended = time.time()
        task.bump_non_verified()
        blocker = self.recovery.classify_blocker(precondition_failed=not unknown,
                                                 login_related=login_related,
                                                 perception_gap=unknown)
        ask = self.recovery.ask_for(blocker)
        task.record_failure(FailureRecord(step=step.n, kind="blocked", detail=reason, observed=reason))
        task.status = TaskStatus.BLOCKED
        task.result_message = f"BLOCKED at step {step.n} ({step.intent}): {reason}. {ask}"
        self.store.save_task(task)
        self.bus.emit(TASK_STEP_RESULT, task_id=task.id, step=step.n, status="BLOCKED", reason=reason)
        self.bus.emit(TASK_WAITING_USER, task_id=task.id, step=step.n, blocker=blocker.value, ask=ask,
                      reason=reason)
        return TaskStatus.BLOCKED

    async def _recover_or_fail(self, task: Task, step: Step, reason: str) -> TaskStatus | None:
        task.status = TaskStatus.RECOVERING
        contract_ = self.registry.get(step.tool)
        alternatives = list(contract_.contract.alternatives) if contract_ else []
        tried = set(task.context.get("tried_alternatives", []))
        alternatives = [a for a in alternatives if a not in tried and self.registry.get(a)]
        action = self.recovery.on_failed(task, step, alternatives_available=bool(alternatives),
                                         tool_error=False)

        if self.recovery.zero_progress_triggered(task) and self.recovery.can_repair(task):
            action = RecoveryAction.REPAIR_PLAN

        if action is RecoveryAction.RETRY:
            step.status = StepStatus.PENDING
            task.status = TaskStatus.EXECUTING
            self.bus.emit(TASK_REPLAN, task_id=task.id, step=step.n,
                          reason=f"retry attempt {step.attempts + 1}/{step.max_attempts}")
            return None

        if action is RecoveryAction.ALTERNATIVE:
            alt = alternatives[0]
            task.context.setdefault("tried_alternatives", []).append(alt)
            old_tool = step.tool
            step.tool = alt
            step.attempts = 0
            step.status = StepStatus.PENDING
            step.evidence = []
            task.status = TaskStatus.EXECUTING
            self.bus.emit(TASK_REPLAN, task_id=task.id, step=step.n,
                          reason=f"alternative action {alt} after {old_tool} failed")
            return None

        if action is RecoveryAction.REPAIR_PLAN:
            if await self._try_repair(task, reason=f"step {step.n}: {reason}"):
                return None
        if self.recovery.zero_progress_triggered(task) and not self.recovery.can_repair(task):
            return await self._task_blocked(
                task, f"zero progress after {task.counters.repairs} repairs: {reason}")
        return await self._task_failed(task, reason)

    async def _task_blocked(self, task: Task, reason: str) -> TaskStatus:
        ask = self.recovery.ask_for(BlockerClass.UNKNOWN)
        task.status = TaskStatus.BLOCKED
        task.result_message = f"BLOCKED: {reason}. {ask}"
        self.store.save_task(task)
        self.bus.emit(TASK_WAITING_USER, task_id=task.id, blocker="zero_progress", ask=ask, reason=reason)
        self.bus.emit(TASK_FAILED, task_id=task.id, status="blocked", reason=reason,
                      failures=len(task.failures))
        return TaskStatus.BLOCKED

    async def _try_repair(self, task: Task, *, reason: str) -> bool:
        planner = self.planner
        repair = getattr(planner, "repair", None) if planner else None
        if not callable(repair) or not self.recovery.can_repair(task):
            return False
        try:
            tail = await repair(task, reason=reason)
        except Exception:
            tail = None
        if not tail:
            return False
        for s in tail:
            if self.registry.get(s.tool) is None:
                return False  # repaired plans may only use registered tools
        task.counters.repairs += 1
        task.counters.replans += 1
        done_n = [s.n for s in task.plan if s.status.done]
        keep = [s for s in task.plan if s.status.done]
        start = (max(done_n) if done_n else 0)
        renamed: list[Step] = []
        for i, s in enumerate(tail, start=start + 1):
            s.n = i
            s.status = StepStatus.PENDING
            renamed.append(s)
        task.plan = keep + renamed
        task.plan = [s for s in task.plan if s.status.done or s.status in (StepStatus.PENDING,)]
        task.status = TaskStatus.EXECUTING
        self.store.save_task(task)
        self.bus.emit(TASK_REPLAN, task_id=task.id, reason=reason,
                      repair=task.counters.repairs, steps=[s.n for s in renamed])
        return True

    async def _task_failed(self, task: Task, reason: str) -> TaskStatus:
        task.status = TaskStatus.FAILED
        task.result_message = f"FAILED: {reason}"
        self.store.save_task(task)
        self.bus.emit(TASK_FAILED, task_id=task.id, reason=reason, failures=len(task.failures))
        return TaskStatus.FAILED

    # ------------------------------------------------------------- terminal states
    def _finish_noop(self, task: Task) -> RunResult:
        task.status = TaskStatus.FINISHED
        task.result_message = ("Already done — the completion condition was verified against "
                               "current state; nothing to do.")
        self.store.save_task(task)
        self.bus.emit(TASK_FINISHED, task_id=task.id, status="success", noop=True,
                      message=task.result_message)
        self._post_task(task)
        return RunResult(task, TaskStatus.FINISHED, task.result_message)

    async def _finish_failed(self, task: Task, reason: str) -> RunResult:
        await self._task_failed(task, reason)
        self._post_task(task)
        return RunResult(task, TaskStatus.FAILED, task.result_message)

    async def _finish_cancelled(self, task: Task, reason: str) -> RunResult:
        task.status = TaskStatus.CANCELLED
        task.result_message = f"CANCELLED: {reason}"
        self.store.save_task(task)
        self.bus.emit(TASK_CANCELLED, task_id=task.id, reason=reason)
        self._post_task(task)
        return RunResult(task, TaskStatus.CANCELLED, task.result_message)

    async def _final_verdict(self, task: Task) -> RunResult:
        obs = await self.perception.verify(task.completion.predicates(), timeout_s=self.completion_timeout_s)
        truth = task.completion.evaluate(obs)

        if truth is Truth.TRUE:
            task.status = TaskStatus.FINISHED
            task.result_message = self._success_message(task, obs)
            self.store.save_task(task)
            self.bus.emit(TASK_FINISHED, task_id=task.id, status="success",
                          message=task.result_message,
                          evidence=[f"{o.level}:{o.path}={o.value}" for o in list(obs.values())[:6]])
            self._post_task(task)
            return RunResult(task, TaskStatus.FINISHED, task.result_message, obs)

        blocked_steps = [s for s in task.plan if s.status is StepStatus.BLOCKED]
        if truth is Truth.UNKNOWN and not blocked_steps:
            detail = "; ".join(task.completion.failing(obs))
            msg = (f"BLOCKED (verification gap): completion condition could not be verified — {detail}. "
                   f"I will not claim success on UNKNOWN.")
            task.status = TaskStatus.BLOCKED
            task.result_message = msg
            self.store.save_task(task)
            self.bus.emit(TASK_WAITING_USER, task_id=task.id, blocker="perception_gap", ask=msg)
            self._post_task(task)
            return RunResult(task, TaskStatus.BLOCKED, msg, obs)

        if blocked_steps:
            step = blocked_steps[0]
            if not task.result_message:
                task.result_message = (f"BLOCKED at step {step.n} ({step.intent}) — "
                                       f"completion condition not satisfied.")
            task.status = TaskStatus.BLOCKED
            self.store.save_task(task)
            self.bus.emit(TASK_FAILED, task_id=task.id, status="blocked", message=task.result_message)
            self._post_task(task)
            return RunResult(task, TaskStatus.BLOCKED, task.result_message, obs)

        detail = "; ".join(task.completion.failing(obs)) or task.completion.describe()
        msg = (f"FAILED: goal not reached. Steps executed: {task.counters.actions_run}. "
               f"Unmet completion condition: {detail}")
        task.status = TaskStatus.FAILED
        task.result_message = msg
        self.store.save_task(task)
        self.bus.emit(TASK_FAILED, task_id=task.id, reason=detail, failures=len(task.failures))
        self._post_task(task)
        return RunResult(task, TaskStatus.FAILED, msg, obs)

    def _success_message(self, task: Task, obs: dict[str, Observation]) -> str:
        verified = sum(1 for s in task.plan if s.status is StepStatus.VERIFIED)
        skipped = sum(1 for s in task.plan if s.status is StepStatus.SKIPPED_ALREADY_DONE)
        evidence = "; ".join(f"{o.path}={o.value!r} ({o.level})" for o in list(obs.values())[:4])
        return (f"FINISHED — completion condition verified. "
                f"steps: {verified} verified, {skipped} skipped, {task.counters.actions_run} actions, "
                f"{len(task.failures)} failures. Evidence: {evidence}")

    def _post_task(self, task: Task) -> None:
        """Memory hooks: episode write on success, distiller, working-memory wipe."""
        try:
            if task.status is TaskStatus.FINISHED and self.memory is not None:
                self.memory.record_episode(task)
                self.bus.emit("EPISODE_WRITTEN", task_id=task.id)
                if self.distiller is not None:
                    skill = self.distiller.consider(task)
                    if skill:
                        self.bus.emit("SKILL_DISTILLED", task_id=task.id, skill=skill)
        except Exception:
            pass
        try:
            recorder = getattr(self.memory, "record_skill_run", None)
            if task.origin_skill and callable(recorder):
                recorder(task.origin_skill, task.status.value, task.id)
        except Exception:
            pass
        try:
            if self.memory is not None:
                self.memory.clear_working(task.id)
        except Exception:
            pass
        self.current = None

    async def _ask(self, decision: RiskDecision, step: Step, task: Task) -> bool:
        if self.confirm is None:
            return False  # fail-safe: without a confirm surface, HIGH/CRITICAL never proceed
        try:
            suite = self.confirm(decision, step, task)
            if asyncio.iscoroutine(suite):
                return bool(await suite)
            return bool(suite)
        except Exception:
            return False

    def _final_message_for(self, task: Task) -> str:  # pragma: no cover - convenience
        return task.result_message or task.status.value


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    for key in ("text", "message", "password", "token"):
        if key in out and isinstance(out[key], str) and len(out[key]) > 40:
            out[key] = out[key][:40] + "…"
    return out
