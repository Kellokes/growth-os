import asyncio
from datetime import datetime, timedelta

from app import content as cnt
from app import db as dbmod
from app import growth

from .test_growth import _approved_task, fake, make_projects, pid  # noqa: F401 - fixtures

POST = '''Pauta pronta.
```conteudo
[{"canal": "Instagram", "titulo": "Bastidores", "texto": "Olha o que rolou ontem 🎉", "midia": "foto do evento",
  "quando": "2030-01-10 18:30"},
 {"canal": "e-mail", "titulo": "Proposta de parceria", "texto": "Olá, Ana! Queremos...", "para": "ana@marca.com",
  "objetivo": "parceria"},
 {"canal": "instagram", "texto": ""}]
```'''


def test_content_pages_need_login(client):
    assert client.get("/content", follow_redirects=False).status_code == 303
    for path in ("/api/content", "/api/content/agenda", "/api/content/outputs", "/api/content/channels"):
        assert client.get(path).status_code == 401


def test_content_page_renders_in_nav(logged):
    html = logged.get("/content").text
    assert '<h1 id="page-title">Conteúdo</h1>' in html
    assert 'href="/board" aria-current="page"' in html  # a central de conteúdo mora dentro do Kanban
    assert "/static/content.js" in html


def test_parse_helpers():
    assert cnt.norm_channel("Twitter") == "x" and cnt.norm_channel("whatever") == "outro"
    assert cnt.parse_when("2030-01-10T18:30") == datetime(2030, 1, 10, 18, 30)
    assert cnt.parse_when("2030-01-10") == datetime(2030, 1, 10, 9, 0)
    assert cnt.parse_when("amanhã") is None and cnt.parse_when("") is None


def test_agent_blocks_become_pending_pieces_of_the_task_project(logged, fake):
    fake([POST])
    tid = _approved_task("Pauta da semana")
    asyncio.run(growth.execute_task(tid))
    s = dbmod.new_session()
    first = s.query(dbmod.Message).filter_by(conversation_id=s.get(dbmod.Task, tid).conversation_id).first()
    assert "```conteudo" in first.content  # o agente sabe como entregar
    rows = s.query(dbmod.ContentItem).order_by(dbmod.ContentItem.id).all()
    assert len(rows) == 2  # o vazio é ignorado
    ig, mail = rows
    assert ig.channel == "instagram" and ig.status == "pending" and ig.scheduled_at == datetime(2030, 1, 10, 18, 30)
    assert mail.channel == "email" and mail.recipient == "ana@marca.com" and mail.purpose == "parceria"
    assert {r.project_id for r in rows} == {pid("zombie-party")} and {r.agent_slug for r in rows} == {"tech"}
    # Ler a mesma conversa de novo não duplica.
    cnt.ingest_blocks(s, s.get(dbmod.Task, tid), POST)
    assert s.query(dbmod.ContentItem).count() == 2
    s.close()

    data = logged.get("/api/content?project=zombie-party").json()
    assert data["counts"] == {"pending": 2}
    make_projects("Outro")
    assert logged.get("/api/content?project=outro").json()["items"] == []  # não vaza entre projetos
    assert len(logged.get("/api/content").json()["items"]) == 2  # "todos" junta

    out = logged.get("/api/content/outputs?project=zombie-party").json()["outputs"]
    assert out[0]["id"] == tid and len(out[0]["content"]) == 2 and "Pauta pronta" in out[0]["result"]
    assert logged.get("/api/content/outputs?project=outro").json()["outputs"] == []


def test_lifecycle_approve_schedule_send(logged):
    make_projects("Zombie Party")
    r = logged.post("/api/content", json={"project": "zombie-party", "channel": "linkedin", "body": "Post do owner"})
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["stage"] == "ready"
    cid = r.json()["id"]
    assert logged.post("/api/content", json={"project": "zombie-party", "body": "  "}).status_code == 422

    soon = (cnt.local_now() + timedelta(days=2)).replace(hour=10, minute=0)
    r = logged.patch(f"/api/content/{cid}", json={"scheduled_at": soon.strftime("%Y-%m-%dT%H:%M")})
    assert r.json()["stage"] == "scheduled"
    assert logged.patch(f"/api/content/{cid}", json={"scheduled_at": "depois"}).status_code == 422
    agenda = logged.get("/api/content/agenda?project=zombie-party&days=7").json()
    assert [e["item"]["id"] for e in agenda["events"] if e["kind"] == "content"] == [cid]

    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).status_code == 409  # já aprovada
    assert logged.post(f"/api/content/{cid}/sent", json={"link": "javascript:alert(1)"}).status_code == 422
    r = logged.post(f"/api/content/{cid}/sent", json={"link": "https://linkedin.com/p/1"})
    assert r.json()["stage"] == "sent" and r.json()["link"].startswith("https://")
    assert logged.patch(f"/api/content/{cid}", json={"body": "mudou"}).status_code == 409
    assert logged.delete(f"/api/content/{cid}").status_code == 409  # enviado fica no histórico
    assert logged.post(f"/api/content/{cid}/reopen").status_code == 409


def test_reject_and_reopen_pending_piece(logged):
    make_projects("Zombie Party")
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), agent_slug="social", channel="instagram", body="rascunho")
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    assert logged.post(f"/api/content/{cid}/sent", json={}).status_code == 409  # sem aprovação não sai
    r = logged.post(f"/api/content/{cid}/decide", json={"approve": False, "note": "tom errado"})
    assert r.json()["status"] == "rejected" and r.json()["note"] == "tom errado"
    assert logged.post(f"/api/content/{cid}/reopen").json()["status"] == "pending"
    assert logged.patch(f"/api/content/{cid}", json={"body": "versão 2", "project": "outro"}).status_code == 422
    assert logged.patch(f"/api/content/{cid}", json={"body": "versão 2"}).json()["body"] == "versão 2"
    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).json()["stage"] == "ready"
    assert logged.delete(f"/api/content/{cid}").status_code == 200
    actions = [r["action"] for r in logged.get("/api/audit").json()]
    assert "content_reject" in actions and "content_approve" in actions and "content_delete" in actions


def test_agenda_lists_routine_runs(logged):
    make_projects("Zombie Party")
    r = logged.post("/api/gprojects/zombie-party/automations",
                    json={"agent": "social", "title": "Post diário", "frequency": "daily", "hour": 23})
    assert r.status_code == 200
    ev = logged.get("/api/content/agenda?project=zombie-party&days=7").json()["events"]
    routines_ = [e for e in ev if e["kind"] == "routine"]
    assert 6 <= len(routines_) <= 7 and routines_[0]["routine"]["title"] == "Post diário"
    make_projects("Outro")
    assert logged.get("/api/content/agenda?project=outro").json()["events"] == []
