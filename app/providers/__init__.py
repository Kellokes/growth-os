from .base import ChatTurn, Provider, ProviderError
from .claude_cli import ClaudeCLIProvider
from .codex_cli import CodexCLIProvider
from .openrouter import OpenRouterProvider


def all_providers() -> dict[str, Provider]:
    providers: list[Provider] = [ClaudeCLIProvider(), CodexCLIProvider(), OpenRouterProvider()]
    return {p.id: p for p in providers}


__all__ = ["ChatTurn", "Provider", "ProviderError", "all_providers"]
