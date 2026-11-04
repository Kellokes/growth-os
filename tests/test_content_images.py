import asyncio
import base64

import pytest

from app import content as cnt
from app import db as dbmod
from app import layouts as lay
from app import modelcfg
from app import motion
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


TEMPLATE = ("<html><body style='margin:0'><div style='background-image:{{bg}}'><h1>{{titulo}}</h1>"
            "<p>{{texto}}</p><small>{{marca}}</small></div></body></html>")
RENDERS: list[str] = []


def approve_layout(logged, monkeypatch, fmt="carrossel"):
    """Layout aprovado -> convertido em template HTML -> template aprovado (IA e renderizador simulados)."""
    async def fake(model, prompt, refs, ratio):
        return PNG, "image/png"

    async def fake_html(model, prompt, image):
        return "```html\n" + TEMPLATE + "\n```"

    def fake_render(html, f, bg, title, text, brand=""):
        RENDERS.append(motion.build_composition(html, f, "BG" if bg else "SAMPLE", title, text, brand))
        return PNG + b"R"

    monkeypatch.setattr(lay, "request_image", fake)
    monkeypatch.setattr(lay, "request_html", fake_html)
    monkeypatch.setattr(motion, "render_still", fake_render)
    RENDERS.clear()
    s = dbmod.new_session()
    modelcfg.save(s, "image", "openrouter", "google/gemini-2.5-flash-image", "")
    modelcfg.save(s, "layout_html", "openrouter", "google/gemini-2.5-pro", "")
    s.close()
    r = logged.post("/api/layouts/generate", json={"project": "zombie-party", "format": fmt, "count": 1})
    lid = r.json()["layouts"][0]["id"]
    logged.post(f"/api/layouts/{lid}/decide", json={"approve": True})
    t = logged.post(f"/api/layouts/{lid}/template", json={})
    assert t.status_code == 200, t.text
    logged.post(f"/api/layouts/{t.json()['id']}/decide", json={"approve": True})


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


def test_generate_needs_approved_template_then_fills_it(logged, monkeypatch):
    make_projects("Zombie Party")
    cid = new_piece(title="Festa de Halloween", img_text="Sábado, 20h")
    r = logged.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 422 and "template" in r.json()["detail"].lower()
    approve_layout(logged, monkeypatch)
    seen = {}

    async def fake(model, prompt, refs, ratio):
        seen.update(n=len(refs), ratio=ratio, prompt=prompt)
        return PNG + b"2", "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    RENDERS.clear()
    r = logged.post(f"/api/content/{cid}/image", json={"feedback": "mais verde"})
    assert r.status_code == 200 and r.json()["image_url"] and r.json()["image_error"] == ""
    assert seen["ratio"] == "4:5" and "NO text" in seen["prompt"] and "foto da festa" in seen["prompt"] and "mais verde" in seen["prompt"]
    html = RENDERS[-1]
    assert '<h1><span class="m-title">Festa de Halloween</span></h1>' in html and 'class="m-text">Sábado, 20h</span>' in html and "BG" in html
    first = r.json()["image_url"]
    again = logged.post(f"/api/content/{cid}/image", json={}).json()["image_url"]
    assert again != first and logged.get(first).status_code == 404  # a versão anterior é apagada
    assert logged.delete(again).status_code == 409  # só pela peça

    # editar o texto da imagem refaz só a imagem final, com o mesmo fundo e sem chamar a IA
    n = seen["n"]
    seen.clear()
    r = logged.patch(f"/api/content/{cid}", json={"img_title": "NOVO <b>TÍTULO</b>"})
    assert r.status_code == 200 and not seen and r.json()["image_url"] != again
    assert "NOVO &lt;b&gt;TÍTULO&lt;/b&gt;" in RENDERS[-1]  # texto sempre escapado
    assert logged.delete(f"/api/content/{cid}").status_code == 200
    s = dbmod.new_session()
    assert s.query(dbmod.LayoutAsset).filter(dbmod.LayoutAsset.kind.in_(("piece", "background"))).count() == 0
    s.close()


