"""edge-tts voice output with a robust playback fallback chain.

EVA owns the TTS; the AVATAR companion only listens to system audio for
lip-sync (EVA_VRM_INTEGRATION_PLAN §3.2). Playback delegates to a small
powershell MediaPlayer fallback when ffplay is absent — never blocks the loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path


class TTS:
    def __init__(self, settings, avatar=None, bus=None):
        self.settings = settings
        self.avatar = avatar
        self.bus = bus
        self.cache_dir = settings.data_dir / "voice"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.failures = 0

    async def say(self, text: str) -> bool:
        if not self.settings.voice_enabled or not text.strip():
            return False
        try:
            import edge_tts  # lazy: voice is optional (doc 08 optional-by-default)
        except ImportError:
            return False
        key = hashlib.sha256(f"{self.settings.voice_name}:{text}".encode()).hexdigest()[:20]
        path = self.cache_dir / f"{key}.mp3"
        if not path.exists():
            try:
                communicate = edge_tts.Communicate(text[:600], self.settings.voice_name)
                await communicate.save(str(path))
            except Exception:
                self.failures += 1
                return False
        if self.avatar is not None:
            self.avatar.on_speak()
        if self.bus is not None:
            self.bus.emit("VOICE_TTS_START", text=text[:120])
        ok = await asyncio.to_thread(self._play, path)
        if self.bus is not None:
            self.bus.emit("VOICE_TTS_DONE", ok=ok)
        return ok

    def _play(self, path: Path) -> bool:
        ffplay = shutil.which("ffplay")
        try:
            if ffplay:
                subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
                               timeout=60)
                return True
            # Windows fallback: WPF MediaPlayer via PowerShell (no extra deps)
            ps = (
                "Add-Type -AssemblyName presentationCore;"
                "$p = New-Object System.Windows.Media.MediaPlayer;"
                f"$p.Open([uri]'{path.as_uri()}');"
                "$p.Play(); Start-Sleep -Milliseconds 800;"
                "while ($p.NaturalDuration.HasTimeSpan -and $p.Position -lt $p.NaturalDuration.TimeSpan) "
                "{ Start-Sleep -Milliseconds 200 }"
            )
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=90)
            return proc.returncode == 0
        except Exception:
            return False
