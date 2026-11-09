import base64
import io
import zipfile

from app import content as cnt
from app import db as dbmod
from app import media as med

from .test_growth import make_projects  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 40
JPG = b"\xff\xd8\xff" + b"2" * 40
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"3" * 40

CSV = """projeto;canal;tipo;quando;texto;titulo;arquivos;conta
{p};instagram;carrossel;2030-01-10 18:30;"Legenda 1
com quebra";Carrossel;c1.jpg|c2.jpg|c3.png;Take One
{p};linkedin|x;texto;2030-01-11 09:00;Só texto;;;
{p};instagram;reel;2030-01-12 10:00;Reel;;r1.mp4;
{p};instagram;post;2030-01-13 10:00;Foto;;f.png;
"""


def zipb(files: dict, csv: str | None = CSV, name="posts.csv") -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if csv is not None:
            z.writestr(name, csv.encode())
        for k, v in files.items():
            z.writestr(k, v)
    return base64.b64encode(buf.getvalue()).decode()


FILES = {"c1.jpg": JPG, "c2.jpg": JPG, "c3.png": PNG, "r1.mp4": MP4, "f.png": PNG}


def test_import_preview_then_commit(logged):
    slug = make_projects("Take One")[0]
    z = zipb(FILES, CSV.format(p=slug))
    prev = logged.post("/api/content-import", json={"data": z}).json()
    assert prev["ok"] and prev["to_create"] == 5 and prev["created"] == 0  # linkedin|x gera 2 peças
    s = dbmod.new_session()
    assert s.query(dbmod.ContentItem).count() == 0  # prévia não grava
    s.close()

    done = logged.post("/api/content-import", json={"data": z, "confirm": True}).json()
    assert done["created"] == 5
    s = dbmod.new_session()
    rows = s.query(dbmod.ContentItem).order_by(dbmod.ContentItem.id).all()
    car = rows[0]
    assert car.status == "approved" and car.channel == "instagram" and car.account == "Take One"
    assert car.scheduled_at.isoformat() == "2030-01-10T18:30:00" and "com quebra" in car.body
    assert len(cnt.media_asset_ids(car)) == 3
    assert {r.channel for r in rows[1:3]} == {"linkedin", "x"} and rows[1].visual_format == ""
    reel = rows[3]
    assert reel.visual_format == "reel" and reel.video_id and not reel.image_id
    assert not cnt.needs_image(reel)
    assert rows[4].visual_format == "" and rows[4].image_id
    s.close()

    # subir de novo não duplica
    again = logged.post("/api/content-import", json={"data": z, "confirm": True}).json()
    assert again["created"] == 0 and again["duplicates"] == 5


def test_import_reports_row_errors_and_blocks_commit(logged):
    slug = make_projects("Take One")[0]
    bad = f"""projeto;canal;tipo;quando;texto;titulo;arquivos;conta
{slug};instagram;carrossel;2030-01-10 18:30;ok;;c1.jpg;
{slug};instagram;post;2001-01-01 10:00;passado;;f.png;
{slug};email;texto;2030-01-10 10:00;mail;;;
{slug};instagram;post;2030-01-10 10:00;falta arquivo;;nao.png;
nope;instagram;texto;;sem data;;;
"""
    z = zipb(FILES, bad)
    prev = logged.post("/api/content-import", json={"data": z}).json()
    assert not prev["ok"] and prev["errors"] == 5
    errs = [" ".join(r["errors"]) for r in prev["rows"]]
    assert "2 a 9 imagens" in errs[0] and "já passou" in errs[1] and "sem envio automático" in errs[2]
    assert "não está no ZIP" in errs[3] and "projeto não encontrado" in errs[4] and "falta a data" in errs[4]
    r = logged.post("/api/content-import", json={"data": z, "confirm": True})
    assert r.status_code == 422
    s = dbmod.new_session()
    assert s.query(dbmod.ContentItem).count() == 0
    s.close()


def test_import_rejects_bad_zip_and_needs_login(client, logged):
    assert logged.post("/api/content-import", json={"data": base64.b64encode(b"nao e zip").decode()}).status_code == 422
    assert logged.post("/api/content-import", json={"data": zipb({}, None)}).status_code == 422  # sem posts.csv
    p = logged.get("/api/content-import/prompt").json()["prompt"]
    assert "posts.csv" in p and "projeto;canal;tipo;quando" in p
    client.headers.pop("X-CSRF-Token", None)
    client.cookies.clear()
    assert client.post("/api/content-import", json={"data": ""}).status_code in (401, 403)


def test_json_manifest_and_default_project(logged):
    slug = make_projects("Take One")[0]
    import json as _j
    man = _j.dumps([{"canal": "instagram", "tipo": "post", "quando": "2030-02-01 10:00", "legenda": "oi", "imagens": ["f.png"]}])
    z = zipb({"f.png": PNG}, man, name="pasta/posts.json")
    out = logged.post("/api/content-import", json={"data": z, "project": slug, "confirm": True}).json()
    assert out["created"] == 1


def test_media_check_is_used():
    assert med.is_video(med.check_media(MP4))
