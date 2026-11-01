import asyncio
import json
from datetime import date, timedelta

import pytest

from app import db as dbmod
from app import growth

from .conftest import code
from .test_chat_and_commands import ScriptedProvider, sh

PLAN_JSON = json.dumps({"tarefas": [
    {"titulo": "Mapear o funil", "porque": "achar gargalo", "passos": "a; b", "risco": "baixo", "kpi": "cadastros"},
    {"titulo": "Teste A/B na landing", "porque": "conversão", "passos": "c", "risco": "médio", "kpi": ""},
]})


@pytest.fixture()
def fake(monkeypatch):
    """Todos os agentes respondem com o mesmo provider roteirizado."""
    holder = {}

    def _use(replies):
        p = ScriptedProvider(replies)
        holder["p"] = p
        monkeypatch.setattr(growth, "pick_provider", lambda agent=None: p)
        return p

    return _use


@pytest.fixture()
def submitted(monkeypatch):
    ids = []
    monkeypatch.setattr(growth.runner, "submit", ids.append)
    return ids


def only_agents(*slugs):
    s = dbmod.new_session()
    for a in s.query(dbmod.Agent).all():
        a.enabled = a.slug in slugs
    s.commit()
    s.close()


def test_seed_is_idempotent_and_keeps_edits(client):
    s = dbmod.new_session()
    assert s.query(dbmod.Agent).count() == 9
    a = s.query(dbmod.Agent).filter_by(slug="growth").one()
    a.mission = "missão editada pelo owner"
    s.commit()
    growth.seed_defaults(s)
    assert s.query(dbmod.Agent).count() == 9
    assert s.query(dbmod.Agent).filter_by(slug="growth").one().mission == "missão editada pelo owner"
    assert s.query(dbmod.Metric).filter_by(key="mrr").count() == 1
    s.close()


def test_parse_tasks_tolerates_noise():
    out = growth.parse_tasks(f"Claro! Aqui está:\n```json\n{PLAN_JSON}\n```")
    assert [t["title"] for t in out] == ["Mapear o funil", "Teste A/B na landing"]
    assert out[1]["risk"] == "medio"
    assert growth.parse_tasks("sem json nenhum") == []
    many = json.dumps({"tarefas": [{"titulo": f"t{i}"} for i in range(6)]})
    assert len(growth.parse_tasks(many)) == growth.MAX_TASKS_PER_AGENT
    assert growth.parse_tasks('[{"titulo": "lista solta", "risco": "xyz"}]')[0]["risk"] == "medio"


def test_pages_need_login(client):
    for path in ("/dashboard", "/plan", "/agents"):
        assert client.get(path, follow_redirects=False).status_code == 303
    assert client.get("/api/plans/today").status_code == 401


def test_generate_plan_and_decide(logged, fake, submitted):
    only_agents("growth", "tech")
    p = fake([PLAN_JSON, PLAN_JSON])
    plan_id = asyncio.run(growth.generate_plan())
    assert "MODO PLANEJAMENTO" in p.seen[0][0][1] and "cadastros" in p.seen[0][0][1]

    data = logged.get("/api/plans/today").json()
    assert data["plan"] == {"id": plan_id, "status": "ready", "error": ""}
    assert len(data["tasks"]) == 4
    ids = [t["id"] for t in data["tasks"]]

    r = logged.post("/api/tasks/decide", json={"ids": ids[:3], "approve": True}).json()
    assert len(r["changed"]) == 3 and submitted == ids[:3]
    logged.post("/api/tasks/decide", json={"ids": [ids[3]], "approve": False})
    # decidir de novo não muda nada
    assert logged.post("/api/tasks/decide", json={"ids": ids, "approve": True}).json()["changed"] == []
    statuses = [t["status"] for t in logged.get("/api/plans/today").json()["tasks"]]
    assert statuses == ["approved", "approved", "approved", "rejected"]
    actions = [a["action"] for a in logged.get("/api/audit").json()]
    assert "task_approved" in actions and "task_rejected" in actions

    # Rodar de novo no mesmo dia não duplica o plano.
    assert asyncio.run(growth.generate_plan()) == plan_id


def test_plan_fails_when_all_agents_fail(logged, monkeypatch):
    only_agents("growth")

    def boom(agent=None):
        raise growth.ProviderError("nenhum provider disponível para os agentes")

    monkeypatch.setattr(growth, "pick_provider", boom)
    asyncio.run(growth.generate_plan())
    plan = logged.get("/api/plans/today").json()["plan"]
    assert plan["status"] == "failed" and "nenhum provider" in plan["error"]


def _approved_task(title="Checar containers") -> int:
    s = dbmod.new_session()
    t = dbmod.Task(agent_slug="tech", title=title, status="approved", steps="rodar gos-docker ps")
    s.add(t)
    s.commit()
    tid = t.id
    s.close()
    return tid