def test_autogen_runs_once_per_piece_and_records_failure(logged, monkeypatch):
    make_projects("Zombie Party")
    ok, bad, text_only = new_piece(), new_piece(), new_piece(channel="linkedin", fmt="")
    asyncio.run(lay.autogen_pending())  # sem template aprovado: não gasta nada
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
    assert a.image_id and a.bg_id and a.image_error == "" and b.image_id is None and "sem créditos" in b.image_error
    assert t.image_id is None and t.image_error == ""  # peça de texto não gera imagem
    s.close()
    assert asyncio.run(lay.autogen_pending()) == 0  # a que falhou não tenta de novo sozinha
    assert calls["n"] == 2
    r = logged.post(f"/api/content/{bad}/image", json={})  # o botão do owner tenta de novo
    assert r.status_code == 200 and r.json()["image_error"] == ""


def test_template_sanitize_and_fill():
    ok = lay.sanitize_template(TEMPLATE)
    assert ok == TEMPLATE
    for bad in ("", TEMPLATE + "<script>x()</script>", TEMPLATE.replace("<div", "<div onclick='x'"),
                TEMPLATE + "<img src='https://evil.com/x.png'>", TEMPLATE.replace("{{texto}}", ""),
                TEMPLATE + "<style>@import url(x.css)</style>"):
        try:
            lay.sanitize_template(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    out = motion.build_composition(TEMPLATE, "story", "none", "A {{texto}} B", "<i>x</i>", "Marca & Cia")
    assert "A {{texto}} B" in out and "&lt;i&gt;x&lt;/i&gt;" in out and "Marca &amp; Cia" in out  # uma passada só


def test_template_flow_requires_approved_layout_and_model(logged, monkeypatch):
    make_projects("Zombie Party")
    approve_layout(logged, monkeypatch)
    st = logged.get("/api/layouts?project=zombie-party").json()
    assert [t["status"] for t in st["templates"]] == ["approved"] and st["html_model"]["model"] == "google/gemini-2.5-pro"
    lid = st["layouts"][0]["id"]
    assert logged.post(f"/api/layouts/{lid + 99}/template", json={}).status_code == 404
    r = logged.post("/api/layouts/generate", json={"project": "zombie-party", "format": "story", "count": 1})
    proposed = r.json()["layouts"][0]["id"]
    assert logged.post(f"/api/layouts/{proposed}/template", json={}).status_code == 409  # layout ainda não aprovado

    async def bad_html(model, prompt, image):
        return "```html\n<html><script>x()</script>{{bg}}{{titulo}}{{texto}}</html>\n```"

    monkeypatch.setattr(lay, "request_html", bad_html)
    logged.post(f"/api/layouts/{proposed}/decide", json={"approve": True})
    r = logged.post(f"/api/layouts/{proposed}/template", json={})
    assert r.status_code == 422 and "recusado" in r.json()["detail"]
    tid = logged.get("/api/layouts?project=zombie-party").json()["templates"][0]["id"]
    assert logged.delete(f"/api/layouts/assets/{tid}").status_code == 409  # template oficial não se apaga


def test_motion_service_calls(monkeypatch):
    sent = {}

    class Resp:
        status_code, content = 200, PNG

        def json(self):
            return {"error": "boom"}

    def fake_post(url, json, timeout):
        sent.update(url=url, json=json)
        return Resp()

    monkeypatch.setattr(motion.httpx, "post", fake_post)
    assert motion.render_still(TEMPLATE, "story", (PNG, "image/png"), "T", "x", "M") == PNG
    assert sent["url"].endswith("/still") and sent["json"]["at"] == motion.STILL_AT
    assert "assets/bg.png" in sent["json"]["binary"] and 'url("assets/bg.png")' in sent["json"]["files"]["index.html"]
    motion.render_still(TEMPLATE, "story", None, "T", "x")
    assert sent["json"]["binary"] == {} and "linear-gradient" in sent["json"]["files"]["index.html"]
    Resp.status_code = 500
    with pytest.raises(ProviderError, match="boom"):
        motion.render_still(TEMPLATE, "story", None, "T", "x")
    Resp.status_code, Resp.content = 200, b"nao-e-png"
    with pytest.raises(ProviderError):
        motion.render_still(TEMPLATE, "story", None, "T", "x")


def test_build_composition_contract():
    doc = motion.build_composition(TEMPLATE, "reel", "BG", "<i>t</i>", "x", "Marca", duration=6)
    assert 'data-composition-id="main"' in doc and 'data-width="1080" data-height="1920"' in doc and 'class="clip"' in doc
    assert 'window.__timelines["main"]' in doc and "lib/gsap.min.js" in doc and "&lt;i&gt;t&lt;/i&gt;" in doc
    assert "{{" not in doc and "<script src=\"http" not in doc


def test_normalize_fonts():
    out = lay.normalize_fonts("a{font-family: Impact, sans-serif} b{font-family:'Poppins', Arial} c{font-family: var(--f)}")
    assert "Impact" not in out and "'Montserrat'" in out and "'Poppins'" in out and "var(--f)" in out


def test_piece_video_flow(logged, monkeypatch):
    make_projects("Zombie Party")
    approve_layout(logged, monkeypatch)
    cid = new_piece(title="Festa")
    calls = []

    async def fake_video(html, f, bg, title, text, brand=""):
        calls.append((f, title))
        return b"\x00\x00\x00\x18ftypmp42" + b"v" * 40

    monkeypatch.setattr(motion, "render_video", fake_video)
    r = logged.post(f"/api/content/{cid}/video")
    assert r.status_code == 422 and "fundo" in r.json()["detail"]  # sem imagem gerada ainda
    assert logged.post(f"/api/content/{cid}/image", json={}).status_code == 200
    item = logged.post(f"/api/content/{cid}/video").json()
    assert item["video_url"] and item["can_video"] and calls[0][0] == "carrossel"
    full = logged.get(item["video_url"])
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    part = logged.get(item["video_url"], headers={"Range": "bytes=4-7"})
    assert part.status_code == 206 and part.content == b"ftyp" and part.headers["content-range"] == f"bytes 4-7/{len(full.content)}"
    tail = logged.get(item["video_url"], headers={"Range": "bytes=-3"})
    assert tail.status_code == 206 and tail.content == full.content[-3:]
    assert logged.get(item["video_url"], headers={"Range": "bytes=9999-"}).status_code == 416
    assert logged.delete(item["video_url"]).status_code == 409  # só pela peça
    # mudar o texto da imagem invalida o vídeo
    r = logged.patch(f"/api/content/{cid}", json={"img_title": "Outro"}).json()
    assert r["video_url"] == "" and logged.get(item["video_url"]).status_code == 404
    again = logged.post(f"/api/content/{cid}/video").json()["video_url"]
    assert logged.delete(f"/api/content/{cid}/video").json()["video_url"] == "" and logged.get(again).status_code == 404
    logged.post(f"/api/content/{cid}/video")
    assert logged.delete(f"/api/content/{cid}").status_code == 200
    s = dbmod.new_session()
    assert s.query(dbmod.LayoutAsset).filter(dbmod.LayoutAsset.kind.in_(("piece", "background", "video"))).count() == 0
    s.close()


def test_html_conversion_uses_configured_provider(logged, monkeypatch):
    """Codex (ou Claude) configurado em "Layouts · converter em HTML" é usado; antes só o OpenRouter passava."""
    make_projects("Zombie Party")
    approve_layout(logged, monkeypatch)
    s = dbmod.new_session()
    modelcfg.save(s, "layout_html", "codex", "gpt-6-sol", "high")
    assert lay.html_choice(s).provider == "codex"
    modelcfg.save(s, "layout_html", "", "", "")
    with pytest.raises(ProviderError, match="provider"):
        lay.html_choice(s)
    s.close()

    seen = {}
    from app.providers.codex_cli import CodexCLIProvider

    async def fake_cli(self, prompt, image, mime):
        seen.update(model=self.model, reasoning=self.reasoning, image=image, mime=mime)
        return "```html\n" + TEMPLATE + "\n```"

    async def boom(*a, **k):
        raise AssertionError("não deveria chamar o OpenRouter")

    monkeypatch.setattr(CodexCLIProvider, "complete_with_image", fake_cli)
    monkeypatch.setattr(CodexCLIProvider, "available", lambda self: (True, ""))
    monkeypatch.setattr(lay, "request_html", boom)
    s = dbmod.new_session()
    modelcfg.save(s, "layout_html", "codex", "gpt-6-sol", "high")
    s.close()
    st = logged.get("/api/layouts?project=zombie-party").json()
    lid = st["layouts"][0]["id"]
    r = logged.post(f"/api/layouts/{lid}/template", json={"feedback": "mais escuro"})
    assert r.status_code == 200, r.text
    assert seen["model"] == "gpt-6-sol" and seen["reasoning"] == "high" and seen["image"] == PNG and r.json()["model"] == "gpt-6-sol"


def test_codex_image_args(monkeypatch):
    from pathlib import Path

    from app.providers import codex_cli

    got = {}

    async def fake_run(args, cwd, timeout, stdin=None):
        got.update(args=args, stdin=stdin, img=Path(args[[a.startswith("--image=") for a in args].index(True)].split("=", 1)[1]).read_bytes())
        Path(args[args.index("--output-last-message") + 1]).write_text("ok")
        return ""

    monkeypatch.setattr(codex_cli, "run_cli", fake_run)
    p = codex_cli.CodexCLIProvider().configured("gpt-6-sol", "low")
    assert asyncio.run(p.complete_with_image("faça", PNG, "image/png")) == "ok"
    assert got["img"] == PNG and got["stdin"] == "faça" and got["args"][-1] == "-" and "--model" in got["args"]


def test_sanitize_strips_harmless_boilerplate_but_blocks_real_risks():
    page = ("<!DOCTYPE html>\n<html lang=\"pt-BR\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=1080\">"
            "<title>Layout</title><!-- feito pelo modelo --><style>body{margin:0}</style></head><body>"
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"><rect width=\"10\" height=\"10\"/></svg>"
            "<div style='background-image:{{bg}}'><h1>{{titulo}}</h1><p>{{texto}}</p>{{marca}}</div></body></html>")
    out = lay.sanitize_template(page)
    assert "<meta" not in out and "<title" not in out and "xmlns" not in out and "<!--" not in out and "<style>" in out
    for bad in (page.replace("<style>", "<style>@import url(//evil.com/x.css);"),
                page.replace("<title>", "<script>x()</script><title>"),
                page.replace("<head>", "<head><link rel=stylesheet href=x.css>"),
                page.replace("<svg", "<img src=\"https://evil.com/p.png\"><svg")):
        with pytest.raises(ValueError):
            lay.sanitize_template(bad)
    with pytest.raises(ValueError, match="achei:"):
        lay.sanitize_template(page.replace("<h1>", "<h1 onclick='x()'>"))


def test_plate_hybrid_keeps_art_and_only_edits_scenery(logged, monkeypatch):
    make_projects("Zombie Party")
    prompts = []

    async def fake_img(model, prompt, refs, ratio):
        prompts.append((prompt, len(refs), refs[0][0] if refs else b""))
        return PNG + bytes([len(prompts)]), "image/png"

    approve_layout(logged, monkeypatch)
    monkeypatch.setattr(lay, "request_image", fake_img)
    s = dbmod.new_session()
    layout = s.query(dbmod.LayoutAsset).filter_by(kind="layout").first()
    import asyncio as aio
    t = aio.run(lay.generate_template(s, layout, "mais limpo"))
    assert prompts == []  # o layout já nasce sem texto: não há mais chamada de imagem para remover texto
    plate = s.get(dbmod.LayoutAsset, t.plate_id)
    assert plate.kind == "plate" and plate.data == layout.data
    assert "NO text at all" in lay.build_prompt("Zombie Party", "", "story") and "LOREM" not in lay.build_prompt("Zombie Party", "", "story")
    assert "ONLY the TEXT" in t.prompt and "{{marca}}" in t.prompt  # modelo só desenha texto
    t.status = "approved"
    s.commit()
    s.query(dbmod.LayoutAsset).filter(dbmod.LayoutAsset.kind == "template", dbmod.LayoutAsset.id != t.id).update({"status": "archived"})
    s.commit()
    cid = new_piece()
    s2 = dbmod.new_session()
    c = s2.get(dbmod.ContentItem, cid)
    aio.run(lay.generate_piece_image(s2, c))
    scenery, nrefs, ref = prompts[-1]
    assert "CHANGE ONLY the photographic scenery" in scenery and "foto da festa" in scenery and nrefs == 1 and ref == plate.data
    # apagar o template leva a arte junto
    s3 = dbmod.new_session()
    lay.delete_template(s3, s3.get(dbmod.LayoutAsset, t.id))
    s3.commit()
    assert s3.get(dbmod.LayoutAsset, plate.id) is None
    for x in (s, s2, s3):
        x.close()
