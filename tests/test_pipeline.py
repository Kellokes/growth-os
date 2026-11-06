"""Linha de produção: peça de agente sai inteira (texto, capa, foto e imagem de cada página, CTA) e só a parte que falha é refeita."""
import asyncio
import json

from app import content as cnt
from app import db as dbmod
from app import growth
from app import layouts as lay
from app import pipeline as pipe
from app.providers.base import ProviderError

from .test_content_images import PNG, approve_layout
from .test_growth import make_projects, pid  # noqa: F401 - fixtures

BODY = ("Cinco dicas para uma festa de Halloween sem estresse. Planeje a lista de convidados cedo.\n\n"
        "Escolha um tema e mantenha a decoração simples.")
COPY = {"titulo_imagem": "Halloween sem estresse", "texto_imagem": "Cinco dicas para organizar a sua festa",
        "paginas": [{"titulo": f"Dica {i}", "texto": f"Texto da dica {i}", "imagem": f"PAGE{i} scene"} for i in (1, 2, 3)],
        "cta": {"titulo": "Salve este post", "texto": "Chame os amigos", "imagem": "CTA scene"}}


class FakeProvider:
    id, model = "fake", "fake-1"

    def __init__(self, data=None):
        self.data, self.calls = data if data is not None else COPY, 0

    async def complete(self, history):
        self.calls += 1
        return "claro!\n" + json.dumps(self.data)


def agent_piece(**kw):
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), agent_slug="growth", channel="instagram", body=BODY,
                          title="Halloween", media="festa de halloween", visual_format="carrossel", **kw)
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def setup(logged, monkeypatch, provider=None, fail=()):
    """Layout aprovado com janela {{midia}} (toda página ganha foto), provider de texto e gerador de imagem falsos."""
    make_projects("Zombie Party")
    approve_layout(logged, monkeypatch)
    s = dbmod.new_session()
    t = s.query(dbmod.LayoutAsset).filter(dbmod.LayoutAsset.kind == "template").one()
    t.html = t.html.replace("<small>", "<div>{{midia}}</div><small>")
    s.commit()
    s.close()
    prov = provider or FakeProvider()
    monkeypatch.setattr(growth, "pick_provider", lambda *a, **k: prov)
    seen: list[str] = []
    failing = set(fail)

    async def fake(model, prompt, refs, ratio):
        seen.append(prompt)
        for f in list(failing):
            if f in prompt:
                failing.discard(f)  # falha só na primeira vez
                raise ProviderError(f"sem créditos para {f}")
        return PNG + b"I", "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    return prov, seen


def load(cid):
    s = dbmod.new_session()
    c = s.get(dbmod.ContentItem, cid)
    s.expunge(c)
    s.close()
    return c


def run(coro):
    return asyncio.run(coro)


def test_agent_piece_is_completed_end_to_end(logged, monkeypatch):
    prov, seen = setup(logged, monkeypatch)
    cid = agent_piece()  # só legenda: sem título/texto da capa, sem páginas, sem CTA
    r = logged.get("/api/content?project=zombie-party").json()["items"][0]
    assert r["pipeline"]["state"] == "working" and r["pipeline"]["done"] == 0
    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).status_code == 409  # sem capa ainda

    assert run(lay.autogen_pending()) == 1
    item = logged.get("/api/content?project=zombie-party").json()["items"][0]
    assert item["pipeline"]["state"] == "ready" and item["pipeline"]["label"] == "Pronta para aprovar"
    assert prov.calls == 1  # uma chamada de texto completou tudo
    assert [s["role"] for s in item["slides"]] == ["pagina"] * 3 + ["cta"]
    assert all(s["image_url"] for s in item["slides"]) and item["image_url"]
    assert item["img_title"] == "Halloween" and item["img_text"]  # capa derivada da legenda (grátis)
    assert len(seen) == 1 + 4  # capa + uma foto por página e pelo CTA
    assert any("PAGE2 scene" in p for p in seen) and any("CTA scene" in p for p in seen)
    s = dbmod.new_session()
    slides = json.loads(s.get(dbmod.ContentItem, cid).slides)
    assert all(x.get("photo_id") and x.get("image_id") for x in slides)
    s.close()
    assert run(lay.autogen_pending()) == 0  # nada mais a fazer
    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).json()["status"] == "approved"


