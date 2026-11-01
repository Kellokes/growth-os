import asyncio
import json

import pytest

from app import db as dbmod
from app import growth, modelcfg
from app.providers.base import ChatTurn, Provider
from app.providers.claude_cli import ClaudeCLIProvider
from app.providers.codex_cli import CodexCLIProvider


class FakeClaude(Provider):
    """Finge ser o Claude (para valer o catálogo) e guarda com qual modelo foi chamado."""

    id = "claude"
    label = "Claude (login da sua conta)"

    def __init__(self):
        self.calls = []

    async def complete(self, history):
        self.calls.append((self.model, self.reasoning))
        return "ok, feito."


@pytest.fixture()
def claude(monkeypatch):
    p = FakeClaude()
    monkeypatch.setattr("app.main.all_providers", lambda: {"claude": p})
    monkeypatch.setattr(growth, "all_providers", lambda: {"claude": p})
    return p


def test_save_and_resolve_settings(logged, claude):
    assert logged.put("/api/models/default", json={"provider": "claude", "model": "sonnet", "reasoning": "medium"}).status_code == 200
    assert logged.put("/api/models/threads", json={"provider": "claude", "model": "opus", "reasoning": "high"}).status_code == 200
    data = logged.get("/api/models").json()
    assert data["effective"]["threads"] == {"provider": "claude", "model": "opus", "reasoning": "high", "source": "threads"}
    # sem regra própria, segue o Padrão
    assert data["effective"]["agents_run"]["model"] == "sonnet"
    assert data["effective"]["agents_run"]["source"] == "default"
    # mídia não herda o Padrão de texto
    assert data["effective"]["image"]["provider"] == ""
    # "Seguir o padrão" limpa a regra
    assert logged.put("/api/models/threads", json={"provider": "", "model": "x", "reasoning": "high"}).status_code == 200
    assert logged.get("/api/models").json()["effective"]["threads"]["source"] == "default"


@pytest.mark.parametrize("activity,body,status", [
    ("threads", {"provider": "claude", "reasoning": "minimal"}, 400),  # nível que o Claude não tem
    ("threads", {"provider": "claude", "model": "opus; rm -rf /"}, 400),
    ("threads", {"provider": "nada"}, 400),
    ("image", {"provider": "claude"}, 400),  # Claude pela conta não gera imagem
    ("image", {"provider": "openrouter", "model": "google/gemini-2.5-flash-image"}, 200),
    ("codex_magico", {"provider": "claude"}, 404),
])
def test_validation(logged, activity, body, status):
    assert logged.put(f"/api/models/{activity}", json=body).status_code == status


def test_thread_uses_and_remembers_the_choice(logged, claude):
    cid = logged.post("/api/conversations").json()["id"]
    r = logged.post(f"/api/conversations/{cid}/messages",
                    json={"provider": "claude", "model": "haiku", "reasoning": "low", "content": "oi"})
    evs = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert claude.calls == [("haiku", "low")]
    assert any(e["type"] == "message" and e["model"] == "haiku" for e in evs)
    conv = logged.get(f"/api/conversations/{cid}").json()
    assert (conv["provider"], conv["model"], conv["reasoning"]) == ("claude", "haiku", "low")
    assert conv["messages"][-1]["model"] == "haiku"
    bad = logged.post(f"/api/conversations/{cid}/messages", json={"provider": "claude", "reasoning": "turbo", "content": "x"})
    assert bad.status_code == 400


def test_agents_pick_override_then_activity(logged, claude):
    s = dbmod.new_session()
    modelcfg.save(s, "default", "claude", "sonnet", "")
    modelcfg.save(s, "agents_plan", "claude", "opus", "max")
    assert growth.pick_provider(None, "agents_plan", s).model == "opus"
    assert growth.pick_provider(None, "agents_run", s).model == "sonnet"
    pa = dbmod.ProjectAgent(project_id=1, agent_slug="growth", provider="claude", model="haiku", reasoning="low")
    p = growth.pick_provider(pa, "agents_plan", s)
    assert (p.model, p.reasoning) == ("haiku", "low")
    assert claude.model == ""  # o original não muda
    s.close()


