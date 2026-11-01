import asyncio

import httpx

from ..config import get_settings
from .base import SYSTEM_PROMPT, ChatTurn, Provider, ProviderError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 502/503 no OpenRouter quase sempre é o provedor do modelo falhando ou sobrecarregado: costuma passar sozinho.
RETRYABLE = {408, 429, 500, 502, 503, 504, 520, 522, 524, 529}
RETRY_DELAYS = (2.0, 6.0)  # pausas entre as tentativas (os testes zeram)


def _api_key() -> str:
    # Chave colada no .env com espaço ou aspas vira 401 "user not found": limpa antes de usar.
    return get_settings().openrouter_api_key.strip().strip("\"'").strip()


def _error_detail(data) -> tuple[int | None, str]:
    """Código e mensagem do corpo de erro do OpenRouter ({"error": {"code", "message", "metadata"}})."""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None, ""
    code = err.get("code") if isinstance(err.get("code"), int) else None
    msg = str(err.get("message") or "").strip()
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    if meta.get("provider_name"):
        msg = f"{msg} (provedor: {meta['provider_name']})"
    return code, msg[:300]


def _is_context_error(status: int, msg: str) -> bool:
    low = msg.lower()
    return status == 413 or (status == 400 and ("context" in low or "too long" in low or ("maximum" in low and "token" in low)))


def _explain(status: int, msg: str, model: str) -> ProviderError:
    if status == 401:
        # "user not found" = a chave não existe/foi revogada na conta do OpenRouter (não é o modelo).
        return ProviderError(
            "OpenRouter recusou a chave (401). Confira GOS_OPENROUTER_API_KEY no .env do VPS "
            "(sem aspas nem espaços) e, se preciso, crie outra em openrouter.ai/keys."
        )
    if status == 402:
        return ProviderError("OpenRouter sem créditos (402). Adicione créditos em openrouter.ai/credits ou use um modelo grátis.")
    if _is_context_error(status, msg):
        return ProviderError(
            f"A conversa ficou grande demais para o modelo {model} ({status}: {msg}). "
            "Comece uma thread nova com um resumo, ou escolha um modelo com contexto maior."
        )
    if status in RETRYABLE:
        what = {429: "limite de uso do modelo", 408: "tempo esgotado", 524: "tempo esgotado"}.get(status, "o provedor do modelo falhou")
        return ProviderError(
            f"OpenRouter respondeu {status} ({what}) mesmo depois de tentar de novo: {msg or 'sem detalhe'}. "
            "É temporário: clique em Tentar de novo, ou troque de modelo se continuar.",
            retryable=True,
        )
    return ProviderError(f"OpenRouter respondeu {status}: {msg or 'sem detalhe'}")


class OpenRouterProvider(Provider):
    id = "openrouter"
    label = "OpenRouter"

    def available(self) -> tuple[bool, str]:
        if not _api_key():
            return False, "GOS_OPENROUTER_API_KEY não configurada"
        return True, ""

    async def _post(self, client: httpx.AsyncClient, body: dict) -> tuple[int, dict | None, str]:
        resp = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {_api_key()}", "X-Title": "Growth OS"},
            json=body,
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        return resp.status_code, data, resp.text[:300]

    async def complete(self, history: list[ChatTurn]) -> str:
        s = get_settings()
        model = self.model or s.openrouter_model
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": t.role, "content": t.content} for t in history]
        body: dict = {"model": model, "messages": messages}
        if self.reasoning == "none":
            body["reasoning"] = {"enabled": False}
        elif self.reasoning:
            body["reasoning"] = {"effort": self.reasoning}

        status, msg = 0, ""
        async with httpx.AsyncClient(timeout=s.provider_timeout_seconds) as client:
            for attempt in range(len(RETRY_DELAYS) + 1):
                if attempt:
                    await asyncio.sleep(RETRY_DELAYS[attempt - 1])
                try:
                    status, data, text = await self._post(client, body)
                except httpx.TimeoutException:
                    raise ProviderError(
                        f"OpenRouter não respondeu em {s.provider_timeout_seconds}s com o modelo {model}. "
                        "Clique em Tentar de novo, ou use um modelo mais rápido / raciocínio menor.",
                        retryable=True,
                    ) from None
                except httpx.HTTPError as e:
                    status, msg = 0, f"falha de rede ({type(e).__name__})"
                    continue
                code, msg = _error_detail(data)
                msg = msg or ("" if data is not None else text)
                if status == 200 and code:  # erro no meio da geração vem com 200 e corpo de erro
                    status = code
                if status == 200:
                    return self._content(data)
                if status not in RETRYABLE:
                    break
        if status == 0:
            raise ProviderError(f"Não consegui falar com o OpenRouter: {msg}. Tente de novo.", retryable=True)
        raise _explain(status, msg, model)

    @staticmethod
    def _content(data) -> str:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError("resposta inesperada do OpenRouter", retryable=True) from e
        content = (choice.get("message") or {}).get("content") or ""
        if isinstance(content, list):  # alguns modelos devolvem partes
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = content.strip()
        if not content and choice.get("finish_reason") == "length":
            raise ProviderError(
                "O modelo gastou o limite de saída (provavelmente no raciocínio) e não respondeu. "
                "Tente de novo com raciocínio menor.",
                retryable=True,
            )
        return content
