import base64

from app import db as dbmod
from app import layouts as lay
from app import modelcfg
from app.providers.base import ProviderError

from .test_growth import make_projects  # noqa: F401 - fixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
B64 = base64.b64encode(PNG).decode()


def set_image_model():
    s = dbmod.new_session()
    modelcfg.save(s, "image", "openrouter", "google/gemini-2.5-flash-image", "")
    s.close()


def test_layouts_need_login(client):
    assert client.get("/layouts", follow_redirects=False).status_code == 303
    assert client.get("/api/layouts?project=x").status_code == 401


def test_page_and_nav(logged):
    html = logged.get("/layouts").text
    assert '<h1 id="page-title">Layouts</h1>' in html and 'href="/layouts" aria-current="page"' in html


def test_detect_mime_and_decode():
    assert lay.detect_mime(PNG) == "image/png" and lay.detect_mime(b"GIF89a") is None
    assert lay.decode_image("data:image/png;base64," + B64) == (PNG, "image/png")
    for bad in ("###", base64.b64encode(b"<svg/>").decode(), ""):
        try:
            lay.decode_image(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_references_brief_and_isolation(logged):
    make_projects("Zombie Party", "A Ilha")
    r = logged.post("/api/layouts/references", json={"project": "zombie-party", "data": B64, "note": "logo"})
    assert r.status_code == 200 and r.json()["kind"] == "reference"
    assert logged.post("/api/layouts/references", json={"project": "zombie-party", "data": "aGk="}).status_code == 422
    assert logged.put("/api/layouts/brief", json={"project": "zombie-party", "brief": " sombrio "}).json()["brief"] == "sombrio"
    st = logged.get("/api/layouts?project=zombie-party").json()
    assert len(st["references"]) == 1 and st["brief"] == "sombrio" and "Modelos" in st["problem"]
    other = logged.get("/api/layouts?project=a-ilha").json()
    assert other["references"] == [] and other["brief"] == ""  # não vaza entre projetos
    img = logged.get(r.json()["url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == PNG
    for _ in range(lay.MAX_REFERENCES):
        logged.post("/api/layouts/references", json={"project": "zombie-party", "data": B64})
    assert logged.post("/api/layouts/references", json={"project": "zombie-party", "data": B64}).status_code == 422


def test_generate_approve_and_replace(logged, monkeypatch):
    make_projects("Zombie Party")
    gen = {"n": 0, "refs": None}

    async def fake(model, prompt, refs, ratio):
        gen["n"] += 1
        gen["refs"], gen["ratio"], gen["prompt"] = len(refs), ratio, prompt
        if gen["n"] > 2:
            raise ProviderError("sem créditos")
        return PNG + bytes([gen["n"]]), "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    body = {"project": "zombie-party", "format": "story", "count": 2}
    assert logged.post("/api/layouts/generate", json=body).status_code == 422  # sem modelo de imagem configurado
    set_image_model()
    logged.post("/api/layouts/references", json={"project": "zombie-party", "data": B64, "note": "logo verde"})
    logged.put("/api/layouts/brief", json={"project": "zombie-party", "brief": "preto e verde ácido"})
    assert logged.post("/api/layouts/generate", json={**body, "format": "tiktok"}).status_code == 422
    assert logged.post("/api/layouts/generate", json={**body, "count": 9}).status_code == 422
    r = logged.post("/api/layouts/generate", json=body)
    assert r.status_code == 200 and len(r.json()["layouts"]) == 2 and r.json()["error"] == ""
    assert gen["refs"] == 1 and gen["ratio"] == "9:16" and "preto e verde ácido" in gen["prompt"] and "logo verde" in gen["prompt"]
    a, b = [x["id"] for x in r.json()["layouts"]]
    partial = logged.post("/api/layouts/generate", json={**body, "count": 2})  # falha na 1ª: nada sai
    assert partial.status_code == 502 and "sem créditos" in partial.json()["detail"]

    assert logged.post(f"/api/layouts/{a}/decide", json={"approve": True}).json()["status"] == "approved"
    assert logged.post(f"/api/layouts/{b}/decide", json={"approve": True}).json()["status"] == "approved"
    layouts = {x["id"]: x["status"] for x in logged.get("/api/layouts?project=zombie-party").json()["layouts"]}
    assert layouts == {a: "approved", b: "approved"}  # vários layouts aprovados por formato (cada um pode virar template)
    assert logged.post(f"/api/layouts/{b}/decide", json={"approve": False}).json()["status"] == "rejected"
    assert logged.delete(f"/api/layouts/assets/{b}").status_code == 200
    actions = [r["action"] for r in logged.get("/api/audit").json()]
    assert "layout_generate" in actions and "layout_approve" in actions


def test_extract_image():
    ok = {"choices": [{"message": {"images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + B64}}]}}]}
    assert lay.extract_image(ok) == (PNG, "image/png")
    for bad in ({}, {"choices": [{"message": {"content": "oi"}}]}):
        try:
            lay.extract_image(bad)
        except ProviderError:
            continue
        raise AssertionError(bad)


def test_request_image_retries_image_only_models(monkeypatch):
    """Seedream/FLUX só geram imagem: o OpenRouter responde 404 a ["image","text"], e a segunda tentativa usa ["image"]."""
    import asyncio

    tried = []
    ok = {"choices": [{"message": {"images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + B64}}]}}]}
    err = {"error": {"code": 404, "message": "No endpoints found that support the requested output modalities: image, text"}}

    async def fake_post(key, body):
        tried.append(body["modalities"])
        return (200, ok) if body["modalities"] == ["image"] else (404, err)

    monkeypatch.setattr(lay, "_post_chat", fake_post)
    monkeypatch.setattr(lay, "_api_key", lambda: "k")
    assert asyncio.run(lay.request_image("bytedance-seed/seedream-4.5", "p", [], "9:16")) == (PNG, "image/png")
    assert tried == [["image", "text"], ["image"]]

    tried.clear()  # modelo que aceita os dois: uma chamada só
    monkeypatch.setattr(lay, "_post_chat", lambda key, body: _ok(tried, body, ok))
    assert asyncio.run(lay.request_image("google/gemini-2.5-flash-image", "p", [], "9:16")) == (PNG, "image/png")
    assert tried == [["image", "text"]]

    async def always_404(key, body):  # erro que não é de modalidade não é repetido
        tried.append(1)
        return 404, {"error": {"code": 404, "message": "No such model"}}

    tried.clear()
    monkeypatch.setattr(lay, "_post_chat", always_404)
    try:
        asyncio.run(lay.request_image("x/y", "p", [], "9:16"))
    except ProviderError as e:
        assert "404" in str(e)
    else:
        raise AssertionError("deveria falhar")
    assert tried == [1]


async def _ok(tried, body, ok):
    tried.append(body["modalities"])
    return 200, ok


def test_request_image_retries_with_https_links_when_data_url_refused(monkeypatch, client):
    """O Seed (Seedream) às vezes recusa data URL: a chamada é refeita com links HTTPS temporários, que somem depois."""
    import asyncio

    sent = []
    ok = {"choices": [{"message": {"images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + B64}}]}}]}
    err = {"error": {"code": 400, "message": "Unsupported image URL scheme. Seedream requires base64 data URLs or HTTP/HTTPS URLs."}}
    seen = {}

    async def fake_post(key, body):
        url = body["messages"][0]["content"][1]["image_url"]["url"]
        sent.append(url)
        if url.startswith("data:"):
            return 400, err
        token = url.rsplit("/", 1)[-1]
        r = client.get(f"/api/public/ref/{token}")  # o provedor baixa a imagem sem login
        seen["status"], seen["body"], seen["type"] = r.status_code, r.content, r.headers["content-type"]
        return 200, ok

    monkeypatch.setattr(lay, "_post_chat", fake_post)
    monkeypatch.setattr(lay, "_api_key", lambda: "k")
    monkeypatch.setattr(lay, "_public_base", lambda: "https://os.exemplo.com")
    assert asyncio.run(lay.request_image("bytedance-seed/seedream-4.5", "p", [(PNG, "image/png")], "9:16")) == (PNG, "image/png")
    assert sent[0].startswith("data:image/png;base64,") and sent[1].startswith("https://os.exemplo.com/api/public/ref/")
    assert seen == {"status": 200, "body": PNG, "type": "image/png"}
    assert client.get(f"/api/public/ref/{sent[1].rsplit('/', 1)[-1]}").status_code == 404  # link apagado depois da chamada
    assert client.get("/api/public/ref/curto").status_code == 404

    monkeypatch.setattr(lay, "_public_base", lambda: "")  # sem GOS_PUBLIC_URL: erro claro, sem nova chamada
    sent.clear()
    try:
        asyncio.run(lay.request_image("bytedance-seed/seedream-4.5", "p", [(PNG, "image/png")], "9:16"))
    except ProviderError as e:
        assert "GOS_PUBLIC_URL" in str(e)
    else:
        raise AssertionError("deveria falhar")
    assert len(sent) == 1