def test_only_the_failed_part_is_retried(logged, monkeypatch):
    prov, seen = setup(logged, monkeypatch, fail=("PAGE2 scene",))
    cid = agent_piece()
    run(lay.autogen_pending())
    item = logged.get("/api/content?project=zombie-party").json()["items"][0]
    p = item["pipeline"]
    assert p["state"] == "working" and p["done"] == p["total"] - 1
    bad = [x for x in p["parts"] if not x["ok"]]
    assert [x["label"] for x in bad] == ["Página 3"] and bad[0]["attempts"] == 1 and "sem créditos" in bad[0]["error"]
    assert "Página 3" in item["image_error"]
    r = logged.post(f"/api/content/{cid}/decide", json={"approve": True})
    assert r.status_code == 409 and "Página 3" in r.json()["detail"]  # incompleta não aprova
    n = len(seen)
    assert run(lay.autogen_pending()) == 0  # espera antes de tentar de novo
    assert len(seen) == n

    r = logged.post(f"/api/content/{cid}/complete")  # o owner manda completar: só a foto que falhou volta
    assert r.status_code == 200 and r.json()["pipeline"]["state"] == "ready"
    assert len(seen) == n + 1 and "PAGE2 scene" in seen[-1] and prov.calls == 1
    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).status_code == 200


def test_parts_give_up_after_max_attempts_and_say_what_is_missing(logged, monkeypatch):
    setup(logged, monkeypatch, provider=FakeProvider({"paginas": []}))  # o modelo não ajuda
    monkeypatch.setattr(pipe, "RETRY_DELAY_S", (0, 0, 0))
    cid = agent_piece()
    for _ in range(5):
        run(lay.autogen_pending())
    c = load(cid)
    s = dbmod.new_session()
    st = pipe.check(s, s.get(dbmod.ContentItem, cid))
    s.close()
    copy = next(x for x in st["parts"] if x["key"] == "copy")
    assert st["state"] == "needs_you" and copy["attempts"] == pipe.MAX_ATTEMPTS and "páginas" in copy["detail"]
    assert "Texto" in st["label"] and c.status == "pending"
    assert run(lay.autogen_pending()) == 0  # esgotou: não insiste sozinho


def test_text_and_email_gaps_are_derived_without_ai(logged, monkeypatch):
    prov, _ = setup(logged, monkeypatch)
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), agent_slug="growth", channel="email",
                          body="Proposta de parceria\nOlá, queremos fazer uma ação juntos.", recipient="a@b.com")
    s.add(c)
    s.commit()
    assert pipe.copy_gaps(c) == ["assunto do e-mail"]
    assert pipe.derive_copy(c) and c.title == "Proposta de parceria" and pipe.copy_gaps(c) == []
    c.recipient = ""
    assert pipe.copy_gaps(c) == ["destinatário do e-mail"]  # o sistema não inventa destinatário
    assert pipe.blockers(s, c) and "destinat" in pipe.blockers(s, c)[0]
    s.close()
    assert prov.calls == 0


def test_owner_pieces_are_not_forced_through_the_pipeline(logged, monkeypatch):
    setup(logged, monkeypatch)
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body="x", visual_format="carrossel")
    s.add(c)
    s.commit()
    assert pipe.blockers(s, c) == [] and not any(p["key"] == "copy" for p in pipe.check(s, c)["parts"])
    s.close()


def test_merge_copy_never_overwrites_what_the_agent_wrote(logged, monkeypatch):
    setup(logged, monkeypatch)
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), agent_slug="growth", channel="instagram", body=BODY, title="T",
                          visual_format="carrossel", img_title="Meu título", img_text="",
                          slides=json.dumps([{"role": "pagina", "title": "Meu", "text": ""},
                                             {"role": "cta", "title": "Meu CTA", "text": ""}]))
    pipe.merge_copy(c, COPY)
    slides = json.loads(c.slides)
    assert c.img_title == "Meu título" and c.img_text == COPY["texto_imagem"]
    assert slides[0]["title"] == "Meu" and slides[0]["text"] == "Texto da dica 1" and len(slides) == 4
    assert slides[-1]["role"] == "cta" and slides[-1]["title"] == "Meu CTA" and slides[-1]["text"] == "Chame os amigos"
    s.close()


def test_agent_guide_and_block_carry_per_page_image(logged, monkeypatch):
    assert '"imagem"' in cnt.guide()
    out = json.loads(cnt.block_slides({"paginas": [{"titulo": "A", "texto": "B", "imagem": "praia"}], "cta": {"titulo": "C", "imagem": "sol"}}))
    assert out[0]["media"] == "praia" and out[1]["media"] == "sol"


