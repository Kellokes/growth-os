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
            return httpx.Response(200, json=[{"id": "c1", "name": "Loja", "identifier": "linkedin"}])
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
    assert body["posts"][0]["value"][0]["content"] == "Promo!" and body["posts"][0]["settings"]["__type"] == "linkedin"
    s = dbmod.new_session()
    assert s.get(dbmod.Action, r["action_id"]).external_ref == "p-9"
    s.close()


def _assets(project_slug, n):
    s = dbmod.new_session()
    pid_ = s.query(dbmod.GrowthProject).filter_by(slug=project_slug).one().id
    ids = []
    for i in range(n):
        row = dbmod.LayoutAsset(project_id=pid_, kind="piece", mime="image/png", data=b"png%d" % i)
        s.add(row)
        s.flush()
        ids.append(row.id)
    s.commit()
    s.close()
    return pid_, ids


def test_postiz_instagram_payload_has_post_type_and_media_in_all_paths(logged, monkeypatch):
    (slug,) = make_projects("Zombie Party")
    project_id, ids = _assets(slug, 2)
    posts, uploads = [], []

    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": "ig", "name": "IG", "identifier": "instagram"},
                                             {"id": "li", "name": "LI", "identifier": "linkedin"}])
        if req.url.path.endswith("/upload"):
            uploads.append(req.content)
            n = len(uploads)
            return httpx.Response(200, json={"id": f"m{n}", "path": f"https://cdn/{n}.png"})
        posts.append(json.loads(req.content))
        return httpx.Response(200, json=[{"postId": "p"}])

    conn = senders.Postiz()
    ctx = senders.Ctx(config=POSTIZ, secret="k", transport=mock(handler), project_id=project_id)
    midia = ",".join(map(str, ids))
    when = "2999-01-01T10:00:00Z"
    for op, extra in (("publicar_post", {}), ("agendar_post", {"quando": when}), ("criar_rascunho", {})):
        asyncio.run(conn.run(ctx, op, {"canal_id": "ig", "texto": "x", "midia_ids": midia, **extra}))
    asyncio.run(conn.run(ctx, "publicar_post", {"canal_id": "li", "texto": "x"}))
    assert [p["posts"][0]["settings"].get("post_type") for p in posts] == ["post", "post", "post", None]
    # as duas imagens sobem em ordem e vão no post (carrossel); sem mídia, o LinkedIn manda image vazio
    assert [p["posts"][0]["value"][0]["image"] for p in posts[:1]] == [[{"id": "m1", "path": "https://cdn/1.png"},
                                                                         {"id": "m2", "path": "https://cdn/2.png"}]]
    assert b"png0" in uploads[0] and b"png1" in uploads[1]
    assert posts[3]["posts"][0]["value"][0]["image"] == []
    posts.clear()
    asyncio.run(conn.run(ctx, "publicar_post", {"canal_id": "ig", "texto": "x", "midia_ids": str(ids[0]), "tipo": "story"}))
    assert posts[0]["posts"][0]["settings"]["post_type"] == "story"


def test_postiz_instagram_without_media_and_foreign_asset_are_refused(logged, monkeypatch):
    a, b = make_projects("Zombie Party", "A Ilha")
    _, mine = _assets(a, 1)
    other_pid, theirs = _assets(b, 1)
    handler = lambda req: httpx.Response(200, json=[{"id": "ig", "name": "IG", "identifier": "instagram"}])  # noqa: E731
    conn = senders.Postiz()
    ctx = senders.Ctx(config=POSTIZ, secret="k", transport=mock(handler), project_id=other_pid)
    with pytest.raises(senders.ConnectorError, match="exige ao menos uma imagem"):
        asyncio.run(conn.run(ctx, "publicar_post", {"canal_id": "ig", "texto": "x"}))
    with pytest.raises(senders.ConnectorError, match="não existe neste projeto"):
        asyncio.run(conn.run(ctx, "publicar_post", {"canal_id": "ig", "texto": "x", "midia_ids": str(mine[0])}))


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


def _fake_postiz(monkeypatch, canais):
    calls = []

    async def fake_run(self, ctx, op, params):
        calls.append((op, params))
        if op == "listar_canais":
            return {"canais": canais}
        return {"tipo": "schedule" if op == "agendar_post" else "now", "canal": "IG", "ref": "p1"}

    monkeypatch.setattr(senders.Postiz, "run", fake_run)
    return calls


