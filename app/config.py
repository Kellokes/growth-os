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
    # Laço do agente: passos seguidos antes de parar e pedir "continue", e quanto da conversa vai para a IA.
    agent_max_steps: int = 25
    agent_history_chars: int = 120_000
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"

    # Etapa 2: gateway do Docker (único com o socket) e Caddyfile montado só para leitura.
    gateway_url: str = "http://docker-gateway:2375"
    gateway_token: str = ""
    caddyfile_path: Path = Path("/projects/caddy/Caddyfile")
    caddy_vars: str = ""
    # Token fine-grained do GitHub só com os repos dos projetos (Contents + Pull requests).
    github_token: str = ""
    github_token_file: Path = Path("/tmp/gos-secrets/github_token")

    # Etapa 3: plano da manhã dos agentes.
    scheduler_enabled: bool = True
    timezone: str = "America/Sao_Paulo"
    plan_hour: int = 7  # hora local em que o plano do dia é gerado
    agent_provider: str = "claude"  # provider padrão dos agentes (cai no primeiro disponível)
    agent_concurrency: int = 2
    public_url: str = ""  # ex.: https://os.exemplo.com, para o link no aviso
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # ex.: "DOMAIN=exemplo.com" para resolver {$DOMAIN} do Caddyfile


@lru_cache
def get_settings() -> Settings:
    return Settings()
