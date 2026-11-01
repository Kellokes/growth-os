import asyncio
import json

import httpx
import pytest

from app import actions as acts
from app import db as dbmod
from app import growth, routines
from app import main as appmain
from app.connectors import REGISTRY, Connector, ConnectorError, Ctx, Op, safe_base_url
from app.executor import classify

from .test_chat_and_commands import ScriptedProvider, sh
from .test_growth import _approved_task, fake, make_projects, pid  # noqa: F401 - fixtures

UMAMI = {"url": "https://analytics.exemplo.com", "website_id": "abc"}


def mock(handler):
    return httpx.MockTransport(handler)


def connect(slug, kind, config, monkeypatch=None, secret="chave-secreta"):
    s = dbmod.new_session()
    p = s.query(dbmod.GrowthProject).filter_by(slug=slug).one()
    routines.save_integration(s, p, kind, config, True)
    env = routines.secret_env_name(p, kind)
    s.close()
    if monkeypatch and env:
        monkeypatch.setenv(env, secret)


def ticket_for(slug, agent="growth"):
    """Ticket de uma tarefa real (no Postgres a chave estrangeira de actions.task_id exige que ela exista)."""
    s = dbmod.new_session()
    t = dbmod.Task(project_id=pid(slug), agent_slug=agent, title="Tarefa do ticket", status="running")
    s.add(t)
    s.commit()
    tid = t.id
    s.close()
    return acts.Ticket(tid, pid(slug), agent, 0)


def run(slug, kind, op, params=None, transport=None):
    s = dbmod.new_session()
    try:
        return asyncio.run(acts.run_operation(s, ticket_for(slug), kind, op, params or {}, transport))
    finally:
        s.close()


def test_safe_base_url_blocks_internal_targets():
    assert safe_base_url("https://analytics.exemplo.com/", resolve=False) == "https://analytics.exemplo.com"
    for bad in ("http://localhost:8000", "http://127.0.0.1", "http://169.254.169.254/latest", "http://docker-gateway:2375",
                "ftp://x.com", "https://user:pw@x.com", "https://[::1]/", "sem-esquema.com"):
        with pytest.raises(ConnectorError):
            safe_base_url(bad, resolve=False)


def test_umami_read_uses_secret_and_returns_numbers(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "umami", UMAMI, monkeypatch)
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"], seen["path"] = req.headers.get("authorization"), req.url.path
        return httpx.Response(200, json={"visitors": {"value": 120}, "visits": {"value": 150}, "pageviews": {"value": 400},
                                         "bounces": {"value": 30}})

    res = run(a, "umami", "ler_visitas", {"dias": 3}, mock(handler))
    assert res["status"] == "done" and res["result"]["visitantes"] == 120 and res["result"]["dias"] == 3
    assert seen == {"auth": "Bearer chave-secreta", "path": "/api/websites/abc/stats"}
    s = dbmod.new_session()
    act = s.query(dbmod.Action).one()
    assert (act.status, act.effect, act.integration) == ("done", "read", "umami")
    assert "chave-secreta" not in act.params + act.result  # a chave nunca é gravada
    assert s.query(dbmod.Event).filter_by(type="action.done").count() == 1
    s.close()


