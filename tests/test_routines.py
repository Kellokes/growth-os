import asyncio
from datetime import datetime

from app import db as dbmod
from app import growth, routines

from .test_growth import PLAN_JSON, fake, make_projects, only_agents, pid, submitted  # noqa: F401 - fixtures


def test_new_pages_need_login(client):
    for path in ("/board", "/automations"):
        assert client.get(path, follow_redirects=False).status_code == 303
    for path in ("/api/board", "/api/routines/catalog"):
        assert client.get(path).status_code == 401


def test_pages_render_with_nav(logged):
    for path, label in (("/board", "Kanban"), ("/automations", "Automações")):
        html = logged.get(path).text
        assert f"<h1 id=\"page-title\">{label}</h1>" in html
        assert 'href="/board"' in html and 'href="/automations"' in html


def test_skills_are_per_project_and_reach_the_right_agents(logged, fake):
    a, b = make_projects("Zombie Party", "A Ilha")
    r = logged.post(f"/api/gprojects/{a}/skills", json={
        "name": "Responder reviews", "description": "responde avaliações", "instructions": "1. ler 2. responder",
        "agents": ["cs"]})
    assert r.status_code == 200 and r.json()["agents"] == ["cs"]
    assert logged.post(f"/api/gprojects/{a}/skills", json={"name": "xx", "instructions": "y", "agents": ["nao-existe"]}).status_code == 400
    lib = logged.post(f"/api/gprojects/{a}/skills/template/seo_landing").json()
    assert lib["template"] == "seo_landing" and "growth" in lib["agents"]
    # Adicionar o mesmo modelo de novo não duplica.
    assert logged.post(f"/api/gprojects/{a}/skills/template/seo_landing").json()["id"] == lib["id"]
    assert len(logged.get(f"/api/gprojects/{a}/skills").json()) == 2
    assert logged.get(f"/api/gprojects/{b}/skills").json() == []

    s = dbmod.new_session()
    pa = s.get(dbmod.GrowthProject, pid(a))
    assert "Responder reviews" in routines.toolbox(s, pa, "cs")
    assert "Responder reviews" not in routines.toolbox(s, pa, "growth")
    assert "Auditoria de SEO" in routines.toolbox(s, pa, "growth")
    assert routines.toolbox(s, s.get(dbmod.GrowthProject, pid(b)), "growth") == ""
    s.close()

    # O plano do dia mostra ao agente as habilidades dele.
    only_agents("growth")
    p = fake([PLAN_JSON, PLAN_JSON])
    asyncio.run(growth.generate_plan())
    zombie = next(seen[0][1] for seen in p.seen if "**Zombie Party**" in seen[0][1])
    assert "Auditoria de SEO da landing" in zombie and "Responder reviews" not in zombie

    sid = r.json()["id"]
    assert logged.patch(f"/api/gprojects/{a}/skills/{sid}", json={"enabled": False}).json()["enabled"] is False
    assert logged.delete(f"/api/gprojects/{b}/skills/{sid}").status_code == 404  # não apaga de outro projeto
    assert logged.delete(f"/api/gprojects/{a}/skills/{sid}").status_code == 200


