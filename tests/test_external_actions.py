import asyncio
import json
import smtplib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import actions as acts
from app import db as dbmod
from app import growth
from app.connectors import senders

from .conftest import code
from .test_growth import make_projects, pid  # noqa: F401
from .test_integration_exec import connect, mock, run, ticket_for

TELEGRAM = {"chat_id": "123"}
POSTIZ = {"url": "https://postiz.exemplo.com"}
EMAIL = {"host": "smtp.exemplo.com", "port": "587", "from": "contato@exemplo.com"}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(acts, "RETRY_DELAYS", (0, 0))


REAL_EXECUTE = acts.execute_approved  # alguns testes trocam o executor do app; aqui chamamos o de verdade


def execute(action_id, transport=None):
    s = dbmod.new_session()
    try:
        a = s.get(dbmod.Action, action_id)
        asyncio.run(REAL_EXECUTE(s, a, transport))
        s.refresh(a)
        return a.status, a.error, a.attempts, a.external_ref
    finally:
        s.close()


def approve(logged, action_id, totp=None):
    body = {"items": [{"kind": "action", "ref_id": action_id}], "approve": True}
    if totp:
        body["totp"] = totp
    return logged.post("/api/approvals/decide", json=body)


def test_telegram_waits_for_approval_by_default_then_sends(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "telegram", TELEGRAM, monkeypatch)
    r = run(a, "telegram", "avisar_owner", {"texto": "Oi"})
    assert r["status"] == "awaiting_approval"
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    status, err, attempts, ref = (lambda aid: execute(aid, mock(handler)))(r["action_id"])
    assert status == "awaiting_approval"  # ainda não aprovada: nada é enviado
    assert seen == []
    s = dbmod.new_session()
    s.get(dbmod.Action, r["action_id"]).status = "approved"
    s.commit()
    s.close()
    status, err, attempts, ref = execute(r["action_id"], mock(handler))
    assert (status, ref) == ("done", "7") and seen[0][0] == "https://api.telegram.org/botchave-secreta/sendMessage"
    assert seen[0][1]["chat_id"] == "123" and "authorization" not in str(seen).lower()


def test_telegram_preapproved_runs_immediately_and_is_logged(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "telegram", {**TELEGRAM, "preaprovar": "sim"}, monkeypatch)
    sent = []
    r = run(a, "telegram", "avisar_owner", {"texto": "Plano pronto"},
            mock(lambda req: sent.append(req) or httpx.Response(200, json={"result": {"message_id": 1}})))
    assert r["status"] == "done" and len(sent) == 1
    hist = logged.get("/api/approvals/history").json()
    assert hist[0]["via"] == "liberada" and hist[0]["kind"] == "action"
    assert logged.get("/api/approvals").json()["counts"]["action"] == 0


def test_postiz_flow_approve_publishes_once_with_raw_key(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "postiz", POSTIZ, monkeypatch)
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("authorization"), json.loads(req.content or b"null")))
        if req.url.path.endswith("/integrations"):
            return httpx.Response(200, json=[{"id": "c1", "name": "Loja", "identifier": "instagram"}])
        return httpx.Response(200, json=[{"postId": "p-9"}])

    assert run(a, "postiz", "listar_canais", transport=mock(handler))["result"]["canais"][0]["id"] == "c1"
    r = run(a, "postiz", "publicar_post", {"canal_id": "c1", "texto": "Promo!"})
    assert r["status"] == "awaiting_approval"
    card = logged.get("/api/approvals?kind=action").json()["items"][0]
    assert card["risk"] == "medio" and not card["needs_2fa"]  # post no próprio projeto: clique simples
    # Aprova pelo caminho do app, mas sem rede: a execução usa o transporte falso na chamada seguinte.
    monkeypatch.setattr("app.main.acts.execute_approved", lambda *x, **k: asyncio.sleep(0))
    assert approve(logged, r["action_id"]).status_code == 200
    calls.clear()
    assert execute(r["action_id"], mock(handler))[0] == "done"
    assert execute(r["action_id"], mock(handler))[0] == "done"  # segunda chamada não faz nada
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2] == "chave-secreta"  # sem "Bearer": o Postiz usa a chave direto
    body = posts[0][3]
    assert body["type"] == "now" and body["posts"][0]["integration"]["id"] == "c1"
    assert body["posts"][0]["value"][0]["content"] == "Promo!" and body["posts"][0]["settings"]["__type"] == "instagram"
    s = dbmod.new_session()
    assert s.get(dbmod.Action, r["action_id"]).external_ref == "p-9"
    s.close()


