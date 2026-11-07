"""Imagem própria em cada página do carrossel: todos os modos, prompt e referências por página, regenerar só uma página."""
import asyncio
import io
import json

from app import db as dbmod
from app import layouts as lay
from app import motion

from .test_layout_variants import PNG, new_piece
from .test_growth import make_projects, pid  # noqa: F401 - fixtures
from .test_layout_variants import AI, B64, RENDERS, studio, upload_template  # noqa: F401 - fixtures

SLIDES = [{"role": "pagina", "title": "Monte o tabuleiro", "text": "Peças no centro", "media": "close no tabuleiro"},
          {"role": "pagina", "title": "Sobreviva", "text": "Fuja"}, {"role": "cta", "title": "Salve", "text": ""}]


def carousel(mode="template", **kw):
    cid = new_piece("carrossel", image_mode=mode, slides=json.dumps(SLIDES), **kw)
    return cid


def add_ref(name="Tabuleiro"):
    s = dbmod.new_session()
    s.add(dbmod.LayoutAsset(project_id=pid("zombie-party"), kind="reference", mime="image/png", data=PNG, name=name,
                            category="objeto", note=f"imagem {name.lower()}", tags=name.lower()))
    s.commit()
    s.close()


def slides_of(cid):
    s = dbmod.new_session()
    try:
        return json.loads(s.get(dbmod.ContentItem, cid).slides)
    finally:
        s.close()


def test_free_modes_make_every_page_with_the_manual_button(studio):
    add_ref()
    for mode in ("reference", "prompt"):
        AI.clear()
        cid = carousel(mode)
        r = studio.post(f"/api/content/{cid}/image", json={})
        assert r.status_code == 200, r.text
        item = r.json()
        assert item["image_url"] and all(s["image_url"] for s in item["slides"]) and len(item["slides"]) == 3
        assert len(AI) == 1 + 3  # capa + uma imagem por página e CTA
        assert any("close no tabuleiro" in a["prompt"] for a in AI)


def test_opaque_art_without_window_gets_scenery_inside_each_page(studio):
    for role in ("capa", "pagina", "cta"):
        upload_template(studio, "carrossel", role, role)  # arte opaca, "texto padrão": sem janela
    cid = carousel(media_source="gerar_imagem")
    r = studio.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 200, r.text
    slides = slides_of(cid)
    assert all(x.get("photo_id") and x.get("image_id") and x.get("photo_full") for x in slides)
    scen = [a for a in AI if "one page of a carousel" in a["prompt"]]
    assert len(scen) == 3 and all(a["refs"] >= 1 for a in scen)  # a arte da página vai de referência
    assert "close no tabuleiro" in scen[0]["prompt"]


def test_slide_prompt_refs_and_single_regeneration(studio):
    add_ref("Tabuleiro")
    add_ref("Cartas")
    for role in ("capa", "pagina", "cta"):
        upload_template(studio, "carrossel", role, role)
    cid = carousel()
    assert studio.post(f"/api/content/{cid}/slides/0/image", json={}).status_code == 409  # sem capa ainda
    studio.post(f"/api/content/{cid}/image", json={})
    det = studio.get(f"/api/content/{cid}/slides").json()["slides"]
    assert len(det) == 3 and det[0]["status"] == "ok" and det[0]["default_prompt"] and det[0]["prompt"] == ""
    assert [r["name"] for r in det[0]["refs"]] == ["Tabuleiro"] and det[0]["refs"][0]["auto"]  # citou tabuleiro: vem o tabuleiro

    # prompt do owner + referências escolhidas: valem só para essa página
    ids = {a.name: a.id for a in _assets()}
    r = studio.put(f"/api/content/{cid}/slides/0", json={"prompt": "Meu prompt exato", "assets": [ids["Cartas"]], "auto_assets": False})
    assert r.status_code == 200 and r.json()["slides"][0]["prompt"] == "Meu prompt exato"
    assert [x["name"] for x in r.json()["slides"][0]["refs"]] == ["Cartas"] and r.json()["slides"][0]["manual_assets"]
    before = len(AI)
    other_img = studio.get(f"/api/content/{cid}/slides").json()["slides"][1]["image_url"]
    r = studio.post(f"/api/content/{cid}/slides/0/image", json={"prompt": "Meu prompt exato"})
    assert r.status_code == 200, r.text
    assert len(AI) == before + 1 and AI[-1]["prompt"] == "Meu prompt exato" and AI[-1]["refs"] == 1 + 1  # arte + Cartas
    assert r.json()["slides"][1]["image_url"] == other_img  # a outra página não foi refeita
    assert r.json()["slides"][0]["used_prompt"] == "Meu prompt exato"
    # prompt igual ao montado não vira edição do owner
    d = studio.put(f"/api/content/{cid}/slides/0", json={"prompt": ""}).json()["slides"][0]
    assert d["prompt"] == "" and studio.put(f"/api/content/{cid}/slides/9", json={}).status_code == 404


def _assets():
    s = dbmod.new_session()
    try:
        rows = s.query(dbmod.LayoutAsset).filter_by(kind="reference").all()
        s.expunge_all()
        return rows
    finally:
        s.close()


def test_big_images_are_shrunk_before_the_motion_service():
    from PIL import Image

    big = io.BytesIO()
    Image.effect_noise((2400, 3000), 80).convert("RGB").save(big, "PNG")
    assert len(big.getvalue()) > motion.INLINE_LIMIT
    data, mime = motion.shrink_image(big.getvalue(), "image/png", "carrossel")
    assert mime == "image/jpeg" and len(data) < motion.INLINE_LIMIT and Image.open(io.BytesIO(data)).size[0] <= 1080
    small = io.BytesIO()
    Image.new("RGB", (10, 10)).save(small, "PNG")
    assert motion.shrink_image(small.getvalue(), "image/png", "carrossel") == (small.getvalue(), "image/png")


def test_moderation_block_is_explained_and_not_retried(studio, monkeypatch):
    from app import pipeline as pipe
    from app.providers.base import ProviderError
    from app.providers.openrouter import _error_detail, _explain

    code, msg = _error_detail({"error": {"code": 400, "message": "Black Forest Labs blocked this request through content moderation.",
                                         "metadata": {"provider_name": "Black Forest Labs"}}})
    err = _explain(400, msg, "black-forest-labs/flux.2-pro")
    assert err.moderated and "filtro de conteúdo" in str(err) and "outro modelo de imagem" in str(err)
    assert not _explain(500, "boom", "m").moderated

    for role in ("capa", "pagina", "cta"):
        upload_template(studio, "carrossel", role, role)
    cid = carousel()
    studio.post(f"/api/content/{cid}/image", json={})
    calls = []

    async def blocked(model, prompt, refs, ratio):
        calls.append(1)
        raise err

    monkeypatch.setattr(lay, "request_image", blocked)
    s = dbmod.new_session()
    c = s.get(dbmod.ContentItem, cid)
    slides = json.loads(c.slides)
    for x in slides:
        x.pop("photo_id", None), x.pop("image_id", None)
    c.slides = json.dumps(slides)
    c.status = "pending"
    s.commit()
    try:
        asyncio.run(pipe.run_piece(s, c, force=True))
        n = len(calls)
        asyncio.run(pipe.run_piece(s, c))
        assert len(calls) == n  # sem nova tentativa: o bloqueio não some repetindo
        st = pipe.check(s, c)
        assert any("filtro de conteúdo" in p["error"] and p["attempts"] >= pipe.MAX_ATTEMPTS for p in st["parts"])
    finally:
        s.close()
