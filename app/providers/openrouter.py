import httpx

from ..config import get_settings
from .base import SYSTEM_PROMPT, ChatTurn, Provider, ProviderError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(Provider):
    id = "openrouter"
    label = "OpenRouter"

    def available(self) -> tuple[bool, str]:
        if not get_settings().openrouter_api_key:
            return False, "GOS_OPENROUTER_API_KEY não configurada"
        return True, ""

    async def complete(self, history: list[ChatTurn]) -> str:
        s = get_settings()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": t.role, "content": t.content} for t in history]
        async with httpx.AsyncClient(timeout=s.provider_timeout_seconds) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {s.openrouter_api_key}", "X-Title": "Growth OS"},
                json={"model": s.openrouter_model, "messages": messages},
            )
        if resp.status_code != 200:
            raise ProviderError(f"OpenRouter respondeu {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError("resposta inesperada do OpenRouter") from e