def test_postiz_rejects_unknown_channel_and_past_schedule(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "postiz", POSTIZ, monkeypatch)
    handler = lambda req: httpx.Response(200, json=[{"id": "c1", "name": "Loja", "identifier": "x"}])  # noqa: E731
    conn = senders.Postiz()
    ctx = senders.Ctx(config=POSTIZ, secret="k", transport=mock(handler))
    with pytest.raises(senders.ConnectorError, match="canal não encontrado"):
        asyncio.run(conn.run(ctx, "publicar_post", {"canal_id": "zzz", "texto": "x"}))
    with pytest.raises(senders.ConnectorError, match="já passou"):
        asyncio.run(conn.run(ctx, "agendar_post", {"canal_id": "c1", "texto": "x", "quando": "2020-01-01T10:00:00Z"}))


def test_transient_failures_are_retried_but_real_ones_are_not(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "telegram", {**TELEGRAM, "preaprovar": "sim"}, monkeypatch)
    n = {"c": 0}

    def flaky(req):
        n["c"] += 1
        return httpx.Response(503) if n["c"] < 3 else httpx.Response(200, json={"result": {"message_id": 2}})

    assert run(a, "telegram", "avisar_owner", {"texto": "x"}, mock(flaky))["status"] == "done" and n["c"] == 3
    s = dbmod.new_session()
    assert s.query(dbmod.Action).order_by(dbmod.Action.id.desc()).first().attempts == 3
    s.close()
    n["c"] = 0
    always = lambda req: (n.__setitem__("c", n["c"] + 1), httpx.Response(500))[1]  # noqa: E731
    r = run(a, "telegram", "avisar_owner", {"texto": "y"}, mock(always))
    assert r["status"] == "error" and n["c"] == 3  # desiste depois de 3 tentativas
    n["c"] = 0
    bad = lambda req: (n.__setitem__("c", n["c"] + 1), httpx.Response(400, text="chat inválido"))[1]  # noqa: E731
    assert run(a, "telegram", "avisar_owner", {"texto": "z"}, mock(bad))["status"] == "error" and n["c"] == 1


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=0):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        FakeSMTP.sent.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(("msg", msg["To"], msg["Subject"], msg.get_content().strip()))


def test_email_needs_2fa_and_is_one_recipient_only(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "email", EMAIL, monkeypatch, secret="senha-smtp")
    FakeSMTP.sent.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    r = run(a, "email", "enviar_email", {"para": "ana@marca.com", "assunto": "Parceria", "texto": "Olá, Ana!"})
    assert r["status"] == "awaiting_approval"
    card = logged.get("/api/approvals?kind=action").json()["items"][0]
    assert card["needs_2fa"] and "ana@marca.com" in card["preview"]
    assert approve(logged, r["action_id"]).status_code == 403  # sem 2FA nada sai
    assert FakeSMTP.sent == []
    ok = approve(logged, r["action_id"], code())
    assert ok.status_code == 200 and ok.json()["actions"][0]["status"] == "done"
    assert FakeSMTP.sent == [("login", "contato@exemplo.com", "senha-smtp"), ("msg", "ana@marca.com", "Parceria", "Olá, Ana!")]
    for bad in ("a@b.com, c@d.com", "sem-arroba", "a@b.com\nBcc: x@y.com"):
        assert run(a, "email", "enviar_email", {"para": bad, "assunto": "x", "texto": "y"})["status"] in ("error", "awaiting_approval")
    s = dbmod.new_session()
    # Destinatário inválido ou com quebra de linha nunca chega ao SMTP, mesmo se alguém aprovar.
    for act in s.query(dbmod.Action).filter(dbmod.Action.status == "awaiting_approval").all():
        act.status = "approved"
        s.commit()
        asyncio.run(acts.execute_approved(s, act))
        assert act.status == "failed"
    s.close()
    assert len([x for x in FakeSMTP.sent if x[0] == "msg"]) == 1


