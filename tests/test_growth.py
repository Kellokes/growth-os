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


def make_projects(*names):
    s = dbmod.new_session()
    out = [growth.create_project(s, n).slug for n in names]
    s.close()
    return out


def pid(slug):
    s = dbmod.new_session()
    i = s.query(dbmod.GrowthProject).filter_by(slug=slug).one().id
    s.close()
    return i


@pytest.fixture()
def fake(monkeypatch):
    """Todos os agentes respondem com o mesmo provider roteirizado."""
    holder = {}

    def _use(replies):
        p = ScriptedProvider(replies)
        holder["p"] = p
        monkeypatch.setattr(growth, "pick_provider", lambda pa=None: p)
        return p

    return _use


@pytest.fixture()
def submitted(monkeypatch):
    ids = []
    monkeypatch.setattr(growth.runner, "submit", ids.append)
    return ids


def only_agents(*slugs):
    s = dbmod.new_session()
    for pa in s.query(dbmod.ProjectAgent).all():
        pa.enabled = pa.agent_slug in slugs
    s.commit()
    s.close()


def test_each_project_gets_its_own_team_and_kpis(client):
    a, b = make_projects("Zombie Party", "A Ilha")
    s = dbmod.new_session()
    assert s.query(dbmod.Agent).count() == 9
    for slug in (a, b):
        p = s.query(dbmod.GrowthProject).filter_by(slug=slug).one()
        assert s.query(dbmod.ProjectAgent).filter_by(project_id=p.id).count() == 9
        assert s.query(dbmod.Metric).filter_by(project_id=p.id, key="mrr").count() == 1
    pa = s.query(dbmod.ProjectAgent).filter_by(project_id=pid(a), agent_slug="growth").one()
    pa.mission = "missão do zombie"
    s.commit()
    growth.seed_defaults(s)  # idempotente e não sobrescreve a missão do projeto
    assert s.query(dbmod.ProjectAgent).count() == 18
    assert s.query(dbmod.ProjectAgent).filter_by(project_id=pid(a), agent_slug="growth").one().mission == "missão do zombie"
    s.close()


def test_import_projects_from_vps(client):
    containers = [
        {"name": "zombie-party-app-1", "project": "zombie-party", "service": "app", "state": "running", "health": ""},
        {"name": "growth-os", "project": "growth-os", "service": "growth-os", "state": "running", "health": ""},
        {"name": "solto", "project": "", "service": "", "state": "running", "health": ""},
    ]
    sites = [{"hosts": ["zombieparty.com.br"], "upstream": "zombie-party-app-1:3000", "redirect": ""}]
    s = dbmod.new_session()
    assert growth.import_projects(s, containers, sites) == ["zombie-party"]
    p = s.query(dbmod.GrowthProject).one()
    assert (p.name, p.compose_project, p.domains) == ("Zombie Party", "zombie-party", "zombieparty.com.br")
    assert growth.import_projects(s, containers, sites) == []  # não duplica
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


def test_generate_plan_per_project_and_decide(logged, fake, submitted):
    a, b = make_projects("Zombie Party", "A Ilha")
    only_agents("growth")
    s = dbmod.new_session()
    s.query(dbmod.GrowthProject).filter_by(slug=b).one().description = "jogo de sobrevivência"
    s.commit()
    s.close()
    growth_metric = logged.post(f"/api/gprojects/{a}/metrics/cadastros/points", json={"value": 42})
    assert growth_metric.status_code == 200
    p = fake([PLAN_JSON, PLAN_JSON])
    plan_id = asyncio.run(growth.generate_plan())
    prompts = [seen[0][1] for seen in p.seen]
    assert all("MODO PLANEJAMENTO" in x for x in prompts)
    zombie = next(x for x in prompts if "**Zombie Party**" in x)
    ilha = next(x for x in prompts if "**A Ilha**" in x)
    assert "último 42" in zombie and "último 42" not in ilha  # cada agente só vê os KPIs do seu projeto
    assert "jogo de sobrevivência" in ilha

    data = logged.get("/api/plans/today").json()
    assert data["plan"] == {"id": plan_id, "status": "ready", "error": ""}
    assert len(data["tasks"]) == 4
    assert sorted({t["project"] for t in data["tasks"]}) == sorted([a, b])
    pending = {x["slug"]: x["pending"] for x in logged.get("/api/gprojects").json()}
    assert pending == {a: 2, b: 2}
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
    make_projects("Zombie Party")
    only_agents("growth")

    def boom(pa=None):
        raise growth.ProviderError("nenhum provider disponível para os agentes")

    monkeypatch.setattr(growth, "pick_provider", boom)
    asyncio.run(growth.generate_plan())
    plan = logged.get("/api/plans/today").json()["plan"]
    assert plan["status"] == "failed" and "nenhum provider" in plan["error"]


def test_plan_without_projects_fails_clearly(logged):
    asyncio.run(growth.generate_plan())
    plan = logged.get("/api/plans/today").json()["plan"]
    assert plan["status"] == "failed" and "nenhum projeto" in plan["error"]


