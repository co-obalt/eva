"""Providers (doc 07 §1 rule 5): Groq (primary) -> APINEX (secondary, RPM-aware)
-> local OpenAI-compatible (Ollama-style). Failover with circuit breakers.

All are plain OpenAI-compatible /chat/completions endpoints (see .env.example).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from pydantic import BaseModel


class ProviderError(RuntimeError):
    pass


class ProviderUnavailable(ProviderError):
    """Every provider in the pool failed — callers must degrade honestly."""


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_s: float = 0.0


class ProviderSpec(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    model: str = ""
    rpm_limit: int = 0            # 0 = unlimited (local)
    timeout_s: float = 60.0


class _Provider:
    def __init__(self, spec: ProviderSpec):
        self.spec = spec
        self._calls: list[float] = []
        self.failures = 0
        self.dead_until = 0.0

    @property
    def alive(self) -> bool:
        return time.time() >= self.dead_until

    @property
    def usable(self) -> bool:
        """A provider without a key (and not local) can never serve calls."""
        if self.spec.api_key:
            return True
        base = self.spec.base_url or ""
        return "127.0.0.1" in base or "localhost" in base

    def note_failure(self) -> None:
        self.failures += 1
        if self.failures >= 3:
            self.dead_until = time.time() + 30.0
            self.failures = 0

    def note_success(self) -> None:
        self.failures = 0
        self.dead_until = 0.0

    async def _respect_rpm(self) -> None:
        if self.spec.rpm_limit <= 0:
            return
        window = 60.0
        now = time.time()
        self._calls = [t for t in self._calls if now - t < window]
        if len(self._calls) >= self.spec.rpm_limit:
            wait = window - (now - self._calls[0]) + 0.5
            if wait > 0:
                await asyncio.sleep(min(wait, 65.0))
            self._calls = [t for t in self._calls if time.time() - t < window]
        self._calls.append(time.time())

    async def chat(self, messages: list[dict], *, temperature: float = 0.2,
                   max_tokens: int = 800, json_mode: bool = False,
                   image_data_url: str | None = None) -> ChatResult:
        if aiohttp is None:
            raise ProviderError("aiohttp missing")
        if not self.spec.api_key and "127.0.0.1" not in self.spec.base_url and "localhost" not in self.spec.base_url:
            raise ProviderError(f"{self.spec.name}: no API key configured")
        await self._respect_rpm()

        if image_data_url is not None:
            content = [{"type": "text", "text": messages[-1]["content"]},
                       {"type": "image_url", "image_url": {"url": image_data_url}}]
            messages = messages[:-1] + [{"role": "user", "content": content}]

        payload: dict[str, Any] = {
            "model": self.spec.model or "default",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"
        url = self.spec.base_url.rstrip("/") + "/chat/completions"
        t0 = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=self.spec.timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 400 and json_mode:
                        # provider without response_format support: retry once without it
                        payload.pop("response_format", None)
                        async with session.post(url, json=payload, headers=headers) as resp2:
                            body = await resp2.text()
                            if resp2.status >= 400:
                                raise ProviderError(f"{self.spec.name} HTTP {resp2.status}: {body[:200]}")
                    elif resp.status >= 400:
                        raise ProviderError(f"{self.spec.name} HTTP {resp.status}: {body[:200]}")
                    data = json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{self.spec.name}: {type(exc).__name__}: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.spec.name}: malformed response") from exc
        usage = data.get("usage") or {}
        return ChatResult(text=text, provider=self.spec.name,
                          model=data.get("model") or self.spec.model,
                          usage={"in": int(usage.get("prompt_tokens") or 0),
                                 "out": int(usage.get("completion_tokens") or 0)},
                          latency_s=round(time.time() - t0, 2))


class ProviderPool:
    def __init__(self, specs: list[ProviderSpec]):
        self.providers = [_Provider(s) for s in specs if s.base_url]
        if not self.providers:
            raise ProviderUnavailable("no providers configured (set GROQ_API_KEY / APINEX_API_KEY / local model)")

    def available(self) -> bool:
        return any(p.alive and p.usable for p in self.providers)

    async def chat(self, messages: list[dict], *, temperature: float = 0.2,
                   max_tokens: int = 800, json_mode: bool = False,
                   image_data_url: str | None = None) -> ChatResult:
        errors: list[str] = []
        for p in self.providers:
            if not p.usable:
                errors.append(f"{p.spec.name}: no API key configured (set it in .env)")
                continue
            if not p.alive:
                errors.append(f"{p.spec.name}: circuit open")
                continue
            try:
                result = await p.chat(messages, temperature=temperature, max_tokens=max_tokens,
                                      json_mode=json_mode, image_data_url=image_data_url)
                p.note_success()
                return result
            except ProviderError as exc:
                p.note_failure()
                errors.append(str(exc))
                continue
        raise ProviderUnavailable("; ".join(errors) or "no providers available")

    def status(self) -> list[dict]:
        return [{"name": p.spec.name, "model": p.spec.model, "alive": p.alive,
                 "usable": p.usable, "failures": p.failures,
                 "rpm": p.spec.rpm_limit} for p in self.providers]


def _key(name: str) -> str:
    import os
    return os.environ.get(name, "")


def build_default_pool(settings) -> ProviderPool:
    specs = [
        ProviderSpec(name="Groq", base_url=settings.groq_base, api_key=settings.groq_key or _key("GROQ_API_KEY"),
                     model=settings.groq_model, timeout_s=settings.provider_timeout_s),
        ProviderSpec(name="APINEX", base_url=settings.apinex_base,
                     api_key=settings.apinex_key or _key("APINEX_API_KEY"),
                     model=settings.groq_model, rpm_limit=settings.apinex_rpm or 5,
                     timeout_s=settings.provider_timeout_s),
        ProviderSpec(name="local", base_url=settings.local_base, api_key="",
                     model=settings.local_model, timeout_s=120.0),
    ]
    return ProviderPool(specs)


def image_to_data_url(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{data}"
    except OSError:
        return None
