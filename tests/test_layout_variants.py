"""Vários templates por formato, upload de arte, carrossel com capa/páginas/CTA e a mídia da peça por cima do layout."""
import asyncio
import base64
import json
import struct

import pytest

from app import content as cnt
from app import db as dbmod
from app import layouts as lay
from app import media as med
from app import modelcfg
from app import motion
from app.providers.base import ProviderError

from .test_growth import make_projects, pid  # noqa: F401 - fixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x04\x38\x00\x00\x07\x80\x08\x02" + b"1" * 30  # RGB, sem alfa
PNG_ALPHA = PNG[:25] + b"\x06" + PNG[26:]
B64 = base64.b64encode(PNG).decode()
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8 + b"\x00\x00\x00\x6cmvhd" + b"\x00" * 12 + struct.pack(">II", 1000, 12500) + b"v" * 60
SLOT = ("<html><body><div style='background-image:{{bg}}'><div class='win'>{{midia}}</div><h1>{{titulo}}</h1>"
        "<p>{{texto}}</p></div></body></html>")
PLAIN = "<html><body><div style='background-image:{{bg}}'><h1>{{titulo}}</h1><p>{{texto}}</p></div></body></html>"
RENDERS: list[dict] = []
AI: list[dict] = []


@pytest.fixture
def studio(logged, monkeypatch):
    make_projects("Zombie Party")

    async def fake_image(model, prompt, refs, ratio):
        AI.append({"prompt": prompt, "refs": len(refs), "ratio": ratio})
        return PNG, "image/png"

    def fake_render(html, f, bg, title, text, brand="", media=None):
        tag = ("assets/media.x", media[1]) if media else None
        RENDERS.append({"html": motion.build_composition(html, f, "BG" if bg else "none", title, text, brand, media=tag),
                        "title": title, "media": media is not None, "bg": bg is not None, "tpl": html})
        return PNG + b"R"

    monkeypatch.setattr(lay, "request_image", fake_image)
    monkeypatch.setattr(motion, "render_still", fake_render)
    RENDERS.clear()
    AI.clear()
    s = dbmod.new_session()
    modelcfg.save(s, "image", "openrouter", "google/gemini-2.5-flash-image", "")
    modelcfg.save(s, "layout_html", "openrouter", "google/gemini-2.5-pro", "")
    s.close()
    return logged


