import asyncio

import pytest

from app import calllog
from app import db as dbmod
from app import layouts as lay
from app import modelcfg
from app.providers.base import ChatTurn, ProviderError

from .test_chat_and_commands import events, new_conv, say, use  # noqa: F401 - fixtures
from .test_content_images import PNG, new_piece  # noqa: F401
from .test_growth import make_projects, pid  # noqa: F401 - fixtures


def rows():
    s = dbmod.new_session()
    try:
        return s.query(dbmod.AiCall).order_by(dbmod.AiCall.id).all()
    finally:
        s.close()


def test_clean_hides_secrets_and_personal_data():
    text = ("use a chave sk-or-v1-abcdef1234567890abcd e o token=ghp_abcdefghijklmnop1234 "
            "mande para ana.silva@exemplo.com ou (11) 98765-4321, cpf 123.456.789-09. Authorization: Bearer abc123def456ghi")
    out = calllog.clean(text, 1000)
    for leak in ("sk-or-v1", "ghp_abc", "ana.silva", "98765", "123.456.789", "abc123def456"):
        assert leak not in out
    assert len(calllog.clean("x " * 500)) <= calllog.SNIPPET


def test_summaries_never_hold_the_whole_prompt():
    hist = [ChatTurn("user", "a" * 5000), ChatTurn("assistant", "b"), ChatTurn("user", "última pergunta " + "z " * 400)]
    s = calllog.summarize_history(hist)
    assert s.startswith("3 mensagem(ns)") and len(s) < 200


def test_track_records_ok_and_error(logged):
    with calllog.track("openrouter", "m/x", "image", "pedido") as call:
        call.done("resposta com a chave sk-or-v1-abcdef1234567890abcd")
    with pytest.raises(ProviderError):
        with calllog.track("codex", "gpt", "threads", "pedido2"):
            raise ProviderError("falhou com Bearer abcdefgh12345678")
    ok, err = rows()
    assert (ok.status, ok.activity, ok.provider, ok.model) == ("ok", "image", "openrouter", "m/x")
    assert ok.duration_ms is not None and "sk-or-v1" not in ok.response_summary
    assert err.status == "error" and "falhou" in err.error and "abcdefgh12345678" not in err.error


def test_cancelled_call_is_recorded(logged):
    async def go():
        async def inner():
            with calllog.track("claude", "", "threads", "x"):
                await asyncio.sleep(10)
        t = asyncio.ensure_future(inner())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    (r,) = rows()
    assert r.status == "error" and r.error == "cancelada"


def test_chat_turn_is_logged_and_listed(logged, use):
    use(["Oi, tudo certo."])
    cid = new_conv(logged)
    events(say(logged, cid, "me chame no email fulano@x.com"))
    data = logged.get("/api/calls").json()
    assert data["running"] == 0 and len(data["calls"]) == 1
    c = data["calls"][0]
    assert c["activity"] == "threads" and c["activity_label"] == "Threads" and c["provider"] == "fake" and c["status"] == "ok"
    assert c["actor"] == "owner" and "fulano@x.com" not in c["request"] and "caracteres" in c["response"]


def test_chat_failure_is_logged_with_the_error(logged, monkeypatch):
    class Boom(lay.ProviderError.__mro__[1]):  # RuntimeError
        pass

    from app.providers.base import Provider

    class Bad(Provider):
        id = "fake"

        async def complete(self, history):
            raise ProviderError("provedor caiu")

    monkeypatch.setattr("app.main.all_providers", lambda: {"fake": Bad()})
    cid = new_conv(logged)
    events(say(logged, cid))
    c = logged.get("/api/calls?status=error").json()["calls"][0]
    assert c["status"] == "error" and "provedor caiu" in c["error"]
    assert logged.get("/api/calls?status=ok").json()["calls"] == []


def test_misconfigured_image_model_is_logged_not_silent(logged):
    make_projects("Zombie Party")
    s = dbmod.new_session()
    modelcfg.save(s, "layout_html", "openrouter", "", "")
    s.close()
    # só abrir a tela não grava nada
    logged.get("/api/layouts?project=zombie-party")
    assert rows() == []
    s = dbmod.new_session()
    layout = dbmod.LayoutAsset(project_id=pid("zombie-party"), kind="layout", format="carrossel", status="approved",
                               mime="image/png", data=PNG)
    s.add(layout)
    s.commit()
    lid = layout.id
    s.close()
    r = logged.post(f"/api/layouts/{lid}/template", json={})
    assert r.status_code == 422
    (c,) = rows()
    assert (c.activity, c.provider, c.status) == ("layout_html", "openrouter", "error")
    assert "modelo do OpenRouter" in c.error and c.project_id == pid("zombie-party")