def _pieces(a, *rows):
    s = dbmod.new_session()
    objs = [dbmod.ContentItem(project_id=pid(a), **r) for r in rows]
    s.add_all(objs)
    s.commit()
    ids = [o.id for o in objs]
    s.close()
    return ids


def test_approving_social_piece_sends_it_to_postiz(logged, monkeypatch):
    from app import content as cnt
    (a,) = make_projects("Zombie Party")
    connect(a, "postiz", POSTIZ, monkeypatch)
    # O Instagram sem Facebook chega do Postiz como "instagram-standalone"; canal desativado não conta.
    calls = _fake_postiz(monkeypatch, [{"id": "ig1", "nome": "IG", "rede": "instagram-standalone"},
                                       {"id": "ig0", "nome": "IG velho", "rede": "instagram", "desativado": True},
                                       {"id": "li1", "nome": "LI", "rede": "linkedin-page"}])
    future = cnt.local_now() + timedelta(days=2)
    ids = _pieces(a, dict(channel="instagram", body="A", status="pending", scheduled_at=future),
                  dict(channel="instagram", body="B", status="pending"),  # sem data: publica agora
                  dict(channel="linkedin", body="L", status="pending", scheduled_at=future),
                  dict(channel="x", body="C", status="pending", scheduled_at=future))  # sem canal X
    out = [logged.post(f"/api/content/{i}/decide", json={"approve": True}).json() for i in ids]
    assert [o["status"] for o in out[:3]] == ["sent", "sent", "sent"]
    sched = [c[1] for c in calls if c[0] == "agendar_post"]
    now = [c[1] for c in calls if c[0] == "publicar_post"]
    assert [(x["canal_id"], x["texto"]) for x in sched] == [("ig1", "A"), ("li1", "L")]
    assert [(x["canal_id"], x["texto"]) for x in now] == [("ig1", "B")]
    assert out[3]["status"] == "approved" and "nenhum canal de X" in out[3]["note"]  # motivo visível; Publicar segue valendo


def test_bulk_approval_and_sweep_send_to_postiz(logged, monkeypatch):
    from app import content as cnt
    from app import main
    (a,) = make_projects("Zombie Party")
    connect(a, "postiz", POSTIZ, monkeypatch)
    calls = _fake_postiz(monkeypatch, [{"id": "ig1", "nome": "IG", "rede": "instagram"}])
    future = cnt.local_now() + timedelta(days=1)
    bulk, stuck, two = _pieces(a, dict(channel="instagram", body="Lote", status="pending", scheduled_at=future),
                               dict(channel="instagram", body="Preso", status="approved", scheduled_at=future),
                               dict(channel="instagram", body="Dois", status="approved", scheduled_at=future))
    r = logged.post("/api/approvals/decide", json={"items": [{"kind": "content", "ref_id": bulk}], "approve": True})
    assert r.status_code == 200
    assert [c[1]["texto"] for c in calls if c[0] == "agendar_post"] == ["Lote"]
    # Aprovada antes (ou por outro caminho) e nunca enviada: a varredura do agendador manda; não repete.
    s = dbmod.new_session()
    s.get(dbmod.ContentItem, two).status = "sent"
    s.commit()
    s.close()
    asyncio.run(main.auto_send_pending())
    asyncio.run(main.auto_send_pending())
    assert [c[1]["texto"] for c in calls if c[0] == "agendar_post"] == ["Lote", "Preso"]
    s = dbmod.new_session()
    assert s.get(dbmod.ContentItem, stuck).status == "sent"
    s.close()