def test_other_readers(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "plausible", {"url": "https://p.exemplo.com", "site_id": "exemplo.com"}, monkeypatch)
    connect(a, "uptime_kuma", {"url": "https://status.exemplo.com", "status_page": "app"}, monkeypatch)
    connect(a, "project_api", {"base_url": "https://api.exemplo.com"}, monkeypatch)

    def handler(req: httpx.Request):
        if "stats/aggregate" in req.url.path:
            return httpx.Response(200, json={"results": {"visitors": {"value": 9}, "pageviews": {"value": 20},
                                                         "bounce_rate": {"value": 40}, "visit_duration": {"value": 61}}})
        if "heartbeat" in req.url.path:
            return httpx.Response(200, json={"heartbeatList": {"1": [{"status": 1, "time": "t"}], "2": [{"status": 0, "time": "t"}]},
                                             "uptimeList": {"1_24": 1, "2_24": 0.5}})
        return httpx.Response(200, json={"usuarios": 42})

    assert run(a, "plausible", "ler_visitas", transport=mock(handler))["result"]["visitantes"] == 9
    st = run(a, "uptime_kuma", "ler_status", transport=mock(handler))["result"]
    assert st["fora_do_ar"] == ["2"] and st["uptime_24h_medio"] == 75.0
    assert run(a, "project_api", "get", {"caminho": "/stats"}, mock(handler))["result"]["dados"] == {"usuarios": 42}
    for bad in ("stats", "/../etc", "//evil.com/x", "/a@b"):
        r = run(a, "project_api", "get", {"caminho": bad}, mock(handler))
        assert r["status"] == "error"