def test_integrations_never_store_secrets(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    items = {i["kind"]: i for i in logged.get(f"/api/gprojects/{a}/integrations").json()}
    assert items["umami"]["status"] == "off" and items["umami"]["secret_env"] == "GOS_INT_ZOMBIE_PARTY_UMAMI_API_KEY"
    r = logged.put(f"/api/gprojects/{a}/integrations/umami", json={"config": {
        "url": "https://analytics.zp.com", "website_id": "abc", "api_key": "segredo-que-nao-deve-ficar"}})
    assert r.status_code == 200
    assert r.json()["status"] == "needs_secret" and "api_key" not in r.json()["config"]
    s = dbmod.new_session()
    assert "segredo" not in s.query(dbmod.Integration).one().config
    s.close()
    monkeypatch.setenv("GOS_INT_ZOMBIE_PARTY_UMAMI_API_KEY", "valor-real")
    got = next(i for i in logged.get(f"/api/gprojects/{a}/integrations").json() if i["kind"] == "umami")
    assert got["status"] == "ready" and got["has_secret"] is True and "valor-real" not in str(got)
    # Uptime Kuma não pede chave: fica pronta só com a URL.
    kuma = logged.put(f"/api/gprojects/{a}/integrations/uptime_kuma", json={"config": {"url": "https://status.zp.com"}}).json()
    assert kuma["status"] == "ready" and kuma["secret_env"] == ""
    assert logged.put(f"/api/gprojects/{a}/integrations/nao_existe", json={"config": {}}).status_code == 404
    s = dbmod.new_session()
    box = routines.toolbox(s, s.get(dbmod.GrowthProject, pid(a)), "growth")
    assert "https://analytics.zp.com" in box and "valor-real" not in box
    s.close()
    actions = [x["detail"] for x in logged.get("/api/audit").json() if x["action"] == "integration_save"]
    assert actions and all("segredo" not in d for d in actions)


def _auto(logged, slug, **kw):
    body = {"agent": "tech", "title": "Checar a saúde dos sites", "frequency": "daily", "hour": 8, **kw}
    r = logged.post(f"/api/gprojects/{slug}/automations", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_automation_schedule_and_runs(logged, submitted):
    a, b = make_projects("Zombie Party", "A Ilha")
    daily = _auto(logged, a, skill_template="saude_containers")
    assert daily["schedule"] == "Todo dia às 8h" and daily["skill"] == "Checagem de saúde dos containers"
    weekly = _auto(logged, a, title="Relatório", agent="growth", frequency="weekly", weekday=0, hour=9, auto_approve=True)
    assert weekly["schedule"] == "Toda segunda às 9h"
    assert logged.get(f"/api/gprojects/{b}/automations").json() == []
    assert logged.post(f"/api/gprojects/{a}/automations", json={"agent": "x", "title": "abc"}).status_code == 404

    # Reseta o "já rodou hoje" que a criação marca quando a hora já passou.
    s = dbmod.new_session()
    s.query(dbmod.Automation).update({dbmod.Automation.last_run_on: None})
    s.commit()
    monday = datetime(2026, 11, 2, 10, 0)  # segunda, 10h
    created = routines.run_due(s, monday)
    assert sorted(t.title for t in created) == ["Checar a saúde dos sites", "Relatório"]
    by_title = {t.title: t for t in created}
    assert by_title["Checar a saúde dos sites"].status == "proposed"  # padrão: espera aprovação
    assert by_title["Relatório"].status == "approved"  # o owner liberou
    # Mesma hora de novo: não duplica. Dia seguinte com a anterior ainda aberta: não empilha.
    assert routines.run_due(s, monday) == []
    tuesday = datetime(2026, 11, 3, 10, 0)
    assert routines.run_due(s, tuesday) == []
    # Fechou a anterior: no dia seguinte roda de novo (a semanal não, porque é só segunda).
    for t in s.query(dbmod.Task).all():
        t.status = "done"
    s.commit()
    assert [t.title for t in routines.run_due(s, datetime(2026, 11, 4, 9, 0))] == ["Checar a saúde dos sites"]
    assert routines.run_due(s, datetime(2026, 11, 5, 7, 0)) == []  # antes da hora
    s.close()

    # Rodar agora: já nasce aprovado e vai para a fila; com uma execução aberta, recusa.
    s = dbmod.new_session()
    for t in s.query(dbmod.Task).all():
        t.status = "done"
    s.commit()
    s.close()
    r = logged.post(f"/api/gprojects/{a}/automations/{daily['id']}/run").json()
    assert r["status"] == "approved" and r["origin"] == "automation" and submitted[-1] == r["id"]
    assert logged.post(f"/api/gprojects/{a}/automations/{daily['id']}/run").status_code == 409
    assert logged.post(f"/api/gprojects/{b}/automations/{daily['id']}/run").status_code == 404

    up = logged.patch(f"/api/gprojects/{a}/automations/{daily['id']}", json={"enabled": False}).json()
    assert up["enabled"] is False and up["next_run"] == ""
    assert logged.delete(f"/api/gprojects/{a}/automations/{weekly['id']}").status_code == 200


def test_automation_task_brief_follows_its_skill(logged, fake):
    (a,) = make_projects("Zombie Party")
    auto = _auto(logged, a, skill_template="saude_containers")
    s = dbmod.new_session()
    t = routines.create_run(s, s.get(dbmod.Automation, auto["id"]), approved=True, actor="teste")
    tid = t.id
    s.close()
    fake(["Tudo certo."])
    asyncio.run(growth.execute_task(tid))
    s = dbmod.new_session()
    t = s.get(dbmod.Task, tid)
    first = s.query(dbmod.Message).filter_by(conversation_id=t.conversation_id).order_by(dbmod.Message.id).first()
    assert "atividade rotineira" in first.content and "Checagem de saúde dos containers" in first.content
    assert t.status == "done"
    s.close()


def test_scheduler_skips_disabled_agent_or_project(logged):
    (a,) = make_projects("Zombie Party")
    _auto(logged, a)
    s = dbmod.new_session()
    s.query(dbmod.Automation).update({dbmod.Automation.last_run_on: None})
    s.query(dbmod.ProjectAgent).filter_by(agent_slug="tech").update({dbmod.ProjectAgent.enabled: False})
    s.commit()
    assert routines.run_due(s, datetime(2026, 11, 2, 10, 0)) == []
    s.close()


def test_board_columns_and_retry(logged, submitted):
    a, b = make_projects("Zombie Party", "A Ilha")
    s = dbmod.new_session()
    rows = [dbmod.Task(project_id=pid(a), agent_slug="growth", title=f"t-{st}", status=st)
            for st in ("proposed", "approved", "running", "waiting", "done", "failed", "rejected")]
    rows.append(dbmod.Task(project_id=pid(b), agent_slug="tech", title="de outro projeto", status="proposed"))
    old = dbmod.Task(project_id=pid(a), agent_slug="growth", title="velha", status="done",
                     finished_at=datetime(2020, 1, 1), created_at=datetime(2020, 1, 1))
    s.add_all(rows + [old])
    s.commit()
    failed_id = next(t.id for t in rows if t.status == "failed")
    s.close()

    data = logged.get(f"/api/board?project={a}").json()
    titles = {t["title"] for t in data["tasks"]}
    assert "de outro projeto" not in titles and "velha" not in titles
    assert {t["status"] for t in data["tasks"]} == {"proposed", "approved", "running", "waiting", "done", "failed", "rejected"}
    assert all(t["origin"] == "owner" for t in data["tasks"])
    assert len(logged.get("/api/board?project=all").json()["tasks"]) == 8
    assert logged.get("/api/board?project=nao-existe").status_code == 404

    r = logged.post(f"/api/tasks/{failed_id}/retry")
    assert r.status_code == 200 and r.json()["status"] == "approved" and submitted == [failed_id]
    assert logged.post(f"/api/tasks/{failed_id}/retry").status_code == 409


def test_runner_accepts_tasks_from_sync_routes(client, monkeypatch):
    """Rotas síncronas rodam fora do event loop: submit precisa entrar na fila sem RuntimeError."""
    ran = []

    async def fake_execute(tid):
        ran.append(tid)

    monkeypatch.setattr(growth, "execute_task", fake_execute)
    import threading
    import time

    errors = []
    th = threading.Thread(target=lambda: _try(lambda: growth.runner.submit(4242), errors))
    th.start()
    th.join()
    for _ in range(50):
        if ran:
            break
        client.get("/healthz")  # dá voltas no loop do app
        time.sleep(0.02)
    assert errors == [] and ran == [4242]


def _try(fn, errors):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        errors.append(e)


def test_sidebar_menu_has_sections_then_threads_on_every_page(logged):
    for path in ("/", "/dashboard", "/plan", "/board", "/agents", "/automations", "/projects", "/settings"):
        html = logged.get(path).text
        side = html.split('id="sidenav"')[1].split("</aside>")[0]
        # primeiro as seções do menu, depois a lista de threads, e no rodapé auditoria/tema/sair
        assert side.index('class="nav"') < side.index('id="conv-list"') < side.index('id="logout"')
        assert 'href="/settings"' in side and 'href="/automations"' in side
        assert html.count('id="conv-list"') == 1