def upload_template(logged, fmt="story", role="", name="", text="embaixo", data=B64):
    r = logged.post("/api/layouts/upload", json={"project": "zombie-party", "format": fmt, "role": role, "name": name,
                                                 "data": data, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def ai_template(logged, monkeypatch, html, fmt="story", role=""):
    async def fake_html(model, prompt, image):
        return "```html\n" + html + "\n```"

    monkeypatch.setattr(lay, "request_html", fake_html)
    r = upload_template(logged, fmt, role, text="ia")
    tid = r["template"]["id"]
    assert logged.post(f"/api/layouts/{tid}/decide", json={"approve": True}).status_code == 200
    return tid


def new_piece(fmt="story", **kw):
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body="Primeiro parágrafo\n\nSegundo\n\nTerceiro",
                          media="festa", visual_format=fmt, img_title="CAPA", **kw)
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def test_upload_art_becomes_ready_template_and_defaults(studio):
    first = upload_template(studio, name="Escuro")
    t1 = first["template"]
    assert first["layout"]["uploaded"] and t1["status"] == "approved" and t1["is_default"] and t1["name"] == "Escuro"
    assert not AI  # texto padrão: sem IA
    second = upload_template(studio, name="Claro", text="topo")["template"]
    assert not second["is_default"]  # o padrão continua o primeiro
    st = studio.get("/api/layouts?project=zombie-party").json()
    assert {t["id"] for t in st["templates"] if t["status"] == "approved"} == {t1["id"], second["id"]}
    assert [r["key"] for r in st["formats"][1]["roles"]] == ["capa", "pagina", "cta"] and st["positions"]

    assert studio.delete(f"/api/layouts/assets/{t1['id']}").status_code == 409  # padrão com outro disponível
    assert studio.post(f"/api/layouts/{second['id']}/default").json()["is_default"]
    assert studio.delete(f"/api/layouts/assets/{t1['id']}").status_code == 200
    assert studio.patch(f"/api/layouts/{second['id']}", json={"name": "Claro v2"}).json()["name"] == "Claro v2"

    # arte com transparência: a foto/vídeo da peça vai atrás dela
    alpha = upload_template(studio, data=base64.b64encode(PNG_ALPHA).decode())["template"]
    assert alpha["media_slot"] and not second["media_slot"]
    assert lay.has_alpha(PNG_ALPHA, "image/png") and not lay.has_alpha(PNG, "image/png")


def test_upload_rejects_bad_input(studio):
    body = {"project": "zombie-party", "format": "story", "data": B64}
    assert studio.post("/api/layouts/upload", json={**body, "format": "tiktok"}).status_code == 422
    assert studio.post("/api/layouts/upload", json={**body, "data": "###"}).status_code == 422


def test_carousel_runs_cover_pages_and_cta_templates(studio):
    capa = upload_template(studio, "carrossel", "capa", "Capa")["template"]["id"]
    pagina = upload_template(studio, "carrossel", "pagina", "Página", text="topo")["template"]["id"]
    cta = upload_template(studio, "carrossel", "cta", "CTA", text="meio")["template"]["id"]
    tpl = {t: dbmod.new_session().get(dbmod.LayoutAsset, t).html for t in (capa, pagina, cta)}
    cid = new_piece("carrossel", media_source="nenhuma")
    RENDERS.clear()
    r = studio.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 200, r.text
    item = r.json()
    assert not AI  # sem mídia: só arte e texto, nada pago
    roles = [s["role"] for s in item["slides"]]
    assert roles == ["pagina", "pagina", "cta"] and all(s["image_url"] for s in item["slides"])
    assert [x["tpl"] for x in RENDERS] == [tpl[capa], tpl[pagina], tpl[pagina], tpl[cta]]
    assert RENDERS[0]["title"] == "CAPA" and item["image_url"]

    # editar as páginas refaz as imagens sem IA, cada uma no seu template
    RENDERS.clear()
    slides = [{"role": "pagina", "title": "Um", "text": "a"}, {"role": "cta", "title": "Bora!", "text": ""}]
    item = studio.patch(f"/api/content/{cid}", json={"slides": slides}).json()
    assert [(s["role"], s["title"]) for s in item["slides"]] == [("pagina", "Um"), ("cta", "Bora!")]
    assert [x["title"] for x in RENDERS] == ["CAPA", "Um", "Bora!"] and not AI

    # escolher outro template para as páginas
    outra = upload_template(studio, "carrossel", "pagina", "Página 2")["template"]["id"]
    item = studio.patch(f"/api/content/{cid}", json={"template_ids": {"pagina": outra}}).json()
    assert item["template_ids"] == {"pagina": outra}
    RENDERS.clear()
    studio.post(f"/api/content/{cid}/image", json={})
    assert RENDERS[1]["tpl"] == dbmod.new_session().get(dbmod.LayoutAsset, outra).html
    assert studio.patch(f"/api/content/{cid}", json={"template_ids": {"cta": 99999}}).status_code == 422


def test_carousel_without_page_templates_falls_back_to_cover(studio):
    capa = upload_template(studio, "carrossel", "capa")["template"]["id"]
    cid = new_piece("carrossel", media_source="nenhuma")
    RENDERS.clear()
    assert studio.post(f"/api/content/{cid}/image", json={}).status_code == 200
    html = dbmod.new_session().get(dbmod.LayoutAsset, capa).html
    assert {x["tpl"] for x in RENDERS} == {html}


def test_uploaded_photo_goes_into_media_slot_without_ai(studio, monkeypatch):
    ai_template(studio, monkeypatch, SLOT)
    AI.clear()
    cid = new_piece()
    RENDERS.clear()
    r = studio.post(f"/api/content/{cid}/media/upload", json={"data": B64})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["media_source"] == "upload" and item["media_file_url"] and item["image_url"] and not AI
    assert RENDERS[-1]["media"] and '<img class="m-media" src="assets/media.x"' in RENDERS[-1]["html"]


def test_uploaded_photo_without_slot_is_merged_by_ai(studio, monkeypatch):
    ai_template(studio, monkeypatch, PLAIN)
    AI.clear()
    cid = new_piece()
    item = studio.post(f"/api/content/{cid}/media/upload", json={"data": B64}).json()
    assert item["image_url"] and len(AI) == 1 and AI[0]["refs"] == 2 and "SECOND attached image" in AI[0]["prompt"]


def test_uploaded_video_plays_in_layout_and_in_piece_video(studio, monkeypatch):
    ai_template(studio, monkeypatch, SLOT, fmt="reel")
    cid = new_piece("reel")
    RENDERS.clear()
    item = studio.post(f"/api/content/{cid}/media/upload", json={"data": base64.b64encode(MP4).decode()}).json()
    assert item["image_url"] and '<video id="m-video" class="m-media"' in RENDERS[-1]["html"]
    got = {}

    async def fake_video(html, f, bg, title, text, brand="", media=None):
        got.update(media=media)
        return b"\x00\x00\x00\x18ftypmp42" + b"v" * 40

    monkeypatch.setattr(motion, "render_video", fake_video)
    assert studio.post(f"/api/content/{cid}/video").json()["video_url"]
    assert got["media"][1] == "video/mp4"
    assert motion.video_seconds((MP4, "video/mp4")) == 12.5 and med.mp4_duration(MP4) == 12.5
    assert studio.delete(f"/api/content/{cid}/media").json()["media_file_url"] == ""


def test_video_under_opaque_art_replaces_background(studio, monkeypatch):
    ai_template(studio, monkeypatch, PLAIN, fmt="reel")
    cid = new_piece("reel")
    RENDERS.clear()
    studio.post(f"/api/content/{cid}/media/upload", json={"data": base64.b64encode(MP4).decode()})
    last = RENDERS[-1]
    assert last["media"] and not last["bg"] and 'class="m-media m-under"' in last["html"]


def test_generated_video_job_then_poll(studio, monkeypatch):
    ai_template(studio, monkeypatch, SLOT, fmt="reel")
    s = dbmod.new_session()
    modelcfg.save(s, "video", "openrouter", "google/veo-3.1", "")
    s.close()
    cid = new_piece("reel", media_source="gerar_video")
    sent = {}

    async def fake_submit(model, prompt, ratio):
        sent.update(model=model, ratio=ratio, prompt=prompt)
        return "gen-vid-1-abc"

    async def fake_poll(job):
        return ("completed", MP4, "")

    monkeypatch.setattr(med, "submit_video", fake_submit)
    monkeypatch.setattr(med, "poll_video", fake_poll)
    assert asyncio.run(lay.autogen_pending()) == 0  # vídeo de IA só no clique do owner
    item = studio.post(f"/api/content/{cid}/image", json={}).json()
    assert item["media_generating"] and not item["image_url"] and sent["ratio"] == "9:16" and "No text" in sent["prompt"]
    assert studio.post(f"/api/content/{cid}/image", json={}).status_code == 422  # ainda gerando
    assert asyncio.run(med.poll_pending_videos()) == 1
    item = [i for i in studio.get("/api/content?project=zombie-party").json()["items"] if i["id"] == cid][0]
    assert item["image_url"] and item["media_file_url"] and not item["media_generating"]


def test_link_source_downloads_safely(studio, monkeypatch):
    ai_template(studio, monkeypatch, SLOT)
    cid = new_piece()
    for bad in ("http://x.com/a.png", "https://x.com:8443/a.png", "https://user:pw@x.com/a.png", "ftp://x"):
        assert studio.post(f"/api/content/{cid}/media/link", json={"url": bad}).status_code == 422
    with pytest.raises(ValueError):
        med._public_host("127.0.0.1")
    with pytest.raises(ValueError):
        med._public_host("10.0.0.5")

    async def fake_download(url):
        return PNG, "image/png"

    monkeypatch.setattr(med, "download", fake_download)
    item = studio.post(f"/api/content/{cid}/media/link", json={"url": "https://cdn.example.com/foto.png"}).json()
    assert item["media_source"] == "link" and item["media_url"].endswith("foto.png") and item["image_url"]


def test_detect_and_limits():
    assert med.detect(PNG) == "image/png" and med.detect(MP4) == "video/mp4" and med.detect(b"<html>") is None
    with pytest.raises(ValueError):
        med.check_media(b"")
    with pytest.raises(ValueError):
        med.check_media(b"\x00\x00\x00\x18ftypmp42" + b"0" * med.MAX_VIDEO_BYTES)
    assert med.norm_source("vídeo") == "gerar_video" and med.norm_source("???") == "gerar_imagem"


def test_autogen_waits_for_owner_media(studio, monkeypatch):
    ai_template(studio, monkeypatch, SLOT)
    up = new_piece(media_source="upload")
    none = new_piece(media_source="nenhuma")
    assert asyncio.run(lay.autogen_pending()) == 1
    s = dbmod.new_session()
    assert s.get(dbmod.ContentItem, up).image_id is None and s.get(dbmod.ContentItem, none).image_id


def test_agent_block_with_pages_cta_and_link(logged):
    make_projects("Zombie Party")
    s = dbmod.new_session()
    t = dbmod.Task(project_id=pid("zombie-party"), agent_slug="growth", title="posts", status="done")
    s.add(t)
    s.commit()
    block = {"canal": "instagram", "texto": "Post", "formato": "carrossel", "titulo_imagem": "Capa",
             "paginas": [{"titulo": "P1", "texto": "a"}, "só texto"], "cta": {"titulo": "Siga"},
             "fonte_midia": "link", "link_midia": "https://cdn.example.com/v.mp4"}
    c = cnt.ingest_blocks(s, t, "```conteudo\n" + json.dumps(block) + "\n```")[0]
    assert c.media_source == "link" and c.media_url.endswith("v.mp4")
    assert [(x["role"], x["title"]) for x in lay.piece_slides(c)] == [("pagina", "P1"), ("pagina", ""), ("cta", "Siga")]
    # agente não escolhe vídeo de IA (caro) nem link sem endereço
    c2 = cnt.ingest_blocks(s, t, "```conteudo\n" + json.dumps({"canal": "tiktok", "texto": "X", "fonte_midia": "gerar_video"}) + "\n```")[0]
    assert c2.media_source == "gerar_imagem"
    s.close()


def test_composition_media_tags():
    html = motion.build_composition(SLOT, "story", "none", "T", "x", media=("assets/media.mp4", "video/mp4"), duration=8)
    assert 'data-duration="8"' in html and '<video id="m-video" class="m-media" src="assets/media.mp4" muted' in html
    assert "m-media m-under" not in html
    plain = motion.build_composition(PLAIN, "story", "none", "T", "x")
    assert "<video" not in plain and "{{midia}}" not in plain
    with pytest.raises(ProviderError):
        raise ProviderError("ok")
