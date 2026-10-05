"""State, predicates, observations (doc 04).

Three-valued logic is law: TRUE / FALSE / UNKNOWN. UNKNOWN is never treated
as TRUE — it triggers perception at the needed ladder level or an honest
BLOCKED (doc 04 §1 rule 3, doc 03 §5).
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
from enum import Enum
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field, model_validator


class Truth(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class ObservationRequest(BaseModel):
    """A question asked of perception: path (+ optional args for parameterized providers)."""
    path: str
    args: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return request_key(self.path, self.args)


def request_key(path: str, args: Mapping[str, Any] | None = None) -> str:
    a = args or {}
    if not a:
        return path
    return f"{path}|{json.dumps({k: a[k] for k in sorted(a)}, sort_keys=True, default=str)}"


class Observation(BaseModel):
    path: str
    args: dict[str, Any] = Field(default_factory=dict)
    truth: Truth = Truth.UNKNOWN
    value: Any = None
    level: str = "L0"          # ladder level that produced it (doc 04)
    at: float = Field(default_factory=time.time)
    confidence: float = 0.9
    ttl_s: float = 2.0
    raw: dict[str, Any] = Field(default_factory=dict)
    note: str = ""

    @property
    def key(self) -> str:
        return request_key(self.path, self.args)

    def fresh(self, max_age_s: float, now: float | None = None) -> bool:
        return ((now or time.time()) - self.at) <= min(max_age_s, self.ttl_s)


# -- operators ---------------------------------------------------------------
_OPS = {"==", "!=", ">", ">=", "<", "<=", "contains", "matches", "in",
        "not_in", "exists", "not_exists", "truthy", "falsy"}


class Predicate(BaseModel):
    """A single checkable statement about the world (doc 03 §1 completion conditions).

    op semantics (three-valued; type mismatch -> UNKNOWN, never a crash):
      eq/ne, gt/gte/lt/lte, contains (str/list, case-insensitive for strings),
      matches (glob, case-insensitive), in/not_in (value in list),
      exists (observation present, value is not None; False is a real value),
      truthy/falsy (Python truthiness of observed value).
    """
    path: str
    op: str = "=="
    value: Any = None
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "Predicate":
        if self.op not in _OPS:
            raise ValueError(f"unknown predicate op {self.op!r}")
        return self

    @property
    def key(self) -> str:
        return request_key(self.path, self.args)

    def describe(self) -> str:
        if self.args:
            a = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
            return f"{self.path}({a}) {self.op} {self.value!r}"
        return f"{self.path} {self.op} {self.value!r}"

    def matches_observation(self, obs: Observation | None) -> Truth:
        if obs is None or obs.truth is Truth.UNKNOWN:
            return Truth.UNKNOWN
        v = obs.value
        op = self.op
        try:
            if op == "exists":
                return Truth.TRUE if v is not None else Truth.FALSE
            if op == "not_exists":
                return Truth.FALSE if v is not None else Truth.TRUE
            if op == "truthy":
                return Truth.TRUE if bool(v) else Truth.FALSE
            if op == "falsy":
                return Truth.TRUE if not bool(v) else Truth.FALSE
            if v is None and op in ("matches", "in", "not_in"):
                return Truth.UNKNOWN  # cannot meaningfully match a null value
            expected = self.value
            if op == "==":
                r = _eq(v, expected)
            elif op == "!=":
                return Truth.TRUE if _eq(v, expected) is False else Truth.FALSE
            elif op == ">":
                r = v > expected
            elif op == ">=":
                r = v >= expected
            elif op == "<":
                r = v < expected
            elif op == "<=":
                r = v <= expected
            elif op == "contains":
                r = _contains(v, expected)
            elif op == "matches":
                r = _matches(v, expected)
            elif op == "in":
                r = _contains(expected, v)
            elif op == "not_in":
                r = not _contains(expected, v)
            else:  # pragma: no cover - validator guards this
                return Truth.UNKNOWN
            return Truth.TRUE if r else Truth.FALSE
        except (TypeError, ValueError, re.error):
            return Truth.UNKNOWN


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    try:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    return a == b


def _contains(container: Any, needle: Any) -> bool:
    if isinstance(container, str):
        return str(needle).casefold() in container.casefold()
    if isinstance(container, (list, tuple, set)):
        for item in container:
            if _eq(item, needle):
                return True
            if isinstance(item, str) and isinstance(needle, str) and needle.casefold() in item.casefold():
                return True
        return False
    return False


def _matches(value: Any, pattern: Any) -> bool:
    if not isinstance(value, str):
        value = str(value)
    pat = str(pattern)
    return fnmatch.fnmatch(value.casefold(), pat.casefold())


class Condition(BaseModel):
    """all_of / any_of over predicates. Empty list folds: all_of()=UNKNOWN (invalid),
    any_of()=FALSE. Validation at task creation rejects empty conditions."""
    all_of: list[Predicate] = Field(default_factory=list)
    any_of: list[Predicate] = Field(default_factory=list)

    def predicates(self) -> list[Predicate]:
        return list(self.all_of) + list(self.any_of)

    def validate_nonempty(self) -> None:
        if not self.all_of and not self.any_of:
            raise ValueError("completion condition must contain at least one predicate")

    def evaluate(self, obs_by_key: Mapping[str, Observation]) -> Truth:
        if not self.all_of and not self.any_of:
            return Truth.UNKNOWN
        results_all = [p.matches_observation(obs_by_key.get(p.key)) for p in self.all_of]
        results_any = [p.matches_observation(obs_by_key.get(p.key)) for p in self.any_of]
        if Truth.FALSE in results_all:
            return Truth.FALSE
        if Truth.TRUE in results_any:
            return Truth.TRUE
        if Truth.UNKNOWN in results_all or Truth.UNKNOWN in results_any:
            return Truth.UNKNOWN
        if results_all and not results_any:
            return Truth.TRUE
        return Truth.FALSE

    def describe(self) -> str:
        parts = [p.describe() for p in self.all_of]
        if self.any_of:
            parts.append("any_of(" + "; ".join(p.describe() for p in self.any_of) + ")")
        return " AND ".join(parts) if parts else "(empty)"

    def failing(self, obs_by_key: Mapping[str, Observation]) -> list[str]:
        out = []
        for p in self.all_of:
            if p.matches_observation(obs_by_key.get(p.key)) is not Truth.TRUE:
                out.append(p.describe())
        return out


# -- string shorthand: "chrome.running == true" ------------------------------
_TOKEN_RE = re.compile(r"^(?P<path>[\w.\-]+)\s*(?P<op>==|!=|>=|<=|>|<|~=|contains|matches)\s*(?P<val>.+)$")


def parse_predicate(text: str) -> Predicate:
    m = _TOKEN_RE.match(text.strip())
    if not m:
        raise ValueError(f"cannot parse predicate: {text!r}")
    op = {"~=": "matches"}.get(m.group("op"), m.group("op"))
    op = {"==": "==", "!=": "!="}.get(op, op)
    return Predicate(path=m.group("path"), op=op, value=_parse_value(m.group("val")))


def _parse_value(raw: str) -> Any:
    v = raw.strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    try:
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        if re.fullmatch(r"-?\d*\.\d+", v):
            return float(v)
    except ValueError:
        pass
    return v


# -- world state (folded current view; observations are the evidence) --------
class BranchMeta(BaseModel):
    level: str = "L0"
    at: float = 0.0
    confidence: float = 0.0


class WorldState:
    """Live model of the world, written ONLY by perception (doc 04 §1 rule 5)."""

    def __init__(self) -> None:
        self.branches: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, BranchMeta] = {}

    def write(self, branch: str, data: Mapping[str, Any], level: str, confidence: float = 0.95) -> None:
        existing = self.branches.setdefault(branch, {})
        existing.update(dict(data))
        self.meta[branch] = BranchMeta(level=level, at=time.time(), confidence=confidence)

    def read(self, path: str) -> tuple[Truth, Any]:
        parts = path.split(".")
        node: Any = self.branches
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return Truth.UNKNOWN, None
        return Truth.TRUE, node

    def branch_of(self, path: str) -> str:
        return path.split(".", 1)[0]

    def invalidate(self, branch: str) -> None:
        self.meta.pop(branch, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "branches": self.branches,
            "meta": {k: v.model_dump() for k, v in self.meta.items()},
        }

    def diff(self, old: "WorldState", branch: str) -> dict[str, Any]:
        a = old.branches.get(branch, {})
        b = self.branches.get(branch, {})
        changed = {k: b[k] for k in b if a.get(k) != b[k]}
        return {k: v for k, v in changed.items()}


def satisfaction(condition: Condition, obs_by_key: Mapping[str, Observation]) -> Truth:
    return condition.evaluate(obs_by_key)


def all_true(predicates: Iterable[Predicate], obs_by_key: Mapping[str, Observation]) -> bool:
    return all(p.matches_observation(obs_by_key.get(p.key)) is Truth.TRUE for p in predicates)


def any_unknown(predicates: Iterable[Predicate], obs_by_key: Mapping[str, Observation]) -> bool:
    return any(p.matches_observation(obs_by_key.get(p.key)) is Truth.UNKNOWN for p in predicates)