def test_request_image_and_html_are_tracked(logged, monkeypatch):
    async def fake_img(model, prompt, refs, ratio):
        return PNG, "image/png"

    async def fake_html(model, prompt, image):
        return "<html></html>"

    monkeypatch.setattr(lay, "_request_image", fake_img)
    monkeypatch.setattr(lay, "_request_html", fake_html)
    with calllog.context("image", 7):
        asyncio.run(lay.request_image("google/x", "um prompt secreto longo " * 50, [], "4:5"))
    asyncio.run(lay.request_html("google/y", "p", (PNG, "image/png")))
    img, html = rows()
    assert (img.activity, img.provider, img.model, img.project_id, img.status) == ("image", "openrouter", "google/x", 7, "ok")
    assert "um prompt secreto" not in img.request_summary and "PNG" in img.response_summary.upper() or "image/png" in img.response_summary
    assert (html.activity, html.status) == ("layout_html", "ok")


def test_stale_running_rows_are_closed_on_startup(logged):
    s = dbmod.new_session()
    s.add(dbmod.AiCall(activity="threads", provider="claude", status="running"))
    s.commit()
    s.close()
    assert logged.get("/api/calls").json()["running"] == 1
    calllog.sweep(startup=True)
    data = logged.get("/api/calls").json()
    assert data["running"] == 0 and data["calls"][0]["status"] == "error"


def test_calls_endpoint_needs_login(client):
    assert client.get("/api/calls").status_code in (401, 303, 307)


def test_clear_errors_keeps_ok_and_running(logged):
    s = dbmod.new_session()
    s.add_all([dbmod.AiCall(activity="threads", provider="a", status="error", error="x"),
               dbmod.AiCall(activity="threads", provider="a", status="ok"),
               dbmod.AiCall(activity="threads", provider="a", status="running")])
    s.commit()
    s.close()
    assert logged.get("/api/calls").json()["failed_total"] == 1
    assert logged.delete("/api/calls?status=ok").status_code == 422
    assert logged.delete("/api/calls?status=error").json() == {"deleted": 1}
    assert sorted(r.status for r in rows()) == ["ok", "running"]


