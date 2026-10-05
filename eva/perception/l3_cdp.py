"""L3 — application APIs: Chrome DevTools Protocol (doc 04).

EVA attaches to the user's REAL Chrome (logins preserved). If Chrome runs
without a debug port we do NOT silently launch a second browser — tools
surface BLOCKED with a precise ask; launch-with-flag happens only when Chrome
is not running at all.

Transport: aiohttp (already installed). Browser-level socket + flatten mode
(Target.attachToTarget {flatten:true}) so every eval is one round-trip.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from .l1_system import process_cmdlines, process_running

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


class CdpError(RuntimeError):
    pass


class CdpSession:
    """One browser-level CDP websocket (flatten mode)."""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._session: Any = None
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._id = 0
        self.closed = False

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(self.ws_url, max_msg_size=16 * 1024 * 1024), self.timeout)

        async def _read() -> None:
            try:
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._dispatch(json.loads(msg.data))
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            except Exception:
                pass
            finally:
                self.closed = True
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(CdpError("cdp connection closed"))
                self._pending.clear()

        self._reader = asyncio.create_task(_read())

    def _dispatch(self, data: dict[str, Any]) -> None:
        if "id" in data:
            fut = self._pending.pop(data["id"], None)
            if fut and not fut.done():
                if "error" in data:
                    fut.set_exception(CdpError(str(data["error"])))
                else:
                    fut.set_result(data.get("result", {}))

    async def send(self, method: str, params: dict | None = None,
                   session_id: str | None = None, timeout: float | None = None) -> dict:
        if self.closed or self._ws is None or getattr(self._ws, "closed", False):
            raise CdpError("cdp session is closed")
        self._id += 1
        payload: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._ws.send_json(payload)
        return await asyncio.wait_for(fut, timeout or self.timeout)

    # -- high level --------------------------------------------------------------
    async def target_infos(self) -> list[dict]:
        res = await self.send("Target.getTargets")
        return [t for t in res.get("targetInfos", []) if t.get("type") == "page"]

    async def attach(self, target_id: str) -> str:
        res = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return res["sessionId"]

    async def pick_target(self, url_contains: str | None = None) -> dict | None:
        infos = await self.target_infos()
        if url_contains:
            for t in infos:
                if url_contains in (t.get("url") or ""):
                    return t
            return None
        for t in infos:
            url = t.get("url") or ""
            if not url.startswith("chrome://") and "devtools" not in url:
                return t
        return infos[0] if infos else None

    async def eval_js(self, expression: str, url_contains: str | None = None,
                      timeout: float = 15.0) -> dict[str, Any]:
        try:
            target = await self.pick_target(url_contains)
            if target is None:
                return {"ok": False, "error": f"no tab matching {url_contains!r}"}
            sid = await self.attach(target["targetId"])
            res = await self.send("Runtime.evaluate", {
                "expression": expression, "returnByValue": True,
                "awaitPromise": True, "userGesture": True}, session_id=sid, timeout=timeout)
            if res.get("exceptionDetails"):
                return {"ok": False, "error": res["exceptionDetails"].get("text") or "js exception"}
            return {"ok": True, "value": (res.get("result") or {}).get("value")}
        except (CdpError, asyncio.TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}

    async def create_target(self, url: str) -> str | None:
        res = await self.send("Target.createTarget", {"url": url})
        return res.get("targetId")

    async def close(self) -> None:
        self.closed = True
        if self._reader:
            self._reader.cancel()
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        try:
            if self._session:
                await self._session.close()
        except Exception:
            pass


class CdpManager:
    """Lazy, resilient CDP attach across the configured ports."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.session: CdpSession | None = None
        self.port: int | None = None
        self.version: dict[str, Any] = {}
        self._dead_until = 0.0
        self._failed_attempts = 0

    # -- discovery ---------------------------------------------------------------
    async def _http_json(self, url: str, timeout: float = 1.5) -> dict | None:
        if aiohttp is None:
            return None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        return await r.json()
        except Exception:
            return None
        return None

    async def discover(self) -> int | None:
        now = time.time()
        if now < self._dead_until:
            return None
        for port in self.settings.cdp_ports:
            data = await self._http_json(f"http://{self.settings.cdp_host}:{port}/json/version")
            if data and data.get("webSocketDebuggerUrl"):
                self.port = port
                self.version = data
                return port
        self._dead_until = now + 10.0   # brief cooldown; don't hammer every observe()
        return None

    async def ensure(self) -> CdpSession | None:
        if self.session is not None and not self.session.closed:
            return self.session
        if self.session is not None and self.session.closed:
            await self.session.close()
            self.session = None
        if self.port is None:
            await self.discover()
        if self.port is None:
            return None
        data = await self._http_json(f"http://{self.settings.cdp_host}:{self.port}/json/version")
        ws = (data or {}).get("webSocketDebuggerUrl") or self.version.get("webSocketDebuggerUrl")
        if not ws:
            return None
        try:
            session = CdpSession(ws)
            await session.connect()
        except Exception:
            self._failed_attempts += 1
            if self._failed_attempts >= 3:
                self._dead_until = time.time() + 30.0
                self.port = None
            return None
        self._failed_attempts = 0
        self.session = session
        return session

    async def eval_js(self, expression: str, url_contains: str | None = None,
                      timeout: float = 15.0) -> dict[str, Any]:
        session = await self.ensure()
        if session is None:
            return {"ok": False, "error": "CDP unavailable (no Chrome with --remote-debugging-port)"}
        out = await session.eval_js(expression, url_contains, timeout)
        if not out.get("ok") and "closed" in str(out.get("error", "")):
            self.session = None
        return out

    async def tabs(self) -> list[dict] | None:
        session = await self.ensure()
        if session is None:
            return None
        try:
            return await session.target_infos()
        except Exception:
            self.session = None
            return None

    async def create_tab(self, url: str) -> bool:
        session = await self.ensure()
        if session is None:
            return False
        try:
            return bool(await session.create_target(url))
        except Exception:
            return False

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None


