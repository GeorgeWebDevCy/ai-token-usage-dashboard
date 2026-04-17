"""Configuration loading.

Config lives at ``~/.ai-token-dashboard/config.yaml``. On first run the file is
created with sensible defaults so the user can opt-in to individual collectors
or change poll intervals without editing code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_APP_DIR = Path.home() / ".ai-token-dashboard"
DEFAULT_CONFIG_PATH = DEFAULT_APP_DIR / "config.yaml"
DEFAULT_DB_PATH = DEFAULT_APP_DIR / "tokens.db"


def _default_config() -> dict[str, Any]:
    """Return the default config dict written on first run.

    Each collector is disabled-by-default for sources that require secrets and
    enabled-by-default for sources that just tail local files. The user can
    flip any of them in the YAML.
    """
    return {
        "server": {"host": "127.0.0.1", "port": 8765},
        "database": {"path": str(DEFAULT_DB_PATH)},
        "display": {
            # Default to EUR per user preference; change to USD/GBP/etc.
            "currency": "EUR",
            # If set, skip live FX fetch entirely and use this as USD->currency.
            "usd_rate_override": None,
            # Used if Frankfurter is unreachable and no cache exists.
            # Approx. USD->EUR as of 2026-04; update if you need precision offline.
            "usd_rate_fallback": 0.92,
        },
        "collectors": {
            "claude_code": {
                "enabled": True,
                # Platform-independent defaults; resolved at runtime.
                # The Xcode path is Apple-only but harmless on other OSes —
                # it simply won't exist and the collector will skip it.
                "paths": [
                    "~/.claude/projects",
                    "~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects",
                ],
            },
            "codex": {
                "enabled": True,
                # Codex CLI has shipped under a few names; we try all.
                "paths": [
                    "~/.codex/sessions",
                    "~/.openai/sessions",
                    "~/.config/openai/sessions",
                ],
            },
            "gemini": {
                "enabled": True,
                "paths": [
                    "~/.gemini/tmp",
                    "~/.config/gemini/sessions",
                ],
            },
            "anthropic_api": {
                "enabled": False,
                "api_key_env": "ANTHROPIC_ADMIN_KEY",
                "poll_interval_s": 300,
            },
            "openai_api": {
                "enabled": False,
                "api_key_env": "OPENAI_ADMIN_KEY",
                "poll_interval_s": 300,
            },
            "google_api": {
                "enabled": False,
                "api_key_env": "GOOGLE_API_KEY",
                "poll_interval_s": 300,
            },
        },
    }


@dataclass
class CollectorConfig:
    enabled: bool = False
    paths: list[str] = field(default_factory=list)
    api_key_env: str | None = None
    poll_interval_s: int = 300

    @property
    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)

    @property
    def resolved_paths(self) -> list[Path]:
        return [Path(os.path.expanduser(p)) for p in self.paths]


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class DisplayConfig:
    currency: str = "EUR"
    usd_rate_override: float | None = None
    usd_rate_fallback: float = 0.92


@dataclass
class AppConfig:
    server: ServerConfig
    display: DisplayConfig
    db_path: Path
    collectors: dict[str, CollectorConfig]

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        """Load config from disk, creating a default file if none exists."""
        cfg_path = path or DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(yaml.safe_dump(_default_config(), sort_keys=False))
        raw = yaml.safe_load(cfg_path.read_text()) or {}
        server_raw = raw.get("server", {})
        db_raw = raw.get("database", {})
        display_raw = raw.get("display", {})
        coll_raw = raw.get("collectors", {})
        collectors = {
            name: CollectorConfig(
                enabled=bool(c.get("enabled", False)),
                paths=list(c.get("paths", [])),
                api_key_env=c.get("api_key_env"),
                poll_interval_s=int(c.get("poll_interval_s", 300)),
            )
            for name, c in coll_raw.items()
        }
        override = display_raw.get("usd_rate_override")
        return cls(
            server=ServerConfig(
                host=server_raw.get("host", "127.0.0.1"),
                port=int(server_raw.get("port", 8765)),
            ),
            display=DisplayConfig(
                currency=str(display_raw.get("currency", "EUR")).upper(),
                usd_rate_override=(float(override) if override is not None else None),
                usd_rate_fallback=float(display_raw.get("usd_rate_fallback", 0.92)),
            ),
            db_path=Path(os.path.expanduser(db_raw.get("path", str(DEFAULT_DB_PATH)))),
            collectors=collectors,
        )