def test_agent_patch_accepts_model(logged, claude):
    s = dbmod.new_session()
    g = dbmod.GrowthProject(slug="zp", name="Zombie Party")
    s.add(g)
    s.commit()
    s.add(dbmod.ProjectAgent(project_id=g.id, agent_slug="growth"))
    s.commit()
    s.close()
    url = "/api/gprojects/zp/agents/growth"
    assert logged.patch(url, json={"provider": "claude", "model": "opus", "reasoning": "high"}).status_code == 200
    assert logged.patch(url, json={"provider": "claude", "reasoning": "turbo"}).status_code == 400
    s = dbmod.new_session()
    pa = s.query(dbmod.ProjectAgent).filter_by(agent_slug="growth").one()
    assert (pa.provider, pa.model, pa.reasoning) == ("claude", "opus", "high")
    s.close()
    # voltar para o padrão limpa modelo e raciocínio
    assert logged.patch(url, json={"provider": ""}).status_code == 200
    s = dbmod.new_session()
    pa = s.query(dbmod.ProjectAgent).filter_by(agent_slug="growth").one()
    assert (pa.provider, pa.model, pa.reasoning) == (None, "", "")
    s.close()


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    from app.config import get_settings
    monkeypatch.setenv("GOS_WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    seen = {}

    async def fake_run(args, cwd, timeout, stdin=None):
        seen["args"] = args
        return '{"result": "ok"}'

    monkeypatch.setattr("app.providers.claude_cli.run_cli", fake_run)
    monkeypatch.setattr("app.providers.codex_cli.run_cli", fake_run)
    yield seen
    get_settings.cache_clear()


def test_cli_flags(captured):
    hist = [ChatTurn("user", "oi")]
    asyncio.run(ClaudeCLIProvider().configured("opus", "high").complete(hist))
    a = captured["args"]
    assert a[a.index("--model") + 1] == "opus" and a[a.index("--effort") + 1] == "high"
    asyncio.run(ClaudeCLIProvider().complete(hist))
    assert "--model" not in captured["args"] and "--effort" not in captured["args"]
    asyncio.run(CodexCLIProvider().configured("gpt-5-codex", "xhigh").complete(hist))
    a = captured["args"]
    assert a[a.index("--model") + 1] == "gpt-5-codex"
    assert 'model_reasoning_effort="xhigh"' in a and a[-1] == "-"


SAMPLE = [
    {"id": "google/gemini-2.5-flash-image", "name": "Gemini Image",
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image", "text"]},
     "pricing": {"prompt": "0.0000003", "completion": "0.0000025", "image": "0.001238", "request": "0"}},
    {"id": "openai/gpt-4o-audio-preview", "name": "GPT-4o Audio",
     "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text", "audio"]},
     "pricing": {"prompt": "0.0000025", "completion": "0.00001", "audio": "0.00004"}},
    {"id": "mistralai/voxtral-small", "name": "Voxtral (transcreve)",
     "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openrouter/auto", "name": "Auto", "architecture": {"output_modalities": ["text"]},
     "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "ruim; rm -rf /", "name": "x", "pricing": {}},
]


def test_parse_openrouter_keeps_prices_and_modalities():
    out = {m["id"]: m for m in modelcfg.parse_openrouter(SAMPLE)}
    assert "ruim; rm -rf /" not in out
    img = out["google/gemini-2.5-flash-image"]
    assert img["kinds"] == ["text", "image"] and img["pricing"]["image"] == pytest.approx(0.001238)
    assert "request" not in img["pricing"]  # zero não aparece
    assert out["mistralai/voxtral-small"]["free"] is True
    assert out["openrouter/auto"]["pricing"] == {"prompt": None, "completion": None}  # variável
    models = list(out.values())
    assert [m["id"] for m in modelcfg.for_kind(models, "image")] == ["google/gemini-2.5-flash-image"]
    # áudio inclui quem gera e quem só entende (transcrição)
    assert {m["id"] for m in modelcfg.for_kind(models, "audio")} == {"openai/gpt-4o-audio-preview", "mistralai/voxtral-small"}
    assert modelcfg.for_kind(models, "video") == []


def test_openrouter_endpoint(logged, monkeypatch):
    async def fake():
        return {"live": True, "updated_at": 1.0, "models": modelcfg.parse_openrouter(SAMPLE)}

    monkeypatch.setattr(modelcfg, "openrouter_models", fake)
    data = logged.get("/api/models/openrouter?kind=image").json()
    assert data["live"] and [m["id"] for m in data["models"]] == ["google/gemini-2.5-flash-image"]