def test_manual_carousel_image_also_makes_page_photos(logged, monkeypatch):
    """Gerar a imagem pelo botão (não só pela linha de produção) cria a foto de cada página e do CTA e as páginas sobem com ela."""
    setup(logged, monkeypatch)
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body=BODY, title="Halloween", media="festa",
                          visual_format="carrossel", img_title="Capa", img_text="Texto da capa",
                          slides=json.dumps([{"role": "pagina", "title": "Dica 1", "text": "t1", "media": "PAGE1 scene"},
                                             {"role": "cta", "title": "Salve", "text": "t", "media": "CTA scene"}]))
    s.add(c)
    s.commit()
    cid = c.id
    run(lay.generate_piece_image(s, c))
    s.close()
    slides = json.loads(load(cid).slides)
    assert load(cid).image_id and all(x.get("photo_id") and x.get("image_id") for x in slides)


def add_asset(name, note, use_when="", category="artefato", always=False):
    s = dbmod.new_session()
    a = dbmod.LayoutAsset(project_id=pid("zombie-party"), kind="reference", format="", mime="image/png", data=PNG, name=name,
                          note=note, use_when=use_when, category=category, always=always)
    s.add(a)
    s.commit()
    aid = a.id
    s.close()
    return aid


def test_each_page_picks_the_assets_its_text_asks_for(logged, monkeypatch):
    make_projects("Zombie Party")
    board = add_asset("Tabuleiro", "tabuleiro do jogo", "quando falar do tabuleiro")
    cards = add_asset("Cartas", "baralho de cartas do jogo", "quando falar das cartas", category="carta")
    logo = add_asset("Logo", "logo da marca", category="logo", always=True)
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid("zombie-party"), channel="instagram", body="x", visual_format="carrossel")
    pick = lambda text, **kw: [a.id for a in __import__("app.assets", fromlist=["x"]).for_slide(s, c, {"title": text, "text": "", **kw})]  # noqa: E731
    assert pick("Monte o tabuleiro") == [logo, board]
    assert pick("As cartas de ação") == [logo, cards]
    assert pick("Sem assunto") == [logo]
    assert set(pick("Tabuleiro e cartas")) == {logo, board, cards}
    assert pick("Qualquer coisa", assets=[cards]) == [cards, logo]  # o agente indicou: vai primeiro
    s.close()


def test_free_mode_carousel_pages_get_their_own_image(logged, monkeypatch):
    prov, seen = setup(logged, monkeypatch)
    cards = add_asset("Cartas", "baralho de cartas do jogo", "quando falar das cartas", category="carta")
    refs_seen: list[int] = []

    async def fake(model, prompt, refs, ratio):
        seen.append(prompt)
        refs_seen.append(len(refs))
        return PNG + b"F", "image/png"

    monkeypatch.setattr(lay, "request_image", fake)
    cid = agent_piece(image_mode="reference")
    run(lay.autogen_pending())
    item = logged.get("/api/content?project=zombie-party").json()["items"][0]
    assert item["pipeline"]["state"] == "ready", item["pipeline"]
    assert [s["role"] for s in item["slides"]] == ["pagina"] * 3 + ["cta"] and all(s["image_url"] for s in item["slides"])
    assert len(seen) == 5 and all(n >= 1 for n in refs_seen)  # capa + 4, cada uma com referência
    assert logged.post(f"/api/content/{cid}/decide", json={"approve": True}).status_code == 200


def test_agent_page_ativos_by_name_are_resolved(logged, monkeypatch):
    make_projects("Zombie Party")
    board = add_asset("Tabuleiro", "tabuleiro")
    s = dbmod.new_session()
    out = json.loads(cnt.block_slides({"paginas": [{"titulo": "A", "texto": "B", "ativos": ["tabuleiro", "nao existe"]}]}, s, pid("zombie-party")))
    s.close()
    assert out[0]["assets"] == [board]


def test_page_windows_get_photos_even_when_cover_media_is_uploaded(logged, monkeypatch):
    """Capa com mídia subida (não gerada): a janela de foto de cada página não pode ficar vazia."""
    prov, seen = setup(logged, monkeypatch)
    cid = agent_piece(media_source="upload")
    s = dbmod.new_session()
    c = s.get(dbmod.ContentItem, cid)
    from app import media as med
    med.set_media(s, c, PNG, "image/png", "upload", "")
    s.commit()
    s.close()
    run(lay.autogen_pending())
    item = logged.get("/api/content?project=zombie-party").json()["items"][0]
    assert item["pipeline"]["state"] == "ready", item["pipeline"]
    slides = json.loads(load(cid).slides)
    assert all(x.get("photo_id") and x.get("image_id") for x in slides)
