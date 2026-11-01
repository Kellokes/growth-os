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
