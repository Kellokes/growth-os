"""Registro de endpoints do OpenRouter: qual modelo fala com qual rota. Fonte única da verdade.

O OpenRouter não aceita qualquer modelo em qualquer rota. Ex.: "openai/gpt-image-2.6-flare" responde 404 em
/chat/completions ("is an image generation model ... Use the /api/v1/images endpoint instead").

Rotas:
  chat        /chat/completions, resposta em texto (conversas, agentes, layout em HTML com visão, áudio de entrada)
  chat_image  /chat/completions com "modalities": a imagem volta em message.images (Gemini Image, GPT-5 Image, Seedream)
  images      /images, modelos que só geram imagem (GPT Image 2.6 ...)
  video       /videos, assíncrono (cria o job, depois consulta)

Para ADICIONAR um modelo: uma linha em ROUTES abaixo (id exato do OpenRouter, a rota e o tipo de atividade). Modelo que não está
aqui é DESCONHECIDO: nada o bloqueia (para não quebrar o que já funciona), mas a tela avisa e o Growth OS não finge saber a rota.
"""
from dataclasses import dataclass

CHAT, CHAT_IMAGE, IMAGES, VIDEO = "chat", "chat_image", "images", "video"
BASE = "https://openrouter.ai/api/v1"
URLS = {CHAT: f"{BASE}/chat/completions", CHAT_IMAGE: f"{BASE}/chat/completions", IMAGES: f"{BASE}/images",
        VIDEO: f"{BASE}/videos"}


@dataclass(frozen=True)
class ModelRoute:
    route: str
    kind: str  # text | image | video | audio: para que atividade o modelo serve


def _r(route: str, kind: str) -> ModelRoute:
    return ModelRoute(route, kind)


ROUTES: dict[str, ModelRoute] = {
    # texto (conversas, agentes; também servem ao layout em HTML quando têm visão)
    "anthropic/claude-sonnet-4.5": _r(CHAT, "text"),
    "openai/gpt-5": _r(CHAT, "text"),
    "google/gemini-2.5-pro": _r(CHAT, "text"),
    "deepseek/deepseek-chat-v3.1": _r(CHAT, "text"),
    # imagem pelo /chat/completions com "modalities"
    "google/gemini-2.5-flash-image": _r(CHAT_IMAGE, "image"),
    "openai/gpt-5-image": _r(CHAT_IMAGE, "image"),
    "bytedance-seed/seedream-4.5": _r(CHAT_IMAGE, "image"),
    # imagem pelo /images (o OpenRouter recusa estes no /chat/completions)
    "openai/gpt-image-2.6-flare": _r(IMAGES, "image"),
    # áudio de entrada vai pelo /chat/completions
    "openai/gpt-4o-audio-preview": _r(CHAT, "audio"),
}

KIND_LABEL = {"text": "texto", "image": "imagem", "video": "vídeo", "audio": "áudio"}
# Rota padrão de cada atividade quando o modelo é desconhecido.
DEFAULT_ROUTE = {"text": CHAT, "audio": CHAT, "image": CHAT_IMAGE, "video": VIDEO}


def lookup(model: str) -> ModelRoute | None:
    """Rota registrada do modelo, ou None se ele é desconhecido."""
    return ROUTES.get((model or "").strip())


def is_known(model: str) -> bool:
    return lookup(model) is not None


def route_for(model: str, kind: str) -> str:
    """Rota a usar. Modelo registrado manda; desconhecido cai na rota padrão da atividade (e quem chama deve avisar)."""
    m = lookup(model)
    return m.route if m else DEFAULT_ROUTE.get(kind, CHAT)


def incompatible(model: str, kind: str) -> str | None:
    """Mensagem de erro se o modelo REGISTRADO não serve para a atividade `kind`; None se serve ou se é desconhecido."""
    m = lookup(model)
    if m is None or m.kind == kind:
        return None
    return (f"{model} é um modelo de {KIND_LABEL.get(m.kind, m.kind)} (rota {m.route}) e não serve para "
            f"{KIND_LABEL.get(kind, kind)}: escolha outro modelo para esta atividade")


def unknown_warning(model: str, kind: str) -> str:
    """Aviso quando o modelo não está no registro; "" se está registrado ou vazio."""
    if not model or is_known(model):
        return ""
    return (f"{model} ainda não está no registro de rotas (app/endpoints.py): vou tentar a rota padrão de "
            f"{KIND_LABEL.get(kind, kind)} ({DEFAULT_ROUTE.get(kind, CHAT)}) e, se o OpenRouter indicar outra, troco e aviso no log")


def wrong_route_hint(msg: str) -> str | None:
    """Rota que o próprio OpenRouter indica num erro 404 do tipo "use the /api/v1/images endpoint instead"."""
    low = (msg or "").lower()
    if "endpoint instead" not in low and "cannot be used with" not in low:
        return None
    if "/images" in low or "image generation model" in low:
        return IMAGES
    if "/videos" in low or "video generation model" in low:
        return VIDEO
    return None
