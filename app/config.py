from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/growth_os.db"
    workspace_dir: Path = Path("./workspace")

    # Cookie "Secure" deve ficar ligado em produção (atrás do Caddy com HTTPS).
    cookie_secure: bool = True
    session_hours: int = 12

    max_login_failures: int = 5
    lockout_minutes: int = 15

    command_timeout_seconds: int = 120
    command_max_output_bytes: int = 200_000

    provider_timeout_seconds: int = 300
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