def test_openrouter_falls_back_without_network(monkeypatch):
    import httpx

    class Down:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise httpx.ConnectError("sem rede")

    monkeypatch.setattr(modelcfg, "_OR_CACHE", {"at": 0.0, "models": None})
    monkeypatch.setattr(modelcfg.httpx, "AsyncClient", Down)
    data = asyncio.run(modelcfg.openrouter_models())
    assert data["live"] is False and data["models"] and data["models"][0]["pricing"] == {}


def test_openrouter_key_is_cleaned_and_401_is_explained(monkeypatch):
    import asyncio
    import httpx
    from app.config import get_settings
    from app.providers import openrouter as orr

    monkeypatch.setenv("GOS_OPENROUTER_API_KEY", '  "sk-or-teste"  ')
    get_settings.cache_clear()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(401, json={"error": {"message": "User not found."}})

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

    monkeypatch.setattr(orr.httpx, "AsyncClient", FakeClient)
    try:
        with pytest.raises(orr.ProviderError, match="401"):
            asyncio.run(orr.OpenRouterProvider().complete([orr.ChatTurn("user", "oi")]))
        assert seen["auth"] == "Bearer sk-or-teste"
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def openrouter(monkeypatch):
    """OpenRouter falso: cada item da lista é uma resposta (ou exceção) na ordem."""
    import httpx
    from app.config import get_settings
    from app.providers import openrouter as orr

    monkeypatch.setenv("GOS_OPENROUTER_API_KEY", "sk-or-teste")
    get_settings.cache_clear()
    monkeypatch.setattr(orr, "RETRY_DELAYS", (0, 0))
    real_client = httpx.AsyncClient
    calls = []

    def setup(responses):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        class FakeClient(real_client):
            def __init__(self, *a, **kw):
                super().__init__(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

        monkeypatch.setattr(orr.httpx, "AsyncClient", FakeClient)
        return calls

    yield setup
    get_settings.cache_clear()


def _ask():
    import asyncio
    from app.providers import openrouter as orr

    return asyncio.run(orr.OpenRouterProvider().complete([orr.ChatTurn("user", "oi")]))


def _ok(text):
    import httpx

    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


def test_openrouter_retries_502_then_succeeds(openrouter):
    import httpx

    calls = openrouter([httpx.Response(502, json={"error": {"code": 502, "message": "Provider returned error"}}), _ok("oi!")])
    assert _ask() == "oi!" and len(calls) == 2


def test_openrouter_persistent_502_is_retryable_and_explained(openrouter):
    import httpx
    from app.providers.base import ProviderError

    bad = {"error": {"code": 502, "message": "Provider returned error", "metadata": {"provider_name": "X"}}}
    calls = openrouter([httpx.Response(502, json=bad) for _ in range(3)])
    with pytest.raises(ProviderError) as e:
        _ask()
    assert e.value.retryable and "502" in str(e.value) and "Tentar de novo" in str(e.value)
    assert len(calls) == 3
    assert "sk-or-teste" not in str(e.value)


def test_openrouter_error_inside_200_body_is_not_a_crash(openrouter):
    import httpx

    openrouter([httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}}), _ok("ok")])
    assert _ask() == "ok"


def test_openrouter_timeout_and_empty_content(openrouter):
    import httpx
    from app.providers.base import ProviderError

    openrouter([httpx.ReadTimeout("lento")])
    with pytest.raises(ProviderError, match="não respondeu") as e:
        _ask()
    assert e.value.retryable

    openrouter([httpx.Response(200, json={"choices": [{"message": {"content": None}, "finish_reason": "length"}]})])
    with pytest.raises(ProviderError, match="limite de saída"):
        _ask()


def test_openrouter_context_too_long_is_explained(openrouter):
    import httpx
    from app.providers.base import ProviderError

    openrouter([httpx.Response(400, json={"error": {"code": 400, "message": "This endpoint's maximum context length is 200000 tokens"}})])
    with pytest.raises(ProviderError, match="grande demais") as e:
        _ask()
    assert not e.value.retryable
