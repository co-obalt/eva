"""Model router (doc 07 §1/§4): tier selection, provider failover, response cache,
per-task ledger memo (same question asked twice = one call — S7d).

The router holds NO business logic: it answers prompts, nothing else.
"""
from __future__ import annotations

import hashlib
import json
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field

from .providers import ChatResult, ProviderPool, ProviderUnavailable, image_to_data_url


class Tier(IntEnum):
    T0 = 0   # deterministic — no LLM at all
    T1 = 1   # small/cheap classification & extraction
    T2 = 2   # strong planning / generation
    T3 = 3   # vision (only via VisionGateway, INV-1)


class NoModelNeeded(RuntimeError):
    """Tier 0 path — the router must not be called."""


class ModelResponse(BaseModel):
    text: str
    provider: str = ""
    model: str = ""
    tier: int = 2
    usage: dict[str, int] = Field(default_factory=dict)
    cached: bool = False
    latency_s: float = 0.0


class ModelRouter:
    def __init__(self, pool: ProviderPool, store=None, bus=None, settings=None):
        self.pool = pool
        self.store = store
        self.bus = bus
        self.settings = settings
        self.task: Any = None
        self.ledger: dict[str, ModelResponse] = {}     # per-task memo
        self.failures = 0

    # ------------------------------------------------------------------ task
    def bind_task(self, task: Any) -> None:
        self.task = task
        self.ledger = {}

    def unbind(self) -> None:
        self.task = None
        self.ledger = {}

    # ------------------------------------------------------------------ call
    async def call(self, tier: Tier | int, purpose: str, *,
                   system: str = "", user: str = "",
                   messages: list[dict] | None = None,
                   json_mode: bool = False, temperature: float = 0.2,
                   max_tokens: int = 800, cache_ttl_s: float = 6 * 3600.0,
                   task: Any = None) -> ModelResponse:
        tier = Tier(int(tier))
        if tier is Tier.T0:
            raise NoModelNeeded(f"{purpose}: Tier 0 is deterministic — no model call allowed")
        if messages is None:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": user})

        memo_key = self._hash(purpose, messages, json_mode)
        task_ = task or self.task

        # 1) task-ledger memo (doc 07 §4: same question never re-asked in one task)
        if task_ and memo_key in self.ledger:
            resp = self.ledger[memo_key].model_copy()
            resp.cached = True
            if task_ is not None and hasattr(task_, "counters"):
                task_.counters.cache_hits += 1
            if self.bus is not None:
                self.bus.emit("MODEL_ROUTED", task_id=getattr(task_, "id", None),
                              tier=int(tier), provider=resp.provider, cached=True, purpose=purpose)
            return resp

        # 2) response cache (identical model+prompt already answered)
        if self.store is not None:
            cached = self.store.cache_get(f"llm:{memo_key}")
            if cached:
                resp = ModelResponse(**cached, cached=True)
                if task_:
                    self.ledger[memo_key] = resp
                    task_.counters.cache_hits += 1
                if self.bus is not None:
                    self.bus.emit("MODEL_ROUTED", task_id=getattr(task_, "id", None),
                                  tier=int(tier), provider=resp.provider, cached=True,
                                  purpose=purpose)
                return resp

        # 3) call the pool with failover (circuit breakers inside)
        try:
            result: ChatResult = await self.pool.chat(
                messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)
        except ProviderUnavailable as exc:
            self.failures += 1
            if self.bus is not None:
                self.bus.emit("MODEL_ROUTED", task_id=getattr(task_, "id", None),
                              tier=int(tier), provider="none", error=str(exc)[:200],
                              purpose=purpose)
            raise
        resp = ModelResponse(text=result.text, provider=result.provider, model=result.model,
                             tier=int(tier), usage=result.usage, latency_s=result.latency_s)
        if task_ is not None and hasattr(task_, "counters"):
            task_.counters.model_calls += 1
            task_.counters.tokens_in += resp.usage.get("in", 0)
            task_.counters.tokens_out += resp.usage.get("out", 0)
        if task_:
            self.ledger[memo_key] = resp
        if self.store is not None and cache_ttl_s > 0:
            self.store.cache_set(f"llm:{memo_key}", resp.model_dump(), cache_ttl_s)
        if self.bus is not None:
            self.bus.emit("MODEL_ROUTED", task_id=getattr(task_, "id", None), tier=int(tier),
                          provider=resp.provider, purpose=purpose,
                          tokens=resp.usage, latency_s=resp.latency_s)
        return resp

    async def call_vision(self, question: str, image_path: str) -> str | None:
        """Tier 3 — only reachable through VisionGateway after policy authorization."""
        if not self.settings or not self.settings.vision_model:
            return None
        data_url = image_to_data_url(image_path)
        if data_url is None:
            return None
        messages = [{"role": "system", "content":
                     "You answer questions about what is visible on a computer screen. "
                     "Be literal and concise; say UNKNOWN when you cannot tell."},
                    {"role": "user", "content": question}]
        result = await self.pool.chat(messages, max_tokens=400, image_data_url=data_url)
        return result.text.strip() or None

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _hash(purpose: str, messages: list[dict], json_mode: bool) -> str:
        payload = json.dumps({"purpose": purpose, "messages": messages, "json": json_mode},
                             sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def status(self) -> dict:
        return {"providers": self.pool.status(), "failures": self.failures,
                "ledger": len(self.ledger)}
