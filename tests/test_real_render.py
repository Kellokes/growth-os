"""Ponta a ponta com o renderizador de verdade: o PNG final de cada página do carrossel tem a foto da página no lugar certo?

Só roda com MOTION_TEST_URL apontando para um servidor /still que renderiza a composição num Chromium real (o mesmo HTML que o
serviço motion recebe). O gerador de imagem é simulado com cores sólidas (uma por chamada): assim dá para medir, nos pixels do
PNG final, onde cada foto foi parar. Sem a variável, o arquivo inteiro é pulado."""
import base64
import colorsys
import hashlib
import io
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MOTION_TEST_URL"), reason="MOTION_TEST_URL não definido")

from PIL import Image, ImageDraw  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import layouts as lay  # noqa: E402
from app import modelcfg  # noqa: E402

from .test_growth import make_projects, pid  # noqa: E402,F401 - fixtures

PROMPTS: dict[tuple, str] = {}


def solid(prompt: str, size=(540, 675)) -> tuple[bytes, str, tuple]:
    h = int(hashlib.md5(prompt.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    rgb = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, 1, 1))
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, "PNG")
    return buf.getvalue(), "image/png", rgb


def art_png(alpha=False) -> bytes:
    im = Image.new("RGBA" if alpha else "RGB", (1080, 1350), (20, 24, 48, 255) if not alpha else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, 1059, 1329], outline=(250, 250, 250, 255), width=12)
    if alpha:  # moldura com janela vazada
        d.rectangle([0, 0, 1079, 1349], outline=(30, 30, 80, 255), width=60)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def fraction(png: bytes, rgb, region=None) -> float:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if region:
        w, h = im.size
        im = im.crop((int(region[0] * w), int(region[1] * h), int(region[2] * w), int(region[3] * h)))
    px = im.getdata()
    return sum(1 for p in px if all(abs(p[i] - rgb[i]) <= 3 for i in range(3))) / len(px)


@pytest.fixture
def real(logged, monkeypatch):
    from app import config
    monkeypatch.setenv("GOS_MOTION_URL", os.environ["MOTION_TEST_URL"])
    config.get_settings.cache_clear()
    make_projects("Zombie Party")
    made: dict[int, tuple] = {}

    async def fake_image(model, prompt, refs, ratio):
        data, mime, rgb = solid(prompt)
        made[len(made)] = (prompt, rgb, len(refs), ratio)
        return data, mime

    monkeypatch.setattr(lay, "request_image", fake_image)
    s = dbmod.new_session()
    modelcfg.save(s, "image", "openrouter", "google/gemini-2.5-flash-image", "")
    s.close()
    logged.made = made
    return logged


def make_art_template(logged, fmt, role, boxes=None, alpha=False):
    b64 = base64.b64encode(art_png(alpha)).decode()
    r = logged.post("/api/layouts/upload", json={"project": "zombie-party", "format": fmt, "role": role, "name": role or fmt,
                                                 "data": b64, "text": "embaixo"})
    assert r.status_code == 200, r.text
    out = r.json()
    if boxes:
        t = logged.post(f"/api/layouts/{out['layout']['id']}/boxes", json={"boxes": boxes})
        assert t.status_code == 200, t.text
        assert logged.post(f"/api/layouts/{t.json()['id']}/default").status_code == 200
        return t.json()["id"]
    return out["template"]["id"]


BOX_TXT = {"titulo": {"x": 6, "y": 6, "w": 88, "h": 10, "max": 40, "size": 60, "align": "center", "color": "#ffffff"},
           "texto": {"x": 6, "y": 17, "w": 88, "h": 8, "max": 80, "size": 34, "align": "left", "color": "#ffffff"}}
BOX_IMG = {**BOX_TXT, "midia": {"x": 10, "y": 40, "w": 80, "h": 50, "radius": 0}}


