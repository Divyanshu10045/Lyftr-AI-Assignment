"""Runtime configuration. Everything comes from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant. Answer clearly and accurately, "
    "and use the earlier messages in this conversation for context."
)


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def sqlite_path_from_url(database_url: str) -> str:
    """`sqlite:////data/app.db` -> `/data/app.db`, `sqlite:///./app.db` -> `./app.db`."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(
            f"DATABASE_URL must be a SQLite URL like sqlite:////data/app.db (got {database_url!r})"
        )
    return database_url[len(prefix):] or ":memory:"


@dataclass(frozen=True)
class Settings:
    api_keys: list[str] = field(default_factory=list)
    model: str = "gemini/gemini-2.5-flash"
    api_base: str | None = None
    database_url: str = "sqlite:///./data/app.db"
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    enable_failure_simulation: bool = False
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    cache_enabled: bool = True
    max_history_messages: int = 40
    key_cooldown_seconds: float = 60.0
    request_timeout_seconds: float = 30.0
    temperature: float = 0.0
    max_output_tokens: int | None = None

    @property
    def db_path(self) -> str:
        return sqlite_path_from_url(self.database_url)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        max_tokens = _int(e.get("LLM_MAX_OUTPUT_TOKENS"), 0)
        return cls(
            api_keys=_split(e.get("LLM_API_KEYS")),
            model=(e.get("LLM_MODEL") or cls.model).strip(),
            api_base=(e.get("LLM_API_BASE") or "").strip() or None,
            database_url=(e.get("DATABASE_URL") or cls.database_url).strip(),
            allowed_origins=_split(e.get("ALLOWED_ORIGINS")) or ["http://localhost:3000"],
            enable_failure_simulation=_bool(e.get("ENABLE_FAILURE_SIMULATION")),
            default_system_prompt=(e.get("DEFAULT_SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT).strip(),
            cache_enabled=_bool(e.get("CACHE_ENABLED"), True),
            max_history_messages=max(2, _int(e.get("MAX_HISTORY_MESSAGES"), 40)),
            key_cooldown_seconds=max(0.0, _float(e.get("KEY_COOLDOWN_SECONDS"), 60.0)),
            request_timeout_seconds=max(1.0, _float(e.get("REQUEST_TIMEOUT_SECONDS"), 30.0)),
            temperature=_float(e.get("LLM_TEMPERATURE"), 0.0),
            max_output_tokens=max_tokens or None,
        )
