"""Editor de caixas: o owner marca na arte onde vão título, texto e a imagem da IA; limites de caracteres por caixa."""
from app import db as dbmod
from app import growth
from app import layouts as lay

from .test_growth import pid  # noqa: F401 - fixtures
from .test_layout_variants import AI, B64, RENDERS, new_piece, studio, upload_template  # noqa: F401 - fixtures

BOXES = {
    "titulo": {"x": 6, "y": 20, "w": 88, "h": 10, "max": 30, "size": 64, "align": "center", "color": "#f5d27a", "weight": 800},
    "texto": {"x": 6, "y": 31, "w": 88, "h": 8, "max": 50, "size": 34, "align": "left", "color": "#ffffff", "valign": "top"},
    "midia": {"x": 10, "y": 45, "w": 80, "h": 40, "radius": 24},
}


def art_id(studio, **kw):
    return upload_template(studio, **kw)["layout"]["id"]


def test_clean_boxes_validates_and_clamps():
    b = lay.clean_boxes({"titulo": {"x": -5, "y": 200, "w": 500, "h": 0, "max": 9999, "size": 2, "align": "x",
                                    "color": "red;}", "weight": 900}, "texto": {}, "midia": "não"})
    assert b["titulo"] == {"x": 0, "y": 95, "w": 100, "h": 3, "max": 400, "size": 16, "align": "center", "valign": "middle",
                           "color": "#ffffff", "weight": 800}
    assert "midia" not in b and b["texto"]["max"] == 140
    for bad in ({}, {"titulo": "x"}, "x"):
        try:
            lay.clean_boxes(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    html = lay.box_template("story", lay.clean_boxes(BOXES))
    assert lay.sanitize_template(html) and "{{midia}}" in html and "left: 10.0%" in html and "color: #f5d27a" in html


def test_fit_text_cuts_on_word():
    assert lay.fit_text("curto", 10) == "curto"
    assert lay.fit_text("As cartas de cada jogador vão no próprio celular", 30) == "As cartas de cada jogador vão…"
    assert len(lay.fit_text("x" * 50, 10)) == 10


def test_editor_from_art_makes_approved_template_and_edits_in_place(studio):
    aid = art_id(studio)
    r = studio.post(f"/api/layouts/{aid}/boxes", json={"boxes": BOXES})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "approved" and t["model"] == "editor" and t["media_slot"] and t["boxes"]["titulo"]["max"] == 30
    assert len(RENDERS[-1]["title"]) <= 30 and not AI  # prévia com texto de exemplo no limite, sem IA

    edited = {**BOXES, "titulo": {**BOXES["titulo"], "max": 40}}
    edited.pop("midia")
    t2 = studio.post(f"/api/layouts/{t['id']}/boxes", json={"boxes": edited}).json()
    assert t2["id"] == t["id"] and t2["boxes"]["titulo"]["max"] == 40 and not t2["media_slot"]
    assert studio.post(f"/api/layouts/{aid}/boxes", json={"boxes": {}}).status_code == 422


def test_boxes_are_optional_but_at_least_one():
    only_img = lay.clean_boxes({"midia": BOXES["midia"]})
    assert list(only_img) == ["midia"] and lay.text_limits(None) == lay.DEFAULT_LIMITS
    html = lay.box_template("reel", only_img)
    assert "{{midia}}" in html and 'class="bx t"' not in html and 'class="bx s"' not in html
    only_title = lay.clean_boxes({"titulo": BOXES["titulo"]})
    assert list(only_title) == ["titulo"] and "{{texto}}" not in lay.box_template("story", only_title)


def test_editor_saves_layout_without_text_boxes(studio):
    aid = art_id(studio)
    t = studio.post(f"/api/layouts/{aid}/boxes", json={"boxes": {"midia": BOXES["midia"]}})
    assert t.status_code == 200, t.text
    assert list(t.json()["boxes"]) == ["midia"] and t.json()["media_slot"]


def test_ai_photo_goes_only_into_image_box(studio):
    aid = art_id(studio)
    tid = studio.post(f"/api/layouts/{aid}/boxes", json={"boxes": BOXES}).json()["id"]
    studio.post(f"/api/layouts/{tid}/default")
    s = dbmod.new_session()
    s.add(dbmod.LayoutAsset(project_id=pid("zombie-party"), kind="reference", mime="image/png", data=b"x", name="Carta",
                            category="carta", always=1))
    s.commit()
    s.close()
    cid = new_piece(img_text="As cartas de cada jogador vão no próprio celular, ninguém vê")
    AI.clear()
    RENDERS.clear()
    r = studio.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 200, r.text
    assert len(AI) == 1 and AI[0]["ratio"] == "1:1" and AI[0]["refs"] == 1  # caixa 864x768 -> 1:1; a arte não vai junto
    assert "ONLY the photo" in AI[0]["prompt"] and "ONE single continuous scene" in AI[0]["prompt"] and "never draw their text" in AI[0]["prompt"]
    last = RENDERS[-1]
    assert last["media"] and last["bg"] and '<div class="photo"><img class="m-media"' in last["html"]
    assert "As cartas de cada jogador vão no próprio celular…" in last["html"]  # limite 50, sem partir palavra
    item = r.json()
    assert item["media_source"] == "gerar_imagem" and item["image_url"]

    # sem caixa de imagem: só arte e texto, sem custo
    no_photo = {k: v for k, v in BOXES.items() if k != "midia"}
    studio.post(f"/api/layouts/{tid}/boxes", json={"boxes": no_photo})
    AI.clear()
    assert studio.post(f"/api/content/{cid}/image", json={}).status_code == 200
    assert not AI and not RENDERS[-1]["media"]


def test_agents_get_text_limits(studio):
    aid = art_id(studio)
    tid = studio.post(f"/api/layouts/{aid}/boxes", json={"boxes": BOXES}).json()["id"]
    studio.post(f"/api/layouts/{tid}/default")
    s = dbmod.new_session()
    try:
        brief = growth.asset_catalog(s, pid("zombie-party"))
    finally:
        s.close()
    assert "Story: título até 30, texto até 50" in brief


def test_upload_goes_into_layout_or_replaces_without_one(studio):
    cid = new_piece()
    r = studio.post(f"/api/content/{cid}/image/upload", json={"data": B64})  # sem template: substitui como está
    assert r.status_code == 200 and r.json()["image_url"] and not r.json()["media_file_url"]

    aid = art_id(studio)
    tid = studio.post(f"/api/layouts/{aid}/boxes", json={"boxes": BOXES}).json()["id"]
    studio.post(f"/api/layouts/{tid}/default")
    AI.clear()
    RENDERS.clear()
    item = studio.post(f"/api/content/{cid}/image/upload", json={"data": B64}).json()
    assert item["media_source"] == "upload" and item["media_file_url"] and item["image_url"]
    assert not AI and RENDERS[-1]["media"] and '<div class="photo"><img class="m-media"' in RENDERS[-1]["html"]