def piece(mode="template", source="gerar_imagem"):
    s = dbmod.new_session()
    slides = [{"role": "pagina", "title": f"Dica {i}", "text": f"Texto {i}", "media": f"cena da pagina {i}"} for i in (1, 2, 3)]
    slides.append({"role": "cta", "title": "Salve", "text": "Chame", "media": "cena do cta"})
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body="Texto do post", media="festa",
                          visual_format="carrossel", img_title="Capa", img_text="Frase da capa", image_mode=mode,
                          media_source=source, slides=json.dumps(slides), status="pending")
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def add_reference():
    s = dbmod.new_session()
    data, mime, _ = solid("ref")
    s.add(dbmod.LayoutAsset(project_id=pid("zombie-party"), kind="reference", format="", mime=mime, data=data, name="Tabuleiro",
                            category="objeto", note="o tabuleiro"))
    s.commit()
    s.close()


def fetch(logged, url):
    r = logged.get(url)
    assert r.status_code == 200
    return r.content


def report(logged, cid, label):
    item = logged.get(f"/api/content/{cid}").json() if False else next(
        i for i in logged.get("/api/content?project=zombie-party").json()["items"] if i["id"] == cid)
    rows = [("capa", item["image_url"], "")] + [(f"{s['role']}{i}", s["image_url"], "") for i, s in enumerate(item["slides"])]
    s = dbmod.new_session()
    c = s.get(dbmod.ContentItem, cid)
    slides = lay.piece_slides(c)
    out = []
    for (name, url, _), sl in zip(rows, [None] + slides):
        if not url:
            out.append((name, None, None))
            continue
        png = fetch(logged, url)
        photo = s.get(dbmod.LayoutAsset, sl["photo_id"]) if sl and sl.get("photo_id") else None
        rgb = solid(photo.prompt)[2] if photo is not None else None
        if sl is None:  # a capa: a imagem final traz a cor do último fundo gerado
            bg = s.get(dbmod.LayoutAsset, c.bg_id) if c.bg_id else s.get(dbmod.LayoutAsset, c.media_id) if c.media_id else None
            rgb = solid(bg.prompt)[2] if bg is not None and bg.prompt else None
        out.append((name, rgb, round(fraction(png, rgb), 3) if rgb else None))
    s.close()
    print(f"\n== {label}: item {item['id']} erro={item['image_error']!r}")
    for r in out:
        print("  ", r)
    return out


@pytest.mark.parametrize("label,boxes,alpha", [("caixa de imagem", BOX_IMG, False), ("caixas sem imagem", BOX_TXT, False),
                                                 ("arte opaca texto padrão", None, False), ("arte com janela vazada", None, True)])
def test_template_mode_pages(real, label, boxes, alpha):
    for role in ("capa", "pagina", "cta"):
        make_art_template(real, "carrossel", role, boxes, alpha)
    cid = piece()
    r = real.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 200, r.text
    out = report(real, cid, "template/" + label)
    pages = [o for o in out[1:]]
    if label == "caixas sem imagem":  # escolha do owner: sem caixa de imagem não há foto; a tela diz por quê
        det = real.get(f"/api/content/{cid}/slides").json()["slides"]
        assert all(d["image_url"] and not d["kind"] and "caixa de imagem" in d["reason"] for d in det)
        return
    assert all(o[1] is not None and o[2] and o[2] > 0.05 for o in pages), pages


@pytest.mark.parametrize("mode", ["reference", "prompt"])
def test_free_modes_manual_button_makes_pages(real, mode):
    add_reference()
    cid = piece(mode=mode)
    r = real.post(f"/api/content/{cid}/image", json={})
    assert r.status_code == 200, r.text
    item = next(i for i in real.get("/api/content?project=zombie-party").json()["items"] if i["id"] == cid)
    print(f"\n== free/{mode}: capa={bool(item['image_url'])} paginas={[bool(s['image_url']) for s in item['slides']]}")
    assert item["image_url"] and all(s["image_url"] for s in item["slides"])

