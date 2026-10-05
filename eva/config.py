"""Settings + .env loading. Stdlib only (doc 08: no needless dependencies)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_env(root: Path = ROOT) -> dict[str, str]:
    """Load .env into os.environ without overriding real environment vars."""
    env = parse_env_file(root / ".env")
    for k, v in env.items():
        if v and not os.environ.get(k):
            os.environ[k] = v
    return env


@dataclass
class Settings:
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    logs_dir: Path = field(default_factory=lambda: ROOT / "data" / "logs")

    # Chrome DevTools Protocol attach (doc 04 L3, doc 12 stack)
    cdp_host: str = "127.0.0.1"
    cdp_ports: tuple[int, ...] = (9222, 9223, 9224)

    # Providers (see .env.example) — primary Groq, secondary APINEX (5 RPM aware)
    groq_key: str = ""
    groq_base: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    apinex_key: str = ""
    apinex_base: str = "https://apinex.bond/v1"
    apinex_rpm: int = 5
    openai_key: str = ""
    openai_base: str = "https://api.groq.com/openai/v1"
    local_base: str = "http://127.0.0.1:11434/v1"
    local_model: str = "qwen2.5:3b"
    provider_timeout_s: float = 60.0

    # Vision (INV-1: only via vision gateway, only when configured)
    vision_model: str = ""
    allow_vision: bool = False

    # Avatar companion (AVATAR agent bus, EVA_VRM_INTEGRATION_PLAN.md)
    avatar_bus: str = "http://127.0.0.1:47903"
    avatar_token: str = ""

    # Voice
    voice_enabled: bool = True
    voice_name: str = "en-US-AnaNeural"

    # Budget / behavior (doc 08)
    max_task_steps: int = 20
    confirm_timeout_s: float = 600.0
    free_ram_degraded_mb: int = 1500

    # Actor loop budgets (M1) — the actor decides per cycle, so it needs its own limits
    actor_max_calls: int = 24            # decisions per task
    actor_token_budget: int = 60000      # in+out tokens per task
    actor_wall_clock_s: float = 900.0    # hard wall-clock ceiling per task
    actor_menu_size: int = 14            # relevance-filtered tool menu size
    actor_repairs: int = 1               # bounded "you are stuck" re-prompts before BLOCKED
    actor_escalate: bool = True          # fast lane failure -> general actor loop

    allowed_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        home = Path.home()
        if not self.allowed_roots:
            roots = [self.root, home / "Desktop", home / "Documents", home / "Downloads"]
            self.allowed_roots = tuple(p for p in roots)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "eva.db"

    @classmethod
    def from_env(cls, root: Path = ROOT) -> "Settings":
        load_env(root)
        env = os.environ
        ports = tuple(
            int(p) for p in env.get("EVA_CDP_PORTS", "9222,9223,9224").split(",") if p.strip()
        )
        api_root = Path(env.get("EVA_ALLOWED_ROOT", "")) if env.get("EVA_ALLOWED_ROOT") else None
        s = cls(
            root=root,
            cdp_host=env.get("EVA_CDP_HOST", "127.0.0.1"),
            cdp_ports=ports or (9222,),
            groq_key=env.get("GROQ_API_KEY", ""),
            groq_base=env.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            groq_model=env.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            apinex_key=env.get("APINEX_API_KEY", ""),
            apinex_base=env.get("APINEX_BASE_URL", "https://apinex.bond/v1"),
            apinex_rpm=int(env.get("APINEX_RPM_LIMIT", "5") or 5),
            openai_key=env.get("OPENAI_API_KEY", ""),
            openai_base=env.get("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
            vision_model=env.get("VISION_MODEL", ""),
            allow_vision=(env.get("EVA_ALLOW_VISION", "0") not in ("", "0", "false", "False")),
            avatar_bus=env.get("AVA_BUS_URL", "http://127.0.0.1:47903"),
            avatar_token=env.get("AVA_BUS_TOKEN", ""),
            voice_enabled=(env.get("EVA_VOICE", "1") not in ("0", "false", "False")),
            voice_name=env.get("EVA_VOICE_NAME", "en-US-AnaNeural"),
            max_task_steps=int(env.get("EVA_MAX_STEPS", "20") or 20),
            actor_max_calls=int(env.get("EVA_ACTOR_MAX_CALLS", "24") or 24),
            actor_token_budget=int(env.get("EVA_ACTOR_TOKEN_BUDGET", "60000") or 60000),
            actor_wall_clock_s=float(env.get("EVA_ACTOR_WALL_CLOCK_S", "900") or 900),
            actor_menu_size=int(env.get("EVA_ACTOR_MENU_SIZE", "14") or 14),
            actor_repairs=int(env.get("EVA_ACTOR_REPAIRS", "1") or 1),
            actor_escalate=(env.get("EVA_ACTOR_ESCALATE", "1") not in ("0", "false", "False")),
        )
        if api_root:
            s.allowed_roots = s.allowed_roots + (api_root,)
        return s


_SETTINGS: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _SETTINGS
    if _SETTINGS is None or reload:
        _SETTINGS = Settings.from_env()
    return _SETTINGS
