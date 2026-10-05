"""Deterministic app registry (doc 12 Tier-0 fallback): name -> exact launch target.

User overrides live in `data/apps.json` (merged over the built-ins):
    {"myapp": {"exe": "C:/path/app.exe", "process": ["myapp"]}}

Web apps are a *knowledge table*, not code paths: `data/webapps.json` can add or
repoint any web app without touching Python:
    {"myportal": {"url": "https://portal.example.com/", "aliases": ["myportal"],
                  "capabilities": ["chat"]}}
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_APPS: dict[str, dict] = {
    "chrome": {"exe": ["C:/Program Files/Google/Chrome/Application/chrome.exe",
                        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
                        "chrome"],
               "process": ["chrome"]},
    "edge": {"exe": ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "msedge"],
             "process": ["msedge"]},
    "firefox": {"exe": ["C:/Program Files/Mozilla Firefox/firefox.exe", "firefox"],
                "process": ["firefox"]},
    "notepad": {"exe": ["notepad.exe"], "process": ["notepad"]},
    "explorer": {"exe": ["explorer.exe"], "process": ["explorer"]},
    "calculator": {"exe": ["calc.exe"], "process": ["CalculatorApp", "calc"]},
    "cmd": {"exe": ["cmd.exe"], "process": ["cmd"]},
    "powershell": {"exe": ["powershell.exe"], "process": ["powershell"]},
    "terminal": {"exe": ["wt.exe", "cmd.exe"], "process": ["windowsterminal", "wt", "cmd"]},
    "vscode": {"exe": ["C:/Users/%USERNAME%/AppData/Local/Programs/Microsoft VS Code/Code.exe",
                        "code.cmd", "code"],
               "process": ["code"]},
    "antigravity": {"exe": ["C:/Users/%USERNAME%/AppData/Local/Programs/antigravity/Antigravity.exe",
                             "antigravity.exe"],
                    "process": ["antigravity"]},
    "spotify": {"exe": ["C:/Users/%USERNAME%/AppData/Roaming/Spotify/Spotify.exe", "spotify"],
                "process": ["spotify"]},
    "vlc": {"exe": ["C:/Program Files/VideoLAN/VLC/vlc.exe", "vlc"], "process": ["vlc"]},
    "word": {"exe": ["winword.exe"], "process": ["winword"]},
    "excel": {"exe": ["excel.exe"], "process": ["excel"]},
    "whatsapp": {"exe": ["C:/Users/%USERNAME%/AppData/Local/WhatsApp/WhatsApp.exe"],
                 "process": ["whatsapp"]},
    "telegram": {"exe": ["C:/Users/%USERNAME%/AppData/Roaming/Telegram Desktop/Telegram.exe"],
                 "process": ["telegram"]},
    "discord": {"exe": ["C:/Users/%USERNAME%/AppData/Local/Discord/Update.exe"],
                "process": ["discord"]},
    "freebuff": {"exe": ["freebuff.exe"], "process": ["freebuff"]},
}

_ALIASES = {
    "browser": "chrome", "google chrome": "chrome", "chrome browser": "chrome",
    "file explorer": "explorer", "files": "explorer", "this pc": "explorer",
    "vs code": "vscode", "visual studio code": "vscode", "code": "vscode",
    "calc": "calculator", "terminal": "terminal", "windows terminal": "terminal",
    "command prompt": "cmd", "notepad++": "notepad", "whats app": "whatsapp",
}


# ----------------------------------------------------------------- web apps
# Addresses/aliases only — machine-specific values never live here.  The whole
# table (and anything in data/webapps.json) is re-resolved by match_web_app().
WEB_APPS: dict[str, dict] = {
    "whatsapp": {"url": "https://web.whatsapp.com/",
                 "aliases": ["whatsapp", "whats app", "whatsapp web", "whats", "wa"],
                 "capabilities": ["chat"]},
    "youtube": {"url": "https://www.youtube.com/", "aliases": ["youtube"]},
    "gmail": {"url": "https://mail.google.com/", "aliases": ["gmail"]},
}


@dataclass
class AppInfo:
    name: str
    exe: str | None
    process: list[str] = field(default_factory=list)

    def exists(self) -> bool:
        return self.exe is not None


def _expand(p: str) -> str:
    p = p.replace("%USERNAME%", os.environ.get("USERNAME", ""))
    p = os.path.expandvars(p)
    return p


def _resolve_exe(candidates: list[str]) -> str | None:
    for cand in candidates:
        cand = _expand(cand)
        p = Path(cand)
        if p.exists():
            return str(p)
        which = shutil.which(cand)
        if which:
            return which
    # OS-level truth before giving up: Windows App Paths registry
    for cand in candidates:
        hit = _app_paths_lookup(Path(_expand(cand)).name)
        if hit:
            return hit
    return None


def _app_paths_lookup(exe_name: str) -> str | None:
    """Ask Windows itself where an executable lives (App Paths), so the literal
    install-path candidates above are only hints, never the last word."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    exe = exe_name if exe_name.casefold().endswith(".exe") else f"{exe_name}.exe"
    sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive, extra in ((winreg.HKEY_CURRENT_USER, 0),
                        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0))):
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | extra) as k:
                val, _ = winreg.QueryValueEx(k, "")
                if val and Path(val).exists():
                    return str(val)
        except OSError:
            continue
    return None


