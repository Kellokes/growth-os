"""Registro de rotas do OpenRouter: cada modelo na rota certa, e o /settings não deixa escolher rota incompatível."""
import asyncio
import base64
import json

import httpx
import pytest

from app import endpoints, layouts as lay, media, modelcfg
from app.config import get_settings
from app.providers import openrouter as orr
from app.providers.base import ProviderError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
FLARE = "openai/gpt-image-2.6-flare"


@pytest.fixture()
def fake_or(monkeypatch):
    """OpenRouter falso: devolve as respostas da lista na ordem e guarda url e corpo de cada pedido."""
    monkeypatch.setenv("GOS_OPENROUTER_API_KEY", "sk-or-teste")
    get_settings.cache_clear()
    real = httpx.AsyncClient
    seen: list[tuple[str, dict]] = []

    def setup(responses):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), json.loads(request.content or b"{}")))
            return responses.pop(0)

        class Fake(real):
            def __init__(self, *a, **kw):
                super().__init__(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

        monkeypatch.setattr(httpx, "AsyncClient", Fake)
        return seen

    yield setup
    get_settings.cache_clear()


def _chat_image_ok():
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    return httpx.Response(200, json={"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]})


def _images_ok():
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})


# ---------- registro ----------

def test_every_model_in_the_openrouter_catalog_is_registered():
    """O que a tela oferece como sugestão tem rota registrada, do tipo certo."""
    for m in modelcfg.CATALOG["openrouter"]["models"]:
        entry = endpoints.lookup(m["id"])
        assert entry is not None, m["id"]
        assert m["kinds"] == [entry.kind], m["id"]


def test_registry_maps_models_to_routes():
    assert endpoints.lookup(FLARE).route == endpoints.IMAGES
    assert endpoints.lookup("google/gemini-2.5-flash-image").route == endpoints.CHAT_IMAGE
    assert endpoints.lookup("openai/gpt-5").route == endpoints.CHAT
    assert endpoints.lookup("quem/nao-existe") is None and not endpoints.is_known("quem/nao-existe")


def test_unknown_model_uses_activity_default_and_says_so():
    assert endpoints.route_for("quem/nao-existe", "image") == endpoints.CHAT_IMAGE
    assert endpoints.route_for("quem/nao-existe", "text") == endpoints.CHAT
    assert "registro" in endpoints.unknown_warning("quem/nao-existe", "image")
    assert endpoints.unknown_warning(FLARE, "image") == ""


def test_incompatible_only_for_registered_models():
    assert endpoints.incompatible(FLARE, "text")
    assert endpoints.incompatible("openai/gpt-5", "image")
    assert endpoints.incompatible(FLARE, "image") is None
    assert endpoints.incompatible("quem/nao-existe", "text") is None  # desconhecido não é bloqueado


def test_wrong_route_hint_reads_the_openrouter_message():
    msg = ("openai/x is an image generation model and cannot be used with the chat/completions endpoint. "
           "Use the /api/v1/images endpoint instead.")
    assert endpoints.wrong_route_hint(msg) == endpoints.IMAGES
    assert endpoints.wrong_route_hint("No endpoints found that support the requested output modalities") is None


# ---------- /settings ----------

@pytest.mark.parametrize("activity,model,status", [
    ("image", FLARE, 200),
    ("image", "openai/gpt-5", 400),  # modelo de texto em Imagens
    ("threads", FLARE, 400),  # modelo de imagem em Threads
    ("layout_html", "google/gemini-2.5-flash-image", 400),
    ("image", "quem/nao-existe", 200),  # desconhecido passa, com aviso
])
def test_settings_cannot_pick_an_incompatible_endpoint(logged, activity, model, status):
    r = logged.put(f"/api/models/{activity}", json={"provider": "openrouter", "model": model})
    assert r.status_code == status


def test_settings_warns_on_unknown_model_only(logged):
    r = logged.put("/api/models/image", json={"provider": "openrouter", "model": "quem/nao-existe"}).json()
    assert "registro" in r["warning"]
    r = logged.put("/api/models/image", json={"provider": "openrouter", "model": FLARE}).json()
    assert r["warning"] == ""


# ---------- chamadas ----------

def test_registered_images_model_goes_to_images_endpoint(fake_or):
    seen = fake_or([_images_ok()])
    data, mime = asyncio.run(lay.request_image(FLARE, "um gato", [], "9:16"))
    assert mime == "image/png" and data == PNG
    assert [u for u, _ in seen] == ["https://openrouter.ai/api/v1/images"]
    assert seen[0][1]["model"] == FLARE and seen[0][1]["prompt"] == "um gato"


def test_chat_image_model_keeps_using_chat_completions(fake_or):
    seen = fake_or([_chat_image_ok()])
    asyncio.run(lay.request_image("google/gemini-2.5-flash-image", "um gato", [], "9:16"))
    assert [u for u, _ in seen] == ["https://openrouter.ai/api/v1/chat/completions"]
    assert seen[0][1]["modalities"] == ["image", "text"]


def test_images_endpoint_with_url_response_downloads_once(fake_or):
    seen = fake_or([httpx.Response(200, json={"data": [{"url": "https://cdn.example/x.png"}]}),
                    httpx.Response(200, content=PNG)])
    data, _ = asyncio.run(lay.request_image(FLARE, "p", [], "1:1"))
    assert data == PNG and seen[1][0] == "https://cdn.example/x.png"


def test_unknown_model_switches_route_when_openrouter_says_so(fake_or):
    msg = ("x/novo-img is an image generation model and cannot be used with the chat/completions endpoint. "
           "Use the /api/v1/images endpoint instead.")
    seen = fake_or([httpx.Response(404, json={"error": {"message": msg, "code": 404}}), _images_ok()])
    data, _ = asyncio.run(lay.request_image("x/novo-img", "p", [], "9:16"))
    assert data == PNG
    assert [u.rsplit("/", 1)[-1] for u, _ in seen] == ["completions", "images"]


def test_registered_chat_model_does_not_switch_route_silently(fake_or):
    msg = "boom: Use the /api/v1/images endpoint instead."
    fake_or([httpx.Response(404, json={"error": {"message": msg, "code": 404}})])
    with pytest.raises(ProviderError, match="404"):
        asyncio.run(lay.request_image("google/gemini-2.5-flash-image", "p", [], "9:16"))


def test_images_model_never_reaches_text_calls(fake_or):
    seen = fake_or([])
    with pytest.raises(ProviderError, match="não serve para texto"):
        asyncio.run(orr.OpenRouterProvider().configured(FLARE).complete([orr.ChatTurn("user", "oi")]))
    with pytest.raises(ProviderError, match="não serve para texto"):
        asyncio.run(lay._request_html(FLARE, "p", (PNG, "image/png")))
    assert seen == []  # nem saiu requisição


def test_text_model_never_reaches_image_or_video_calls(fake_or):
    seen = fake_or([])
    with pytest.raises(ProviderError, match="não serve para imagem"):
        asyncio.run(lay.request_image("openai/gpt-5", "p", [], "9:16"))
    with pytest.raises(ProviderError, match="não serve para vídeo"):
        asyncio.run(media.submit_video("openai/gpt-5", "p", "9:16"))
    assert seen == []


def test_images_endpoint_sends_references_as_input_references(fake_or):
    seen = fake_or([_images_ok()])
    asyncio.run(lay.request_image(FLARE, "use a carta", [(PNG, "image/png"), (PNG, "image/png")], "9:16"))
    refs = seen[0][1]["input_references"]
    assert len(refs) == 2 and all(r["type"] == "image_url" and r["image_url"]["url"].startswith("data:image/png;base64,") for r in refs)


def test_images_endpoint_without_refs_has_no_input_references(fake_or):
    seen = fake_or([_images_ok()])
    asyncio.run(lay.request_image(FLARE, "um gato", [], "9:16"))
    assert "input_references" not in seen[0][1]


def test_images_endpoint_retries_with_https_links_when_data_url_refused(fake_or, monkeypatch):
    monkeypatch.setattr(lay, "share_image", lambda d, m: "https://os.example/api/public/ref/tok")
    seen = fake_or([httpx.Response(400, json={"error": {"message": "Unsupported image URL scheme data"}}), _images_ok()])
    asyncio.run(lay.request_image(FLARE, "p", [(PNG, "image/png")], "1:1"))
    assert seen[1][1]["input_references"][0]["image_url"]["url"] == "https://os.example/api/public/ref/tok"


def test_images_endpoint_says_so_when_model_refuses_references(fake_or):
    fake_or([httpx.Response(400, json={"error": {"message": "unsupported parameter: input_references"}})])
    with pytest.raises(Exception, match="não aceita imagens de referência"):
        asyncio.run(lay.request_image(FLARE, "p", [(PNG, "image/png")], "1:1"))