def test_execute_task_runs_loop_and_ingests_kpis(logged, fake):
    fake([sh("echo ok"), 'Tudo no ar.\n```kpi\n{"uptime": 99.9, "chave_inexistente": 1}\n```'])
    tid = _approved_task()
    asyncio.run(growth.execute_task(tid))
    s = dbmod.new_session()
    t = s.get(dbmod.Task, tid)
    assert t.status == "done" and "Tudo no ar" in t.result and t.conversation_id
    conv = s.get(dbmod.Conversation, t.conversation_id)
    assert conv.title.endswith("Tech: Checar containers")
    first = s.query(dbmod.Message).filter_by(conversation_id=conv.id).order_by(dbmod.Message.id).first()
    assert "APROVOU esta tarefa" in first.content
    s.close()
    uptime = next(m for m in logged.get("/api/metrics").json() if m["key"] == "uptime")
    assert uptime["last"] == 99.9 and uptime["points"][0]["source"] == "agente:tech"


def test_execute_task_waits_for_2fa_then_finishes_from_chat(logged, fake, monkeypatch):
    p = fake([sh("rm -rf ./build"), "Limpei a pasta build."])
    monkeypatch.setattr("app.main.all_providers", lambda: {"fake": p})
    tid = _approved_task("Limpar build")
    asyncio.run(growth.execute_task(tid))
    s = dbmod.new_session()
    t = s.get(dbmod.Task, tid)
    assert t.status == "waiting"
    run = s.query(dbmod.CommandRun).filter_by(conversation_id=t.conversation_id, status="pending").one()
    cid = t.conversation_id
    s.close()

    assert logged.post(f"/api/commands/{run.id}/approve", json={"totp": code()}).status_code == 200
    logged.post(f"/api/conversations/{cid}/continue", json={"provider": "fake"})
    s = dbmod.new_session()
    t = s.get(dbmod.Task, tid)
    assert t.status == "done" and "Limpei" in t.result
    s.close()
    carry = logged.get("/api/plans/today").json()["carryover"]
    assert all(c["id"] != tid for c in carry)


def test_task_not_approved_never_runs(logged, fake):
    p = fake(["não devia rodar"])
    s = dbmod.new_session()
    t = dbmod.Task(agent_slug="tech", title="x", status="proposed")
    s.add(t)
    s.commit()
    tid = t.id
    s.close()
    asyncio.run(growth.execute_task(tid))
    assert p.seen == []


def test_adhoc_task_is_queued(logged, submitted):
    r = logged.post("/api/tasks", json={"agent": "social", "title": "Analisar concorrente X"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert submitted == [r.json()["id"]]
    assert logged.post("/api/tasks", json={"agent": "nao-existe", "title": "abc"}).status_code == 404
    carry = logged.get("/api/plans/today").json()["carryover"]
    assert [c["title"] for c in carry] == ["Analisar concorrente X"]


def test_metrics_points_and_targets(logged):
    today = growth.local_today()
    for i, v in enumerate([100, 120, 150]):
        day = (today - timedelta(days=2 - i)).isoformat()
        assert logged.post("/api/metrics/cadastros/points", json={"value": v, "day": day}).status_code == 200
    # mesmo dia substitui
    logged.post("/api/metrics/cadastros/points", json={"value": 160, "day": today.isoformat()})
    assert logged.post("/api/metrics/nao_existe/points", json={"value": 1}).status_code == 404
    assert logged.post("/api/metrics", json={"key": "cadastros", "name": "Cadastros", "area": "growth", "target": 200}).status_code == 200
    assert logged.post("/api/metrics", json={"key": "Bad Key", "name": "x"}).status_code == 422
    m = next(m for m in logged.get("/api/metrics").json() if m["key"] == "cadastros")
    assert (m["last"], m["prev"], m["target"], len(m["points"])) == (160, 120, 200, 3)
    s = dbmod.new_session()
    snap = growth.kpi_snapshot(s)
    s.close()
    assert "cadastros" in snap and "último 160" in snap and "SEM DADOS" in snap


def test_agent_update(logged):
    r = logged.patch("/api/agents/design", json={"enabled": False, "mission": "Foco total em landing pages que convertem."})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert logged.patch("/api/agents/design", json={"provider": "inexistente"}).status_code == 400
    assert logged.patch("/api/agents/nada", json={"enabled": True}).status_code == 404
    agents = {a["slug"]: a for a in logged.get("/api/agents").json()}
    assert agents["design"]["mission"].startswith("Foco total")


def test_notify_is_noop_without_telegram():
    assert asyncio.run(growth.notify("oi")) is False


def test_scheduler_waits_for_hour(client, monkeypatch):
    called = []

    async def fake_generate(day=None, actor=""):
        called.append(day)
        raise asyncio.CancelledError  # sai do laço depois da primeira tentativa

    monkeypatch.setattr(growth, "generate_plan", fake_generate)
    monkeypatch.setenv("GOS_PLAN_HOUR", "0")
    from app.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(growth.scheduler_loop())
    assert called and isinstance(called[0], date)