def test_each_project_uses_its_own_postiz_channel(logged, monkeypatch):
    from app import content as cnt
    (a,) = make_projects("A Ilha")
    # um Postiz só, três contas de Instagram: uma por projeto
    chans = [{"id": "z", "nome": "Zombie Party", "rede": "instagram"}, {"id": "t", "nome": "Take One", "rede": "instagram"},
             {"id": "i", "nome": "A Ilha - Jogue no seu Whats", "rede": "instagram"}]
    future = cnt.local_now() + timedelta(days=1)

    connect(a, "postiz", POSTIZ, monkeypatch)  # sem campo Canal: o nome que cita o projeto decide
    calls = _fake_postiz(monkeypatch, chans)
    (p1,) = _pieces(a, dict(channel="instagram", body="1", status="pending", scheduled_at=future))
    assert logged.post(f"/api/content/{p1}/decide", json={"approve": True}).json()["status"] == "sent"
    assert [c[1]["canal_id"] for c in calls if c[0] == "agendar_post"] == ["i"]

    connect(a, "postiz", {**POSTIZ, "canal": "take one"}, monkeypatch)  # o campo Canal manda mais que o nome
    calls.clear()
    (p2,) = _pieces(a, dict(channel="instagram", body="2", status="pending", scheduled_at=future))
    assert logged.post(f"/api/content/{p2}/decide", json={"approve": True}).json()["status"] == "sent"
    assert [c[1]["canal_id"] for c in calls if c[0] == "agendar_post"] == ["t"]

    connect(a, "postiz", {**POSTIZ, "canal": "inexistente"}, monkeypatch)  # canal errado: não chuta, explica
    (p3,) = _pieces(a, dict(channel="instagram", body="3", status="pending", scheduled_at=future))
    out = logged.post(f"/api/content/{p3}/decide", json={"approve": True}).json()
    assert out["status"] == "approved" and "inexistente" in out["note"]

    connect(a, "postiz", POSTIZ, monkeypatch)  # Publicar sem canal na tela também usa o do projeto
    calls.clear()
    assert logged.post(f"/api/content/{p3}/publish", json={}).json()["status"] == "sent"
    assert [c[1]["canal_id"] for c in calls if c[0] in ("agendar_post", "publicar_post")] == ["i"]


def test_postiz_network_mapping():
    from app.main import postiz_network
    assert postiz_network("instagram-standalone") == "instagram"
    assert postiz_network("linkedin-page") == "linkedin"
    assert postiz_network("x") == "x" and postiz_network("threads") == "threads"


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


def test_several_channels_of_one_network_pick_by_project(logged, monkeypatch):
    """Vários Instagram no Postiz: vai o que leva o nome do projeto; sem pista, a tela recebe as opções e lembra a escolha."""
    from app import content as cnt
    (ilha,) = make_projects("A Ilha")
    connect(ilha, "postiz", POSTIZ, monkeypatch)
    calls = _fake_postiz(monkeypatch, [{"id": "t1", "nome": "Take One", "rede": "instagram"},
                                       {"id": "z1", "nome": "Zombie Party", "rede": "instagram-standalone"},
                                       {"id": "a1", "nome": "A Ilha Jogo", "rede": "instagram"}])
    future = cnt.local_now() + timedelta(days=1)
    (one,) = _pieces(ilha, dict(channel="instagram", body="Ilha", status="pending", scheduled_at=future))
    r = logged.post(f"/api/content/{one}/decide", json={"approve": True}).json()
    assert r["status"] == "sent" and r["auto"]["state"] == "sent"
    assert [c[1]["canal_id"] for c in calls if c[0] == "agendar_post"] == ["a1"]

    # Sem nome que combine: pede a escolha (opções na resposta); publicar lembrando vale para as próximas.
    (zp,) = make_projects("Outro")
    calls.clear()
    connect(zp, "postiz", POSTIZ, monkeypatch)
    p1, p2 = _pieces(zp, dict(channel="instagram", body="1", status="pending"), dict(channel="instagram", body="2", status="pending"))
    r = logged.post(f"/api/content/{p1}/decide", json={"approve": True}).json()
    assert r["status"] == "approved" and r["auto"]["state"] == "pick" and len(r["auto"]["pick"]) == 3
    assert not calls or all(c[0] == "listar_canais" for c in calls)
    assert logged.post(f"/api/content/{p1}/publish", json={"canal_id": "z1", "lembrar": True}).json()["status"] == "sent"
    r2 = logged.post(f"/api/content/{p2}/decide", json={"approve": True}).json()
    assert r2["status"] == "sent" and [c[1]["canal_id"] for c in calls if c[0] == "publicar_post"] == ["z1", "z1"]


def test_postiz_canal_field_wins_over_name(logged, monkeypatch):
    from app import content as cnt
    (a,) = make_projects("A Ilha")
    connect(a, "postiz", {**POSTIZ, "canal": "take one"}, monkeypatch)
    calls = _fake_postiz(monkeypatch, [{"id": "t", "nome": "Take One", "rede": "instagram"}, {"id": "i", "nome": "A Ilha Jogo", "rede": "instagram"}])
    (p,) = _pieces(a, dict(channel="instagram", body="x", status="pending", scheduled_at=cnt.local_now() + timedelta(days=1)))
    assert logged.post(f"/api/content/{p}/decide", json={"approve": True}).json()["status"] == "sent"
    assert [c[1]["canal_id"] for c in calls if c[0] == "agendar_post"] == ["t"]