def _approved_task(title="Checar containers", project="zombie-party") -> int:
    s = dbmod.new_session()
    if not s.query(dbmod.GrowthProject).filter_by(slug=project).count():
        growth.create_project(s, project.replace("-", " ").title(), slug=project, compose_project=project)
    t = dbmod.Task(project_id=s.query(dbmod.GrowthProject).filter_by(slug=project).one().id,
                   agent_slug="tech", title=title, status="approved", steps="rodar gos-docker ps")
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
    assert conv.title.endswith("Zombie Party · Tech: Checar containers")
    first = s.query(dbmod.Message).filter_by(conversation_id=conv.id).order_by(dbmod.Message.id).first()
    assert "APROVOU esta tarefa" in first.content and "gos-docker ps zombie-party" in first.content
    s.close()
    uptime = next(m for m in logged.get("/api/gprojects/zombie-party/metrics").json() if m["key"] == "uptime")
    assert uptime["last"] == 99.9 and uptime["points"][0]["source"] == "agente:tech"
    make_projects("Outro")
    other = next(m for m in logged.get("/api/gprojects/outro/metrics").json() if m["key"] == "uptime")
    assert other["last"] is None  # KPI reportado num projeto não vaza para outro


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
    make_projects("Zombie Party")
    s = dbmod.new_session()
    t = dbmod.Task(project_id=pid("zombie-party"), agent_slug="tech", title="x", status="proposed")
    s.add(t)
    s.commit()
    tid = t.id
    s.close()
    asyncio.run(growth.execute_task(tid))
    assert p.seen == []


def test_adhoc_task_is_queued(logged, submitted):
    (a,) = make_projects("Zombie Party")
    r = logged.post("/api/tasks", json={"project": a, "agent": "social", "title": "Analisar concorrente X"})
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["project"] == a
    assert submitted == [r.json()["id"]]
    assert logged.post("/api/tasks", json={"project": a, "agent": "nao-existe", "title": "abc"}).status_code == 404
    assert logged.post("/api/tasks", json={"project": "nada", "agent": "social", "title": "abc"}).status_code == 404
    carry = logged.get("/api/plans/today").json()["carryover"]
    assert [c["title"] for c in carry] == ["Analisar concorrente X"]
    assert [t["title"] for t in logged.get(f"/api/tasks?project={a}").json()] == ["Analisar concorrente X"]


def test_metrics_points_and_targets(logged):
    (a,) = make_projects("Zombie Party")
    base = f"/api/gprojects/{a}/metrics"
    today = growth.local_today()
    for i, v in enumerate([100, 120, 150]):
        day = (today - timedelta(days=2 - i)).isoformat()
        assert logged.post(f"{base}/cadastros/points", json={"value": v, "day": day}).status_code == 200
    # mesmo dia substitui
    logged.post(f"{base}/cadastros/points", json={"value": 160, "day": today.isoformat()})
    assert logged.post(f"{base}/nao_existe/points", json={"value": 1}).status_code == 404
    assert logged.post("/api/gprojects/nada/metrics/cadastros/points", json={"value": 1}).status_code == 404
    assert logged.post(base, json={"key": "cadastros", "name": "Cadastros", "area": "growth", "target": 200}).status_code == 200
    assert logged.post(base, json={"key": "Bad Key", "name": "x"}).status_code == 422
    m = next(m for m in logged.get(base).json() if m["key"] == "cadastros")
    assert (m["last"], m["prev"], m["target"], len(m["points"])) == (160, 120, 200, 3)
    s = dbmod.new_session()
    snap = growth.kpi_snapshot(s, pid(a))
    s.close()
    assert "cadastros" in snap and "último 160" in snap and "SEM DADOS" in snap


def test_project_agent_update_is_per_project(logged):
    a, b = make_projects("Zombie Party", "A Ilha")
    url = f"/api/gprojects/{a}/agents/design"
    r = logged.patch(url, json={"enabled": False, "mission": "Foco total em landing pages que convertem."})
    assert r.status_code == 200
    assert logged.patch(url, json={"provider": "inexistente"}).status_code == 400
    assert logged.patch(f"/api/gprojects/{a}/agents/nada", json={"enabled": True}).status_code == 404
    mine = {x["slug"]: x for x in logged.get(f"/api/gprojects/{a}/agents").json()}
    other = {x["slug"]: x for x in logged.get(f"/api/gprojects/{b}/agents").json()}
    assert mine["design"]["mission"].startswith("Foco total") and mine["design"]["custom"] and not mine["design"]["enabled"]
    assert other["design"]["enabled"] and not other["design"]["custom"]
    # mandar a missão padrão de volta tira a personalização
    logged.patch(url, json={"mission": mine["design"]["default_mission"]})
    assert not {x["slug"]: x for x in logged.get(f"/api/gprojects/{a}/agents").json()}["design"]["custom"]


def test_project_crud(logged):
    r = logged.post("/api/gprojects", json={"name": "Novo Negócio", "description": "SaaS de agenda"})
    assert r.status_code == 200 and r.json()["slug"] == "novo-negocio"
    assert logged.post("/api/gprojects", json={"name": "Novo Negócio"}).status_code == 409
    r = logged.patch("/api/gprojects/novo-negocio", json={"repo": "kellokes/agenda", "domains": "agenda.com"})
    assert r.json()["repo"] == "kellokes/agenda"
    assert logged.patch("/api/gprojects/novo-negocio", json={"repo": "não é repo"}).status_code == 422
    assert len(logged.get("/api/gprojects/novo-negocio/agents").json()) == 9


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
