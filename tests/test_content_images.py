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


TEMPLATE = ("<html><body style='margin:0'><div style='background-image:{{bg}}'><h1>{{titulo}}</h1>"
            "<p>{{texto}}</p><small>{{marca}}</small></div></body></html>")
RENDERS: list[str] = []


def approve_layout(logged, monkeypatch, fmt="carrossel"):
    """Layout aprovado -> convertido em template HTML -> template aprovado (IA e renderizador simulados)."""
    async def fake(model, prompt, refs, ratio):
        return PNG, "image/png"

    async def fake_html(model, prompt, image):
        return "```html\n" + TEMPLATE + "\n```"

    def fake_render(html, f):
        RENDERS.append(html)
        return PNG + b"R"

    monkeypatch.setattr(lay, "request_image", fake)
    monkeypatch.setattr(lay, "request_html", fake_html)
    monkeypatch.setattr(lay, "render_png", fake_render)
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
    assert "<h1>Festa de Halloween</h1>" in html and "<p>Sábado, 20h</p>" in html and 'url("data:image/png;base64,' in html
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
    out = lay.fill_template(TEMPLATE, "none", "A {{texto}} B", "<i>x</i>", "Marca & Cia")
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


def test_render_png_talks_to_gotenberg(monkeypatch):
    sent = {}

    class Resp:
        status_code, content = 200, PNG

    def fake_post(url, files, data, timeout):
        sent.update(url=url, name=files["files"][0], data=data)
        return Resp()

    monkeypatch.setattr(lay.httpx, "post", fake_post)
    assert lay.render_png("<html/>", "story") == PNG
    assert sent["url"].endswith("/forms/chromium/screenshot/html") and sent["name"] == "index.html"
    assert (sent["data"]["width"], sent["data"]["height"], sent["data"]["format"]) == ("1080", "1920", "png")
    Resp.status_code = 500
    try:
        lay.render_png("<html/>", "story")
    except ProviderError:
        pass
    else:
        raise AssertionError("500 deveria virar ProviderError")
