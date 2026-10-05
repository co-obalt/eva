"""Tool contracts (doc 05 §1). An action without preconditions, expected
effects and a verification method does not exist. Registration validates
this; execution refuses anything unregistered.

Doc 13 rule 3 / doc 04 INV-3: a contract whose verification names L5/L6/L7
(screenshot/vision/OCR) is REJECTED at registration — verification must be
structural state, API, filesystem or event based.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .state import Observation, Predicate
from .task import RiskLevel, Task

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_FORBIDDEN_VERIFY = ("l5", "l6", "l7", "ocr", "screenshot", "vision", "pixel", "image")


class IdempotencyClass(str, Enum):
    NATURALLY_IDEMPOTENT = "naturally_idempotent"
    CHECK_BEFORE_CREATE = "check_before_create"
    EFFECT_TOKEN = "effect_token"
    DESTRUCTIVE = "destructive"


class VerificationSpec(BaseModel):
    method: str                                     # e.g. "L1_process", "L3_cdp_url", "fs_stat"
    timeout_s: float = 8.0

    @model_validator(mode="after")
    def _structural_only(self) -> "VerificationSpec":
        low = self.method.lower()
        if any(bad in low for bad in _FORBIDDEN_VERIFY):
            raise ValueError(
                f"verification method {self.method!r} violates doc 13 rule 3 "
                f"(no screenshot/vision/OCR as verification)")
        return self


class ParamSpec(BaseModel):
    type: str = "string"                            # string|int|float|bool
    required: bool = True
    doc: str = ""


class RiskContextFn:
    """Marker alias for readability."""
    pass


class ToolContract(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    category: str                                    # os|browser|files|terminal|apps|documents|comms|system
    description: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)

    preconditions: list[Predicate] = Field(default_factory=list)
    effects: list[Predicate] = Field(default_factory=list)
    preconditions_for: Callable[[dict[str, Any]], list[Predicate]] | None = Field(default=None, exclude=True)
    effects_for: Callable[[dict[str, Any]], list[Predicate]] | None = Field(default=None, exclude=True)

    verification: VerificationSpec
    idempotency: IdempotencyClass
    risk: RiskLevel
    risk_context: Callable[[dict[str, Any]], RiskLevel] | None = Field(default=None, exclude=True)

    ui_lock: bool = False                           # drives mouse/keyboard/focus (doc 03 §4.1)
    structural_target: bool = False                 # INV-2: coordinate target needs justification
    uiaction: bool = False                          # UI-affecting (even without ui_lock)
    cost_hint: dict[str, Any] = Field(default_factory=lambda: {"latency_ms": 50, "tokens": 0})
    parallel_safe: bool = False
    alternatives: list[str] = Field(default_factory=list)

    def resolve_preconditions(self, args: dict[str, Any]) -> list[Predicate]:
        out = list(self.preconditions)
        if self.preconditions_for:
            out.extend(self.preconditions_for(args))
        return out

    def resolve_effects(self, args: dict[str, Any]) -> list[Predicate]:
        out = list(self.effects)
        if self.effects_for:
            out.extend(self.effects_for(args))
        return out


class ToolResult(BaseModel):
    ok: bool
    outputs: dict[str, Any] = Field(default_factory=dict)
    observed: list[Observation] = Field(default_factory=list)   # structural evidence produced by the tool
    note: str = ""
    mutated: bool = False
    blocked: bool = False      # user-fixable blocker -> task BLOCKED with a precise ask
    ask: str = ""              # the exact instruction the user must act on


Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[ToolResult]]


class ToolContext(BaseModel):
    """Everything a tool handler may touch. Tools never call the LLM (doc 07 §5)."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    bus: Any = None
    perception: Any = None
    store: Any = None
    settings: Any = None
    task: Task | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    log: Callable[[str], None] | None = Field(default=None, exclude=True)

    def emit(self, type_: str, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.emit(type_, task_id=self.task.id if self.task else None, **payload)


class ContractError(ValueError):
    pass


class RegisteredTool(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    contract: ToolContract
    handler: Handler = Field(exclude=True)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    # -- registration --------------------------------------------------------
    def register(self, contract: ToolContract, handler: Handler) -> None:
        self._validate(contract, handler)
        if contract.name in self._tools:
            raise ContractError(f"duplicate tool name {contract.name!r}")
        self._tools[contract.name] = RegisteredTool(contract=contract, handler=handler)

    def _validate(self, c: ToolContract, handler: Handler) -> None:
        if not _NAME_RE.match(c.name):
            raise ContractError(f"invalid tool name {c.name!r} (use dotted lower_snake)")
        if not callable(handler):
            raise ContractError(f"{c.name}: handler is not callable")
        if not (c.effects or c.effects_for):
            raise ContractError(f"{c.name}: contract must declare effects or effects_for")
        if not c.verification.timeout_s > 0:
            raise ContractError(f"{c.name}: verification timeout must be positive")
        if not isinstance(c.idempotency, IdempotencyClass):
            raise ContractError(f"{c.name}: invalid idempotency class")
        if not isinstance(c.risk, RiskLevel):
            raise ContractError(f"{c.name}: invalid risk level")
        for spec in c.params.values():
            if spec.type not in ("string", "int", "float", "bool"):
                raise ContractError(f"{c.name}: param type {spec.type!r} unsupported")

    # -- lookup ---------------------------------------------------------------
    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def require(self, name: str) -> RegisteredTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ContractError(f"tool {name!r} is not registered (planner may only emit registered tools)")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def for_domain(self, category: str) -> list[RegisteredTool]:
        return [t for t in self._tools.values() if t.contract.category == category]

    def validate_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Strict coercion: unknown args are rejected (doc 05: typed schema)."""
        c = self.require(name).contract
        clean: dict[str, Any] = {}
        for key, spec in c.params.items():
            if key in args:
                clean[key] = _coerce(spec.type, args[key], f"{name}.{key}")
            elif spec.required and key not in args:
                raise ContractError(f"{name}: missing required param {key!r}")
        unknown = set(args) - set(c.params)
        if unknown:
            raise ContractError(f"{name}: unknown params {sorted(unknown)}")
        return clean


def _coerce(type_: str, value: Any, where: str) -> Any:
    try:
        if type_ == "string":
            return value if isinstance(value, str) else str(value)
        if type_ == "int":
            return int(value)
        if type_ == "float":
            return float(value)
        if type_ == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError):
        raise ContractError(f"{where}: cannot coerce {value!r} to {type_}")
    return value
