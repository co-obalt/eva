"""Resource budget & degraded mode (doc 08 §2/§5).

Degraded triggers: free RAM below threshold · provider pool unavailable.
Degradation is ALWAYS explicit (MODE_CHANGED event + CLI banner), never silent.
"""
from __future__ import annotations

import time

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


class ResourceMonitor:
    def __init__(self, settings, bus=None):
        self.settings = settings
        self.bus = bus
        self.mode = "normal"
        self.reason = ""
        self._last_check = 0.0
        self._proc = psutil.Process() if psutil else None

    # ------------------------------------------------------------------ stats
    def rss_mb(self) -> float:
        if self._proc is None:
            return 0.0
        try:
            return round(self._proc.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            return 0.0

    def free_ram_mb(self) -> float:
        if psutil is None:
            return 99999.0
        try:
            return round(psutil.virtual_memory().available / (1024 * 1024), 1)
        except Exception:
            return 99999.0

    def cpu_percent(self) -> float:
        if psutil is None:
            return 0.0
        try:
            return psutil.cpu_percent(interval=None)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ mode
    def check(self, providers_available: bool = True) -> str:
        """Recompute mode at most every 5 s; emit MODE_CHANGED on transitions."""
        now = time.time()
        if now - self._last_check < 5.0:
            return self.mode
        self._last_check = now
        free = self.free_ram_mb()
        if free < self.settings.free_ram_degraded_mb:
            mode, reason = "degraded", f"free RAM {free:.0f} MB < {self.settings.free_ram_degraded_mb} MB"
        elif not providers_available:
            mode, reason = "degraded", "no model provider reachable (offline?)"
        else:
            mode, reason = "normal", ""
        if mode != self.mode:
            self.mode, self.reason = mode, reason
            if self.bus is not None:
                self.bus.emit("MODE_CHANGED", mode=mode, reason=reason)
        return self.mode

    def report(self) -> dict:
        return {"mode": self.mode, "reason": self.reason,
                "rss_mb": self.rss_mb(), "free_ram_mb": self.free_ram_mb(),
                "cpu_percent": self.cpu_percent()}

    def budget_ok(self, active: bool = False) -> bool:
        cap = 600.0 if active else 250.0
        return self.rss_mb() <= cap
