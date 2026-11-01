import json

from app import db as dbmod
from app import growth

from .conftest import code
from .test_growth import make_projects, pid  # noqa: F401


def _task(project: str, title="Fazer algo", status="proposed", risk="baixo") -> int:
    s = dbmod.new_session()
    t = dbmod.Task(project_id=pid(project), agent_slug="growth", title=title, status=status, risk=risk,
                   steps="1. passo", rationale="porque sim")
    s.add(t)
    s.commit()
    tid = t.id
    s.close()
    return tid


def _content(project: str) -> int:
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid(project), agent_slug="social", channel="instagram", title="Post",
                          body="texto", status="pending")
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def _mission(project: str) -> int:
    s = dbmod.new_session()
    m = dbmod.Mission(project_id=pid(project), title="Campanha de Natal", goal="vender", origin="chat")
    s.add(m)
    s.commit()
    mid = m.id
    s.close()
    return mid


def _action(project: str, risk="alto") -> int:
    s = dbmod.new_session()
    a = dbmod.Action(project_id=pid(project), integration="postiz", operation="publicar_post", effect="external",
                     risk=risk, params=json.dumps({"texto": "oi"}), status="awaiting_approval",
                     idempotency_key=f"k{risk}{dbmod.utcnow().timestamp()}")
    s.add(a)
    s.commit()
    aid = a.id
    s.close()
    return aid


def test_approvals_need_login(client):
    for path in ("/api/approvals", "/api/approvals/history"):
        assert client.get(path).status_code == 401
    assert client.post("/api/approvals/decide", json={"items": [{"kind": "task", "ref_id": 1}], "approve": True}).status_code == 401


def test_queue_gathers_every_kind_and_is_per_project(logged):
    a, b = make_projects("Zombie Party", "A Ilha")
    _task(a)
    _task(b, "Outra")
    _content(a)
    _mission(a)
    _action(a)
    _task(a, "já aprovada", status="approved")  # não aparece
    data = logged.get("/api/approvals").json()
    assert data["counts"] == {"task": 2, "mission": 1, "content": 1, "action": 1, "total": 5}
    mine = logged.get(f"/api/approvals?project={a}").json()
    assert mine["counts"]["total"] == 4 and {i["project"] for i in mine["items"]} == {a}
    assert mine["items"][0]["kind"] == "action" and mine["items"][0]["needs_2fa"] is True  # risco alto primeiro
    only = logged.get(f"/api/approvals?project={a}&kind=content").json()["items"]
    assert [i["kind"] for i in only] == ["content"] and only[0]["origin"] == "conteudo"
    assert logged.get("/api/approvals?kind=nada").status_code == 422


