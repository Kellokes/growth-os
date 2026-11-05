"""Ativos do projeto (logo, cartas, objetos, vídeos): biblioteca por projeto que entra nas artes e no prompt dos agentes."""
import json

from app import assets as ast
from app import content as cnt
from app import db as dbmod
from app import growth

from .test_growth import make_projects, pid  # noqa: F401
from .test_layout_variants import AI, B64, ai_template, new_piece, studio, PLAIN  # noqa: F401


def add(logged, **kw):
    body = {"project": "zombie-party", "data": B64, **kw}
    r = logged.post("/api/assets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_asset_library_crud_and_validation(studio):
    logo = add(studio, name="Logo", category="logo", note="logo verde", always=True)
    carta = add(studio, name="Carta Zumbi Rei", category="carta", note="carta lendária", use_when="quando falar das cartas")
    vid = studio.post("/api/assets", json={"project": "zombie-party", "link": "https://site.com/trailer.mp4", "name": "Trailer",
                                           "always": True}).json()
    assert vid["is_link"] and vid["category"] == "video" and not vid["always"] and vid["link"].endswith(".mp4")
    assert studio.post("/api/assets", json={"project": "zombie-party", "link": "http://x.com/v.mp4"}).status_code == 422
    assert studio.post("/api/assets", json={"project": "zombie-party"}).status_code == 422  # nem imagem nem link
    assert studio.post("/api/assets", json={"project": "zombie-party", "data": B64, "link": "https://a.com/x"}).status_code == 422
    r = studio.patch(f"/api/assets/{carta['id']}", json={"use_when": "posts do card game", "category": "personagem"})
    assert r.json()["use_when"] == "posts do card game" and r.json()["category"] == "personagem"
    lst = studio.get("/api/assets?project=zombie-party").json()
    assert [a["id"] for a in lst["assets"]][0] == logo["id"] and {c["key"] for c in lst["categories"]} >= {"logo", "carta", "video"}
    assert studio.delete(f"/api/layouts/assets/{vid['id']}").status_code == 200

    # outro projeto não vê nem usa os ativos deste
    make_projects("A Ilha")
    assert studio.get("/api/assets?project=a-ilha").json()["assets"] == []


def test_assets_go_with_the_image_prompt(studio, monkeypatch):
    ai_template(studio, monkeypatch, PLAIN)
    add(studio, name="Logo", category="logo", note="logo verde", always=True)
    carta = add(studio, name="Carta Zumbi Rei", category="carta", note="carta lendária", use_when="quando falar das cartas")
    add(studio, name="Mesa de bar", category="cenario", note="mesa", use_when="happy hour")
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body="Nova expansão de cartas chegando!",
                          visual_format="story", status="pending")
    s.add(c)
    s.commit()
    cid = c.id
    picked = [a.name for a in ast.for_piece(s, c)]
    assert picked == ["Logo", "Carta Zumbi Rei"]  # "sempre" + o que combina com o assunto; a mesa de bar não
    s.close()
    AI.clear()
    assert studio.post(f"/api/content/{cid}/image", json={}).status_code == 200
    assert AI[-1]["refs"] == 3 and "image 2 = Logo" in AI[-1]["prompt"] and "image 3 = Carta Zumbi Rei" in AI[-1]["prompt"]

    # escolha manual na peça substitui o automático
    assert studio.patch(f"/api/content/{cid}", json={"asset_ids": [carta["id"]]}).json()["asset_ids"] == [carta["id"]]
    AI.clear()
    studio.post(f"/api/content/{cid}/image", json={})
    assert AI[-1]["refs"] == 2 and "Logo" not in AI[-1]["prompt"]
    assert studio.patch(f"/api/content/{cid}", json={"asset_ids": [99999]}).status_code == 422
    assert studio.patch(f"/api/content/{cid}", json={"asset_ids": []}).json()["asset_ids"] == []


def test_layout_generation_uses_only_style_assets(studio, monkeypatch):
    add(studio, name="Logo", category="logo", note="logo")
    add(studio, name="Carta", category="carta", note="carta")
    seen = {}

    async def fake(model, prompt, refs, ratio):
        seen["n"] = len(refs)
        return b"\x89PNG\r\n\x1a\n" + b"1" * 30, "image/png"

    import app.layouts as lay

    monkeypatch.setattr(lay, "request_image", fake)
    r = studio.post("/api/layouts/generate", json={"project": "zombie-party", "format": "story", "count": 1})
    assert r.status_code == 200 and seen["n"] == 1


def test_agents_see_catalog_and_pick_assets_by_name(studio):
    add(studio, name="Carta Zumbi Rei", category="carta", note="carta lendária", use_when="posts de cartas")
    studio.post("/api/assets", json={"project": "zombie-party", "link": "https://site.com/trailer.mp4", "name": "Trailer"})
    s = dbmod.new_session()
    text = growth.asset_catalog(s, pid("zombie-party"))
    assert "Carta Zumbi Rei" in text and "posts de cartas" in text and "https://site.com/trailer.mp4" in text
    t = dbmod.Task(project_id=pid("zombie-party"), agent_slug="growth", title="posts", status="done")
    s.add(t)
    s.commit()
    block = {"canal": "instagram", "texto": "Post", "ativos": ["carta zumbi rei", "não existe"]}
    c = cnt.ingest_blocks(s, t, "```conteudo\n" + json.dumps(block) + "\n```")[0]
    assert len(json.loads(c.asset_ids)) == 1
    assert '"ativos"' in cnt.guide()
    s.close()


def test_at_mentions_send_the_asset_where_the_prompt_says(studio, monkeypatch):
    ai_template(studio, monkeypatch, PLAIN)
    add(studio, name="Logo", category="logo", note="logo verde", always=True)
    cartas = add(studio, name="Cartas", category="carta", note="cartas do jogo")
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body="x", visual_format="story",
                          status="pending", image_mode="prompt",
                          image_prompt="Mesa de bar com @cartas espalhadas e @Logo no canto, fale com a@cartas.com e @Nada")
    s.add(c)
    s.commit()
    cid = c.id
    assert [a.name for a in ast.mentioned(s, c)] == ["Cartas", "Logo"]
    assert [a.name for a in ast.for_piece(s, c)] == ["Cartas", "Logo"]
    out = ast.rewrite_mentions(s, c, c.image_prompt, ast.for_piece(s, c), 1)
    assert "cartas (image 1) espalhadas" in out and "Logo (image 2) no canto" in out
    assert "a@cartas.com" in out and "@Nada" in out
    s.close()
    AI.clear()
    assert studio.post(f"/api/content/{cid}/image", json={}).status_code == 200
    assert AI[-1]["refs"] == 2 and "cartas (image 1)" in AI[-1]["prompt"] and "image 1 = Cartas" in AI[-1]["prompt"]
    assert cartas["id"]