def test_errors_are_reported_not_raised(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    assert "não está pronta" in run(a, "umami", "ler_visitas")["message"]  # não ligada
    connect(a, "umami", UMAMI, monkeypatch)
    assert "desconhecida" in run(a, "umami", "apagar_tudo")["message"]
    assert "desconhecidos" in run(a, "umami", "ler_visitas", {"x": 1})["message"]
    r = run(a, "umami", "ler_visitas", transport=mock(lambda req: httpx.Response(401)))
    assert r["status"] == "error" and "recusou a chave" in r["message"]
    s = dbmod.new_session()
    assert s.query(dbmod.Action).filter_by(status="failed").count() == 1
    assert s.query(dbmod.Event).filter_by(type="action.failed").count() == 1
    s.close()


def test_integration_without_secret_is_not_ready(logged):
    (a,) = make_projects("Zombie Party")
    connect(a, "umami", UMAMI)  # sem a chave no ambiente
    assert "não está pronta" in run(a, "umami", "ler_visitas")["message"]


def test_integrations_are_scoped_to_the_ticket_project(logged, monkeypatch):
    a, b = make_projects("Zombie Party", "A Ilha")
    connect(a, "umami", UMAMI, monkeypatch)
    assert run(a, "umami", "ler_visitas", transport=mock(lambda r: httpx.Response(200, json={})))["status"] == "done"
    assert run(b, "umami", "ler_visitas")["status"] == "error"  # o outro projeto não tem a integração


class Publisher(Connector):
    kind = "postiz"
    called = []
    ops = {"publicar_post": Op("external", "publica", {"texto": (str, True)}, risk="alto")}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        Publisher.called.append(params)
        return {"ok": True}


def test_external_operation_waits_for_approval_and_never_runs(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "postiz", {"url": "https://postiz.exemplo.com"}, monkeypatch)
    monkeypatch.setitem(REGISTRY, "postiz", Publisher())
    Publisher.called.clear()
    r = run(a, "postiz", "publicar_post", {"texto": "Olá mundo"})
    assert r["status"] == "awaiting_approval" and Publisher.called == []
    s = dbmod.new_session()
    act = s.get(dbmod.Action, r["action_id"])
    assert act.status == "awaiting_approval" and act.risk == "alto" and json.loads(act.params) == {"texto": "Olá mundo"}
    s.close()
    cards = logged.get("/api/approvals?kind=action").json()["items"]
    assert len(cards) == 1 and cards[0]["needs_2fa"] and "Olá mundo" in cards[0]["preview"]
    assert run(a, "postiz", "publicar_post", {})["status"] == "error"  # parâmetro obrigatório


def test_internal_endpoint_is_loopback_only_and_ticket_bound(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "umami", UMAMI, monkeypatch)
    tid = _approved_task("Ler visitas")
    s = dbmod.new_session()
    task = s.get(dbmod.Task, tid)
    task.conversation_id = None
    ticket = acts.issue_ticket(task)
    s.close()
    body = {"ticket": ticket, "integracao": "umami", "operacao": "ler_visitas", "parametros": {}}
    # De fora (o cliente de teste não é loopback): some como se a rota não existisse.
    assert logged.post("/internal/actions", json=body).status_code == 404
    monkeypatch.setattr(appmain, "INTERNAL_HOSTS", {"testclient"})
    r = logged.post("/internal/actions", json={**body, "ticket": "errado"})
    assert r.status_code == 403
    # Atrás do Caddy chega com X-Forwarded-For: recusa mesmo com ticket válido.
    assert logged.post("/internal/actions", json=body, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404
    cat = logged.post("/internal/integrations", json={"ticket": ticket}).json()["integracoes"]
    assert [c["integracao"] for c in cat] == ["umami"]
    acts.revoke_ticket(ticket)
    assert logged.post("/internal/actions", json=body).status_code == 403  # tarefa terminou


def test_task_gets_a_ticket_only_while_it_runs_and_commands_receive_it(logged, fake, monkeypatch):
    monkeypatch.setenv("GOS_SECRET_KEY", "segredo-do-app")
    (a,) = make_projects("Zombie Party")
    p = fake([sh('echo "T=$TASK_TICKET G=$GOS_SECRET_KEY"'), "Pronto."])
    seen = []
    orig = p.complete

    async def spy(history):
        seen.append(len(acts._TICKETS))
        return await orig(history)

    p.complete = spy
    tid = _approved_task("Ticket")
    asyncio.run(growth.execute_task(tid))
    assert seen[0] == 1 and acts._TICKETS == {}  # nasce com a tarefa e morre com ela
    s = dbmod.new_session()
    out = s.query(dbmod.CommandRun).one().output
    token = out.split("T=")[1].split(" ")[0]
    assert len(token) > 10
    assert "segredo-do-app" not in out  # variáveis GOS_ continuam fora dos comandos
    s.close()


def test_plain_chat_has_no_ticket(logged, monkeypatch):
    p = ScriptedProvider([sh('echo "T=[$TASK_TICKET]"'), "ok"])
    monkeypatch.setattr("app.main.all_providers", lambda: {"fake": p})
    cid = logged.post("/api/conversations").json()["id"]
    logged.post(f"/api/conversations/{cid}/messages", json={"provider": "fake", "content": "oi"})
    s = dbmod.new_session()
    assert "T=[]" in s.query(dbmod.CommandRun).one().output
    s.close()


def test_gos_int_runs_without_approval_and_prompt_lists_operations(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    assert classify("gos-int list").risk == "safe"
    assert classify("gos-int run umami ler_visitas '{\"dias\": 7}'").risk in ("safe", "auto")
    connect(a, "umami", UMAMI, monkeypatch)
    s = dbmod.new_session()
    box = routines.toolbox(s, s.query(dbmod.GrowthProject).one(), "growth")
    assert "gos-int run" in box and "umami ler_visitas(dias?: int) [read]" in box and "chave-secreta" not in box
    s.close()


def test_daily_kpi_collection_without_ai(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "umami", UMAMI, monkeypatch)
    connect(a, "uptime_kuma", {"url": "https://status.exemplo.com", "status_page": "app"}, monkeypatch)

    def handler(req: httpx.Request):
        if "heartbeat" in req.url.path:
            return httpx.Response(200, json={"heartbeatList": {"1": [{"status": 1}]}, "uptimeList": {"1_24": 0.99}})
        return httpx.Response(200, json={"visitors": {"value": 77}})

    s = dbmod.new_session()
    done = asyncio.run(acts.collect_kpis(s, mock(handler)))
    assert sorted(done) == ["zombie-party/uptime", "zombie-party/visitantes"]
    series = {m["key"]: m for m in growth.metric_series(s, pid(a), 7)}
    assert series["visitantes"]["points"][-1]["value"] == 77 and series["uptime"]["points"][-1]["value"] == 99.0
    assert s.query(dbmod.MetricPoint).filter(dbmod.MetricPoint.source.like("integração:%")).count() == 2
    assert s.query(dbmod.Action).filter(dbmod.Action.task_id.isnot(None)).count() == 0  # coleta não é de nenhuma tarefa
    s.close()