def test_decide_task_and_content_records_history(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    submitted = []
    monkeypatch.setattr(growth.runner, "submit", lambda tid: submitted.append(tid))
    t1, t2, c = _task(a, "Aprovar"), _task(a, "Rejeitar"), _content(a)
    r = logged.post("/api/approvals/decide", json={"items": [{"kind": "task", "ref_id": t1}, {"kind": "content", "ref_id": c}],
                                                    "approve": True})
    assert r.status_code == 200 and len(r.json()["decided"]) == 2
    assert submitted == [t1]  # só a tarefa vai para a fila
    assert logged.post("/api/approvals/decide", json={"items": [{"kind": "task", "ref_id": t2}], "approve": False}).status_code == 200
    s = dbmod.new_session()
    assert s.get(dbmod.Task, t1).status == "approved" and s.get(dbmod.Task, t2).status == "rejected"
    assert s.get(dbmod.ContentItem, c).status == "approved"
    assert s.query(dbmod.Event).filter_by(type="approval.approved").count() == 2
    s.close()
    hist = logged.get("/api/approvals/history").json()
    assert {(h["kind"], h["status"]) for h in hist} == {("task", "approved"), ("content", "approved"), ("task", "rejected")}
    # Decidir de novo não duplica nem reexecuta.
    again = logged.post("/api/approvals/decide", json={"items": [{"kind": "task", "ref_id": t1}], "approve": True})
    assert again.status_code == 409 and submitted == [t1]
    assert logged.get("/api/approvals").json()["counts"]["total"] == 0


def test_legacy_endpoints_share_the_same_history(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    monkeypatch.setattr(growth.runner, "submit", lambda tid: None)
    t, c = _task(a), _content(a)
    assert logged.post("/api/tasks/decide", json={"ids": [t], "approve": True}).status_code == 200
    assert logged.post(f"/api/content/{c}/decide", json={"approve": False, "note": "fraco"}).status_code == 200
    hist = {(h["kind"], h["status"]) for h in logged.get("/api/approvals/history").json()}
    assert hist == {("task", "approved"), ("content", "rejected")}


def test_mission_approval_activates_it(logged):
    (a,) = make_projects("Zombie Party")
    m = _mission(a)
    assert logged.post("/api/approvals/decide", json={"items": [{"kind": "mission", "ref_id": m}], "approve": True}).status_code == 200
    s = dbmod.new_session()
    assert s.get(dbmod.Mission, m).status == "active"
    s.close()


def test_external_action_needs_2fa_to_approve_but_not_to_reject(logged):
    (a,) = make_projects("Zombie Party")
    act = _action(a)
    item = {"items": [{"kind": "action", "ref_id": act}]}
    assert logged.post("/api/approvals/decide", json={**item, "approve": True}).status_code == 403
    assert logged.post("/api/approvals/decide", json={**item, "approve": True, "totp": "000000"}).status_code == 403
    s = dbmod.new_session()
    assert s.get(dbmod.Action, act).status == "awaiting_approval"
    s.close()
    ok = logged.post("/api/approvals/decide", json={**item, "approve": True, "totp": code()})
    assert ok.status_code == 200
    s = dbmod.new_session()
    assert s.get(dbmod.Action, act).status == "approved"
    s.close()
    assert logged.get("/api/approvals/history").json()[0]["via"] == "2fa"

    rej = _action(a)
    assert logged.post("/api/approvals/decide", json={"items": [{"kind": "action", "ref_id": rej}], "approve": False}).status_code == 200
    s = dbmod.new_session()
    assert s.get(dbmod.Action, rej).status == "rejected"
    s.close()


def test_low_risk_action_approves_with_a_click(logged):
    (a,) = make_projects("Zombie Party")
    act = _action(a, risk="baixo")
    assert logged.post("/api/approvals/decide", json={"items": [{"kind": "action", "ref_id": act}], "approve": True}).status_code == 200


def test_new_navigation_has_three_areas_and_old_routes_redirect(logged):
    html = logged.get("/approvals").text
    assert '<h1 id="page-title">Aprovações</h1>' in html and "/static/approvals.js" in html
    assert 'href="/approvals" aria-current="page"' in html
    for old in ('href="/plan"', 'href="/agents"', 'href="/content"'):
        assert old not in html  # saíram do menu
    for label in ("Aprovações", "Kanban", "Automações"):
        assert f"<span>{label}</span>" in html
    r = logged.get("/plan", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/approvals"
    r = logged.get("/agents", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/automations#agentes"
    auto = logged.get("/automations").text
    assert 'data-tab="agentes"' in auto and "/static/agents.js" in auto and 'id="agents"' in auto


def test_old_routes_need_login(client):
    for path in ("/plan", "/agents", "/approvals"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_board_shows_tasks_content_and_actions_with_types(logged):
    (a,) = make_projects("Zombie Party")
    _task(a, "Tarefa A")
    c, act = _content(a), _action(a)
    data = logged.get(f"/api/board?project={a}").json()["tasks"]
    by_type = {t["type"]: t for t in data}
    assert set(by_type) == {"task", "content", "action"}
    assert by_type["content"]["status"] == "proposed" and by_type["content"]["id"] == c
    assert by_type["action"]["status"] == "proposed" and by_type["action"]["id"] == act and by_type["action"]["risk"] == "alto"
    # Depois de aprovar o conteúdo, ele vai para "na fila"; enviar vira concluído.
    assert logged.post("/api/approvals/decide", json={"items": [{"kind": "content", "ref_id": c}], "approve": True}).status_code == 200
    assert logged.post(f"/api/content/{c}/sent", json={}).status_code == 200
    data = logged.get(f"/api/board?project={a}").json()["tasks"]
    assert next(t for t in data if t["type"] == "content")["status"] == "done"
    make_projects("A Ilha")
    assert [t for t in logged.get("/api/board?project=a-ilha").json()["tasks"]] == []  # não vaza entre projetos


def test_waiting_task_records_why(logged):
    (a,) = make_projects("Zombie Party")
    tid = _task(a, "Esperando", status="waiting")
    s = dbmod.new_session()
    s.get(dbmod.Task, tid).wait_reason = "2fa"
    s.commit()
    s.close()
    t = next(x for x in logged.get("/api/board").json()["tasks"] if x["id"] == tid and x["type"] == "task")
    assert t["wait_reason"] == "2fa" and t["status"] == "waiting"
