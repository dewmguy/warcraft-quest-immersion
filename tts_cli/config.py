from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from tts_cli.paths import PROJECT_ROOT


class ConfigurationError(ValueError):
    """Raised when a requested feature does not have valid configuration."""


@dataclass(frozen=True)
class Settings:
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "wow"
    mysql_database: str = "mangos"
    elevenlabs_api_key: str | None = None

    def require_elevenlabs(self) -> str:
        if not self.elevenlabs_api_key or self.elevenlabs_api_key == "API_KEY_HERE":
            raise ConfigurationError(
                "ELEVENLABS_API_KEY is required for audio generation. "
                "Add it to .env or your environment."
            )
        return self.elevenlabs_api_key


def _parse_port(raw_port: str) -> int:
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("MYSQL_PORT must be an integer.") from error

    if not 1 <= port <= 65535:
        raise ConfigurationError("MYSQL_PORT must be between 1 and 65535.")
    return port


def load_settings(
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> Settings:
    """Load configuration without making optional services import-time requirements."""
    if environ is None:
        dotenv_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        load_dotenv(dotenv_path=dotenv_path, override=False)
        environ = os.environ

    return Settings(
        mysql_host=environ.get("MYSQL_HOST", "localhost").strip(),
        mysql_port=_parse_port(environ.get("MYSQL_PORT", "3306")),
        mysql_user=environ.get("MYSQL_USER", "root").strip(),
        mysql_password=environ.get("MYSQL_PASSWORD", "wow"),
        mysql_database=environ.get("MYSQL_DATABASE", "mangos").strip(),
        elevenlabs_api_key=environ.get("ELEVENLABS_API_KEY") or None,
    )