def _load_overrides() -> dict:
    try:
        from ..config import ROOT
        f = ROOT / "data" / "apps.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _load_webapp_overrides() -> dict:
    try:
        from ..config import ROOT
        f = ROOT / "data" / "webapps.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def web_apps() -> dict[str, dict]:
    """Built-in web-app knowledge merged with data/webapps.json overrides."""
    table = {k: dict(v) for k, v in WEB_APPS.items()}
    for name, spec in (_load_webapp_overrides() or {}).items():
        table[str(name).casefold()] = {**table.get(str(name).casefold(), {}), **spec}
    return table


def _norm_token(t: str) -> str:
    return "".join(ch for ch in str(t).casefold() if ch.isalnum())


def match_web_app(tokens) -> dict | None:
    """Resolve any web app mentioned in the user's words — vocabulary comes from
    the table (aliases), never from app-specific code branches."""
    toks = {_norm_token(t) for t in tokens}
    toks.discard("")
    if not toks:
        return None
    for name, spec in web_apps().items():
        words = {_norm_token(name)} | {_norm_token(a) for a in spec.get("aliases", [])}
        words.discard("")
        if toks & words:
            return {"name": name, **spec}
    return None


def find_web_app(query: str) -> dict | None:
    q = " ".join(str(query).strip().casefold().split())
    if not q:
        return None
    table = web_apps()
    for name, spec in table.items():
        aliases = [str(a).casefold() for a in spec.get("aliases", [])] + [name]
        if q in aliases:
            return {"name": name, **spec}
    for name, spec in table.items():
        if q in name or name in q:
            return {"name": name, **spec}
    return None


def list_apps() -> list[str]:
    return sorted(set(KNOWN_APPS) | set(_load_overrides()))


def find_app(query: str) -> AppInfo | None:
    q = " ".join(str(query).strip().casefold().split())
    if not q:
        return None
    q = _ALIASES.get(q, q)
    table = dict(KNOWN_APPS)
    table.update(_load_overrides())
    if q in table:
        spec = table[q]
        exe = _resolve_exe(list(spec.get("exe", [])))
        return AppInfo(name=q, exe=exe, process=list(spec.get("process", [q])))
    for name, spec in table.items():
        if q in name or name in q:
            exe = _resolve_exe(list(spec.get("exe", [])))
            return AppInfo(name=name, exe=exe, process=list(spec.get("process", [name])))
    # last resort: a raw command that exists on PATH
    which = shutil.which(q)
    if which:
        return AppInfo(name=q, exe=which, process=[Path(which).stem])
    return None


def launch_app(app: AppInfo, args: list[str] | None = None) -> bool:
    if not app.exe:
        return False
    try:
        subprocess.Popen([app.exe, *(args or [])], close_fds=True)
        return True
    except OSError:
        return False