# -- Chrome profile scanning (L3, filesystem truth) ------------------------------

def chrome_binary() -> Path | None:
    for c in CHROME_CANDIDATES:
        if c.exists():
            return c
    return None


def chrome_user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(local) / "Google" / "Chrome" / "User Data"


def chrome_profiles() -> list[dict[str, Any]]:
    base = chrome_user_data_dir()
    info: dict[str, Any] = {}
    try:
        raw = json.loads((base / "Local State").read_text(encoding="utf-8", errors="replace"))
        info = (raw.get("profile", {}) or {}).get("info_cache", {}) or {}
    except Exception:
        info = {}
    out: list[dict[str, Any]] = []
    candidates = [base / "Default"] + sorted(base.glob("Profile *"))
    for d in candidates:
        if not d.is_dir():
            continue
        meta = info.get(d.name, {})
        out.append({
            "dir": d.name,
            "name": meta.get("name") or d.name,
            "email": meta.get("user_name") or "",
            "gaia": meta.get("gaia_id") or "",
        })
    return out


def match_profile(query: str) -> dict[str, Any] | None:
    q = str(query).strip().casefold()
    profiles = chrome_profiles()
    for p in profiles:                 # exact dir / name / email
        if q in (p["dir"].casefold(), str(p["name"]).casefold(), p["email"].casefold()):
            return p
    for p in profiles:                 # substring
        hay = f"{p['dir']} {p['name']} {p['email']}".casefold()
        if q and q in hay:
            return p
    return None


def running_profile_dir() -> str | None:
    """--profile-directory from any live chrome process (honest when Chrome was
    launched by EVA; UNKNOWN when the user launched chrome normally)."""
    for cmdline in process_cmdlines(["chrome"]):
        for i, part in enumerate(cmdline):
            if part.startswith("--profile-directory="):
                return part.split("=", 1)[1]
            if part == "--profile-directory" and i + 1 < len(cmdline):
                return cmdline[i + 1]
    return None


def launch_chrome(profile_dir: str | None, url: str, port: int) -> bool:
    """Launch Chrome with the debug port. Only legal when Chrome is NOT running."""
    import subprocess
    exe = chrome_binary()
    if exe is None:
        return False
    args = [str(exe), f"--remote-debugging-port={port}", "--remote-allow-origins=*"]
    if profile_dir:
        args.append(f"--profile-directory={profile_dir}")
    if url:
        args.append(url)
    try:
        subprocess.Popen(args, close_fds=True)
        return True
    except OSError:
        return False
