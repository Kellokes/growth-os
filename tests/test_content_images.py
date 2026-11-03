import asyncio
import base64

from app import content as cnt
from app import db as dbmod
from app import layouts as lay
from app import modelcfg
from app.providers.base import ProviderError

from .test_growth import make_projects, pid  # noqa: F401 - fixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 40
B64 = base64.b64encode(PNG).decode()


def new_piece(slug="zombie-party", channel="instagram", fmt="carrossel", **kw):
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid(slug), channel=channel, body="Texto do post", media="foto da festa",
                          visual_format=fmt, **kw)
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def approve_layout(logged, monkeypatch, fmt="carrossel"):
    async def fake(model, prompt, refs, ratio):
        return PNG, "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    s = dbmod.new_session()
    modelcfg.save(s, "image", "openrouter", "google/gemini-2.5-flash-image", "")
    s.close()
    r = logged.post("/api/layouts/generate", json={"project": "zombie-party", "format": fmt, "count": 1})
    logged.post(f"/api/layouts/{r.json()['layouts'][0]['id']}/decide", json={"approve": True})


def test_norm_visual_and_block_ingest():
    assert cnt.norm_visual("", "instagram") == "carrossel" and cnt.norm_visual("Stories", "linkedin") == "story"
    assert cnt.norm_visual("", "linkedin") == "" and cnt.norm_visual("nenhum", "instagram") == ""
    assert "formato" in cnt.guide()


def test_visual_piece_cannot_be_approved_or_scheduled_without_image(logged, monkeypatch):
    make_projects("Zombie Party")
    cid = new_piece()
    r = logged.post(f"/api/content/{cid}/decide", json={"approve": True})
    assert r.status_code == 409 and "imagem" in r.json()["detail"]
    item = logged.get("/api/content?project=zombie-party").json()["items"][0]
    assert item["needs_image"] is True and item["visual_format"] == "carrossel"

    # com a imagem (upload do owner) aprova e agenda
    assert logged.post(f"/api/content/{cid}/image/upload", json={"data": "###"}).status_code == 422
    r = logged.post(f"/api/content/{cid}/image/upload", json={"data": B64})
    assert r.status_code == 200 and r.json()["image_url"] and not r.json()["needs_image"]
    assert logged.get(r.json()["image_url"]).content == PNG
    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).json()["status"] == "approved"

    # peça sem imagem não agenda, nem texto puro muda isso
    other = new_piece(status="approved")
    r = logged.patch(f"/api/content/{other}", json={"scheduled_at": "2030-01-10T10:00"})
    assert r.status_code == 409
    assert logged.patch(f"/api/content/{other}", json={"visual_format": ""}).status_code == 200
    assert logged.patch(f"/api/content/{other}", json={"scheduled_at": "2030-01-10T10:00"}).json()["stage"] == "scheduled"
    assert logged.patch(f"/api/content/{other}", json={"visual_format": "tiktok"}).status_code == 422
    assert logged.post(f"/api/content/{other}/image/upload", json={"data": B64}).status_code == 422  # peça sem formato visual


def test_generate_needs_approved_layout_then_uses_it(logged, monkeypatch):
    make_projects("Zombie Party")
    cid = new_piece()
    r = logged.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 422 and "layout" in r.json()["detail"].lower()
    approve_layout(logged, monkeypatch)
    seen = {}

    async def fake(model, prompt, refs, ratio):
        seen.update(n=len(refs), ratio=ratio, prompt=prompt)
        return PNG + b"2", "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    r = logged.post(f"/api/content/{cid}/image", json={"feedback": "mais verde"})
    assert r.status_code == 200 and r.json()["image_url"] and r.json()["image_error"] == ""
    assert seen["n"] == 1 and seen["ratio"] == "4:5" and "foto da festa" in seen["prompt"] and "mais verde" in seen["prompt"]
    first = r.json()["image_url"]
    again = logged.post(f"/api/content/{cid}/image", json={}).json()["image_url"]
    assert again != first and logged.get(first).status_code == 404  # a versão anterior é apagada
    assert logged.delete(again.replace("/api/layouts/assets/", "/api/layouts/assets/")).status_code == 409  # só pela peça
    assert logged.delete(f"/api/content/{cid}").status_code == 200
    assert logged.get(again).status_code == 404


def test_autogen_runs_once_per_piece_and_records_failure(logged, monkeypatch):
    make_projects("Zombie Party")
    ok, bad, text_only = new_piece(), new_piece(), new_piece(channel="linkedin", fmt="")
    asyncio.run(lay.autogen_pending())  # sem layout aprovado: não gasta nada
    s = dbmod.new_session()
    assert s.get(dbmod.ContentItem, ok).image_id is None and s.get(dbmod.ContentItem, ok).image_error == ""
    s.close()
    approve_layout(logged, monkeypatch)
    calls = {"n": 0}

    async def fake(model, prompt, refs, ratio):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ProviderError("sem créditos")
        return PNG, "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    assert asyncio.run(lay.autogen_pending()) == 2
    s = dbmod.new_session()
    a, b, t = (s.get(dbmod.ContentItem, i) for i in (ok, bad, text_only))
    assert a.image_id and a.image_error == "" and b.image_id is None and "sem créditos" in b.image_error
    assert t.image_id is None and t.image_error == ""  # peça de texto não gera imagem
    s.close()
    assert asyncio.run(lay.autogen_pending()) == 0  # a que falhou não tenta de novo sozinha
    assert calls["n"] == 2
    r = logged.post(f"/api/content/{bad}/image", json={})  # o botão do owner tenta de novo
    assert r.status_code == 200 and r.json()["image_error"] == ""
