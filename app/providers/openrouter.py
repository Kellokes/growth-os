import httpx

from ..config import get_settings
from .base import SYSTEM_PROMPT, ChatTurn, Provider, ProviderError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _api_key() -> str:
    # Chave colada no .env com espaço ou aspas vira 401 "user not found": limpa antes de usar.
    return get_settings().openrouter_api_key.strip().strip("\"'").strip()


class OpenRouterProvider(Provider):
    id = "openrouter"
    label = "OpenRouter"

    def available(self) -> tuple[bool, str]:
        if not _api_key():
            return False, "GOS_OPENROUTER_API_KEY não configurada"
        return True, ""

    async def complete(self, history: list[ChatTurn]) -> str:
        s = get_settings()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": t.role, "content": t.content} for t in history]
        body: dict = {"model": self.model or s.openrouter_model, "messages": messages}
        if self.reasoning == "none":
            body["reasoning"] = {"enabled": False}
        elif self.reasoning:
            body["reasoning"] = {"effort": self.reasoning}
        async with httpx.AsyncClient(timeout=s.provider_timeout_seconds) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {_api_key()}", "X-Title": "Growth OS"},
                json=body,
            )
        if resp.status_code == 401:
            # "user not found" = a chave não existe/foi revogada na conta do OpenRouter (não é o modelo).
            raise ProviderError(
                "OpenRouter recusou a chave (401). Confira GOS_OPENROUTER_API_KEY no .env do VPS "
                "(sem aspas nem espaços) e, se preciso, crie outra em openrouter.ai/keys."
            )
        if resp.status_code != 200:
            raise ProviderError(f"OpenRouter respondeu {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError("resposta inesperada do OpenRouter") from e