def test_usage_is_recorded_and_summed(logged):
    with calllog.track("openrouter", "m/x", "image", "pedido") as call:
        calllog.note_usage({"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01})
        calllog.note_usage({"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.005})
        calllog.note_usage("lixo")  # ignorado
        call.done("ok")
    calllog.note_usage({"cost": 1})  # fora de uma chamada: ignorado
    (r,) = rows()
    assert (r.input_tokens, r.output_tokens) == (150, 25) and abs(r.cost_usd - 0.015) < 1e-9


def test_costs_summary_prefers_api_cost_then_estimates(logged):
    from app import costs

    models = [{"id": "m/text", "pricing": {"prompt": 1e-6, "completion": 2e-6}},
              {"id": "m/img", "pricing": {"image": 0.04}}]
    with calllog.track("openrouter", "m/text", "layout_html", "a"):
        calllog.note_usage({"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.5})  # custo da API vence
    with calllog.track("openrouter", "m/text", "threads", "b"):
        calllog.note_usage({"prompt_tokens": 1000, "completion_tokens": 500})  # estimado: 0.001 + 0.001
    with calllog.track("openrouter", "m/img", "image", "c"):
        pass  # linha antiga sem uso: imagem × preço por imagem
    with calllog.track("openrouter", "m/text", "threads", "d"):
        pass  # sem uso nem preço de imagem: fora do total
    with calllog.track("claude", "opus", "threads", "e"):
        pass
    s = costs.summarize(dbmod.new_session(), models)
    assert abs(s["total"]["cost_usd"] - (0.5 + 0.002 + 0.04)) < 1e-9
    assert s["total"]["calls"] == 4 and s["total"]["unpriced"] == 1 and s["subscription_calls"] == {"claude": 1}
    assert s["sources"] == {"api": 1, "estimado": 2, "sem uso": 1}
    assert {r["model"] for r in s["by_model"]} == {"m/text", "m/img"} and len(s["by_day"]) == 1


def test_costs_endpoint_and_page(logged, monkeypatch):
    async def fake():
        return {"live": True, "models": [{"id": "m/text", "pricing": {"prompt": 1e-6, "completion": 2e-6}}]}

    monkeypatch.setattr("app.main.modelcfg.openrouter_models", fake)
    with calllog.track("openrouter", "m/text", "threads", "b"):
        calllog.note_usage({"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.25})
    d = logged.get("/api/costs").json()
    assert d["total"]["cost_usd"] == 0.25 and d["by_activity"][0]["label"] == "Threads"
    assert logged.get("/costs").status_code == 200

def test_after_id_hides_older_calls_without_deleting_them(logged):
    s = dbmod.new_session()
    s.add_all([dbmod.AiCall(activity="threads", provider="a", status="error", error="velho") for _ in range(7)])
    s.commit()
    s.close()
    first = logged.get("/api/calls?limit=5").json()
    assert len(first["calls"]) == 5 and first["failed_total"] == 7
    cut = first["latest_id"]
    s = dbmod.new_session()
    s.add(dbmod.AiCall(activity="threads", provider="a", status="ok"))
    s.commit()
    s.close()
    after = logged.get(f"/api/calls?limit=5&after_id={cut}").json()
    assert len(after["calls"]) == 1 and after["failed_total"] == 0 and after["failed_24h"] == 0
    assert len(rows()) == 8  # nada foi apagado


def _openrouter_reply(monkeypatch, reply: str):
    from app.providers import openrouter as orp

    sent = {}

    async def fake_post(self, client, body):
        sent["body"] = body
        return 200, {"choices": [{"message": {"content": reply}}], "usage": {"prompt_tokens": 3}}, ""

    monkeypatch.setattr(orp.OpenRouterProvider, "_post", fake_post)
    return orp.OpenRouterProvider().configured("m/x"), sent


def _log_prompts(monkeypatch, on: bool):
    from app.config import get_settings

    monkeypatch.setenv("GOS_LOG_PROMPTS", "true" if on else "false")
    get_settings.cache_clear()


def test_full_prompt_is_not_stored_by_default(logged, monkeypatch):
    provider, _ = _openrouter_reply(monkeypatch, "resposta inteira do modelo")
    with calllog.track("openrouter", "m/x", "threads", "pedido"):
        asyncio.run(provider.complete([ChatTurn("user", "pergunta completa " * 40)]))
    (r,) = rows()
    assert r.prompt_full == "" and r.response_full == ""
    data = logged.get("/api/calls").json()
    assert data["log_prompts"] is False and data["calls"][0]["has_full"] is False


def test_log_prompts_stores_system_user_and_reply_without_secrets(logged, monkeypatch):
    _log_prompts(monkeypatch, True)
    provider, sent = _openrouter_reply(monkeypatch, "linha 1\nlinha 2 com a chave sk-or-v1-abcdef1234567890abcd")
    question = "use o token=ghp_abcdefghijklmnop1234 e me diga " + "x " * 300
    with calllog.track("openrouter", "m/x", "threads", "pedido") as call:
        reply = asyncio.run(provider.complete([ChatTurn("user", question)]))
        call.done(calllog.summarize_text(reply))
    (r,) = rows()
    assert r.prompt_full.startswith("### sistema\nVocê é o Growth OS") and "### user\nuse o token=[oculto]" in r.prompt_full
    assert "x " * 300 in r.prompt_full  # inteiro, não o trecho de 100 caracteres
    assert r.response_full.startswith("linha 1\nlinha 2") and "sk-or-v1" not in r.response_full
    assert "ghp_abc" not in r.prompt_full and "Bearer" not in r.prompt_full
    # a chave de verdade vai só no cabeçalho, nunca no corpo que é logado
    assert "Authorization" not in str(sent["body"])
    data = logged.get("/api/calls").json()
    assert data["log_prompts"] is True
    c = data["calls"][0]
    assert c["has_full"] is True and "prompt_full" not in c
    full = logged.get(f"/api/calls/{c['id']}").json()
    assert full["prompt_full"] == r.prompt_full and full["response_full"] == r.response_full
    assert logged.get("/api/calls/999999").status_code == 404


def test_log_prompts_keeps_prompt_of_failed_call_and_hides_images(logged, monkeypatch):
    _log_prompts(monkeypatch, True)
    with pytest.raises(ProviderError):
        with calllog.track("openrouter", "m/img", "layout_html", "pedido"):
            calllog.note_prompt(messages=[{"role": "user", "content": [
                {"type": "text", "text": "converta este layout"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}]}])
            raise ProviderError("caiu")
    (r,) = rows()
    assert r.status == "error" and "converta este layout" in r.prompt_full
    assert "[imagem enviada em base64]" in r.prompt_full and "AAAA" not in r.prompt_full


def test_full_text_is_capped_and_base64_is_hidden():
    assert "[imagem image/png em base64]" in calllog.redact_full("veja data:image/png;base64," + "Q" * 900)
    out = calllog.redact_full("a " * (calllog.FULL_LIMIT // 2 + 5))
    assert out.endswith("[cortado: 10 caracteres a mais]")


def test_detail_endpoint_needs_login(client):
    assert client.get("/api/calls/1").status_code in (401, 303, 307)