def test_daily_cap_per_project_and_integration(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "email", EMAIL, monkeypatch)
    for i in range(5):
        assert run(a, "email", "enviar_email", {"para": f"p{i}@x.com", "assunto": "a", "texto": "b"})["status"] == "awaiting_approval"
    r = run(a, "email", "enviar_email", {"para": "p9@x.com", "assunto": "a", "texto": "b"})
    assert r["status"] == "error" and "limite de 5" in r["message"]


def test_publish_approved_content_by_email_and_social(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "email", EMAIL, monkeypatch)
    connect(a, "postiz", POSTIZ, monkeypatch)
    FakeSMTP.sent.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    s = dbmod.new_session()
    mail = dbmod.ContentItem(project_id=pid(a), channel="email", title="Parceria", body="Oi!", recipient="ana@marca.com", status="approved")
    post = dbmod.ContentItem(project_id=pid(a), channel="instagram", body="Promo", status="pending")
    s.add_all([mail, post])
    s.commit()
    mid, pid_ = mail.id, post.id
    s.close()
    assert logged.post(f"/api/content/{pid_}/publish", json={"canal_id": "c1"}).status_code == 409  # não aprovada
    assert logged.post(f"/api/content/{mid}/publish", json={}).status_code == 403  # e-mail exige 2FA
    r = logged.post(f"/api/content/{mid}/publish", json={"totp": code()})
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["action"]["status"] == "done"
    assert [x[0] for x in FakeSMTP.sent] == ["login", "msg"]
    hist = logged.get("/api/approvals/history").json()[0]
    assert hist["via"] == "2fa" and hist["requested_by"] == f"conteudo:{mid}"
    # Rede social sem canal escolhido, e rede sem integração.
    assert logged.post(f"/api/content/{pid_}/decide", json={"approve": True}).status_code == 200
    assert logged.post(f"/api/content/{pid_}/publish", json={}).status_code == 422
    s = dbmod.new_session()
    s.query(dbmod.Integration).filter_by(kind="postiz").delete()
    s.commit()
    s.close()
    assert logged.post(f"/api/content/{pid_}/publish", json={"canal_id": "c1"}).status_code == 409
    wa = dbmod.new_session()
    z = dbmod.ContentItem(project_id=pid(a), channel="whatsapp", body="x", status="approved")
    wa.add(z)
    wa.commit()
    zid = z.id
    wa.close()
    assert logged.post(f"/api/content/{zid}/publish", json={}).status_code == 422


def test_failed_publish_marks_content_failed_and_can_retry(logged, monkeypatch):
    (a,) = make_projects("Zombie Party")
    connect(a, "email", EMAIL, monkeypatch)

    class Down(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"no")

    monkeypatch.setattr(smtplib, "SMTP", Down)
    s = dbmod.new_session()
    m = dbmod.ContentItem(project_id=pid(a), channel="email", title="T", body="B", recipient="ana@marca.com", status="approved")
    s.add(m)
    s.commit()
    mid = m.id
    s.close()
    r = logged.post(f"/api/content/{mid}/publish", json={"totp": code()})
    assert r.status_code == 200 and r.json()["status"] == "failed" and "recusou" in r.json()["note"]
    FakeSMTP.sent.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    again = logged.post(f"/api/content/{mid}/publish", json={"totp": code()})
    assert again.json()["status"] == "sent"
