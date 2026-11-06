"""Ativos do projeto: a biblioteca de referências que entra nas artes (logo, artefatos, cartas do jogo, personagens, vídeos do site).

Cada ativo é uma linha de layout_assets com kind="reference" (as referências antigas do estúdio de layouts viram ativos sem
categoria). O owner diz o que é (note), quando usar (use_when) e se vai em toda arte (always, ex.: a logo).

Na hora de gerar a imagem de uma peça, entram como imagens de referência, com a descrição de cada uma no prompt:
  - os escolhidos na peça (content_items.asset_ids), ou, sem escolha,
  - os marcados "sempre" + os que combinam com o assunto da peça (nome ou "quando usar" aparece no texto).
Vídeos (links) não vão para o modelo de imagem: ficam no catálogo dos agentes e podem virar a mídia da peça (fonte "link").
Tudo é por projeto: nunca entra ativo de outro projeto.
"""
import json
import re
import unicodedata

from .db import ContentItem, LayoutAsset

CATEGORIES: dict[str, str] = {
    "logo": "Logo",
    "artefato": "Artefato / objeto",
    "carta": "Carta do jogo",
    "personagem": "Personagem",
    "cenario": "Cenário / local",
    "produto": "Produto",
    "estilo": "Estilo / paleta",
    "video": "Vídeo (link)",
    "texto": "Texto",
    "outro": "Outro",
}
MAX_ASSETS = 60  # por projeto
MAX_SENT = 5  # ativos que vão junto de cada imagem (além da arte do layout)
LINK_MIME = "text/uri-list"
TEXT_MIME = "text/plain"  # ativo de texto: briefing, regra da marca, frase fixa... (o conteúdo fica em data)
MAX_TEXT = 4000
_STOP = {"para", "com", "sobre", "quando", "falar", "usar", "use", "isso", "este", "esta", "como", "mais", "jogo", "post",
         "posts", "sempre", "qualquer", "deve", "pode", "the", "and", "with", "from", "that", "this"}


def norm_category(raw, is_link: bool = False, is_text: bool = False) -> str:
    c = str(raw or "").strip().lower()
    if is_link:
        return "video"
    if is_text:
        return "texto"
    return c if c in CATEGORIES and c not in ("video", "texto") else "outro"


def is_link(a: LayoutAsset) -> bool:
    return a.mime == LINK_MIME or bool(a.url)


def is_text(a: LayoutAsset) -> bool:
    return a.mime == TEXT_MIME


def is_image(a: LayoutAsset) -> bool:
    return not is_link(a) and not is_text(a)


def text_of(a: LayoutAsset) -> str:
    return bytes(a.data or b"").decode("utf-8", "replace").strip()


def label(a: LayoutAsset) -> str:
    return a.name or a.note[:40] or f"ativo #{a.id}"


def asset_json(a: LayoutAsset) -> dict:
    link = is_link(a)
    txt = is_text(a)
    return {
        "id": a.id, "name": a.name or "", "category": a.category or ("video" if link else "texto" if txt else "estilo"),
        "category_name": CATEGORIES.get(a.category or ("video" if link else "texto" if txt else "estilo"), "Outro"),
        "note": a.note or "", "use_when": a.use_when or "", "always": bool(a.always),
        "link": a.url if link else "", "url": "" if link or txt else f"/api/layouts/assets/{a.id}", "is_link": link, "is_text": txt,
        "text": text_of(a) if txt else "",
        "created_at": a.created_at.isoformat(),
    }


def project_assets(db, project_id: int) -> list[LayoutAsset]:
    return (db.query(LayoutAsset).filter(LayoutAsset.project_id == project_id, LayoutAsset.kind == "reference")
            .order_by(LayoutAsset.always.desc(), LayoutAsset.id).all())


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def _keywords(a: LayoutAsset) -> set[str]:
    cat = CATEGORIES.get(a.category or "", "")
    words = re.findall(r"[a-z0-9]{4,}", _fold(f"{a.name} {a.use_when} {a.note} {cat}"))
    return {w for w in words if w not in _STOP}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w  # cartas -> carta, tabuleiros -> tabuleiro


def chosen_ids(c: ContentItem) -> list[int]:
    try:
        v = json.loads(c.asset_ids or "")
    except ValueError:
        return []
    return [int(x) for x in v if isinstance(x, int)] if isinstance(v, list) else []


def _fold_map(text: str) -> tuple[str, list[int]]:
    """Texto sem acento e minúsculo, com o índice de cada letra no texto original (para trocar trechos sem perder o resto)."""
    out, idx = [], []
    for i, ch in enumerate(text or ""):
        for f in unicodedata.normalize("NFKD", ch.lower()):
            if not unicodedata.combining(f):
                out.append(f)
                idx.append(i)
    return "".join(out), idx


def find_mentions(db, project_id: int, text: str) -> list[tuple[int, int, LayoutAsset]]:
    """@Nome no texto -> (início, fim, ativo) neste projeto, em ordem de aparição. O nome pode ter espaços (acha o mais longo)."""
    folded, idx = _fold_map(text)
    named = sorted((a for a in project_assets(db, project_id) if a.name.strip()), key=lambda a: -len(a.name.strip()))
    found: list[tuple[int, int, LayoutAsset]] = []
    pos = 0
    while True:
        at = folded.find("@", pos)
        if at < 0:
            break
        pos = at + 1
        if at and (folded[at - 1].isalnum() or folded[at - 1] == "."):  # e-mail, não menção
            continue
        for a in named:
            n = _fold_map(a.name.strip())[0]
            end = at + 1 + len(n)
            if folded[at + 1:end] == n and not (end < len(folded) and folded[end].isalnum()):
                found.append((idx[at], idx[end - 1] + 1, a))
                pos = end
                break
    return found


def mentioned(db, c: ContentItem) -> list[LayoutAsset]:
    """Ativos de imagem citados com @ na mídia ou no prompt da peça, sem repetir, na ordem em que aparecem (máx. MAX_SENT)."""
    out: list[LayoutAsset] = []
    # "Mídia" (o que a imagem deve mostrar, com layout) e "Prompt da imagem" (sem layout): os dois aceitam @
    for _, _, a in find_mentions(db, c.project_id, f"{c.media or ''}\n{c.image_prompt or ''}"):
        if is_image(a) and a not in out:
            out.append(a)
    return out[:MAX_SENT]


def rewrite_mentions(db, c: ContentItem, prompt: str, assets: list[LayoutAsset], first_index: int = 1) -> str:
    """Troca @Nome por "Nome (image N)", o N da imagem de referência que de fato vai junto; sem imagem, só tira o @."""
    spots = find_mentions(db, c.project_id, prompt)
    if not spots:
        return prompt
    pos = {a.id: first_index + i for i, a in enumerate(assets)}
    out, last = [], 0
    for start, end, a in spots:
        out.append(prompt[last:start])
        shown = prompt[start + 1:end]
        if a.id in pos:
            out.append(f"{shown} (image {pos[a.id]})")
        elif is_text(a):  # texto: entra inteiro, no ponto citado
            out.append(f"{shown} (\"{text_of(a)[:MAX_TEXT]}\")")
        elif is_link(a):
            out.append(f"{shown} (video: {a.url})")
        else:
            out.append(shown)
        last = end
    out.append(prompt[last:])
    return "".join(out)


def for_piece(db, c: ContentItem, limit: int = MAX_SENT) -> list[LayoutAsset]:
    """Ativos de imagem que entram na arte desta peça: os citados com @ no prompt (na ordem), depois os escolhidos,
    ou, sem escolha, "sempre" + os que combinam com o assunto."""
    rows = [a for a in project_assets(db, c.project_id) if is_image(a)]
    first = mentioned(db, c)
    ids = chosen_ids(c)
    if ids:
        by_id = {a.id: a for a in rows}
        rest = [by_id[i] for i in ids if i in by_id]
        return (first + [a for a in rest if a not in first])[:limit]
    subject = " ".join([c.title, c.body, c.media, c.img_title, c.img_text, c.image_prompt])
    picked = for_subject(db, c.project_id, subject, limit=MAX_SENT)
    return (first + [a for a in picked if a not in first])[:limit]


def for_subject(db, project_id: int, text: str, forced: list[int] | None = None, limit: int = MAX_SENT) -> list[LayoutAsset]:
    """Ativos de imagem certos para UM assunto (uma página, o CTA): os escolhidos de antemão (forced, na ordem), os "sempre" e os que
    o assunto cita (nome, "quando usar", descrição ou tipo: falou em tabuleiro, vem o tabuleiro; falou em cartas, vêm as cartas).
    Quem combina com mais palavras vem primeiro."""
    rows = [a for a in project_assets(db, project_id) if is_image(a)]
    by_id = {a.id: a for a in rows}
    out = [by_id[i] for i in (forced or []) if i in by_id]
    words = {_stem(w) for w in re.findall(r"[a-z0-9]{4,}", _fold(text))}
    scored = []
    for a in rows:
        if a in out or a.always:
            continue
        hits = len({_stem(k) for k in _keywords(a)} & words)
        if hits:
            scored.append((-hits, a.id, a))
    out += [a for a in rows if a.always and a not in out]
    out += [a for _, _, a in sorted(scored)]
    return out[:limit]


def slide_ids(s: dict) -> list[int]:
    v = s.get("assets")
    return [int(x) for x in v if isinstance(x, int)] if isinstance(v, list) else []


def for_slide(db, c: ContentItem, s: dict, limit: int = MAX_SENT) -> list[LayoutAsset]:
    """Ativos da página: os que o agente indicou para ela, os citados com @ ou escolhidos na peça e o que o texto dela pede."""
    forced = slide_ids(s) + [a.id for a in mentioned(db, c)] + chosen_ids(c)
    forced = list(dict.fromkeys(forced))
    text = " ".join(str(x) for x in (s.get("media"), s.get("title"), s.get("text")) if x)
    return for_subject(db, c.project_id, text or c.media or c.title, forced, limit)


def prompt_lines(assets: list[LayoutAsset], first_index: int = 1) -> str:
    """Descrição dos ativos anexados, na ordem das imagens, para o modelo saber o que é cada um e como usar."""
    if not assets:
        return ""
    parts = []
    for i, a in enumerate(assets, start=first_index):
        what = a.note.strip() or CATEGORIES.get(a.category, "")
        use = f" Use it: {a.use_when.strip()[:200]}." if a.use_when.strip() else ""
        parts.append(f"image {i} = {label(a)} ({CATEGORIES.get(a.category, 'asset')}): {what[:200]}.{use}")
    return ("Brand assets attached (reproduce them faithfully when they appear, never invent a different logo, card or "
            "object): " + " | ".join(parts))


def text_lines(assets: list[LayoutAsset]) -> str:
    """Para modelos que não recebem imagem (vídeo): só o nome e a descrição."""
    return "; ".join(f"{label(a)}: {a.note.strip()[:160]}" for a in assets if a.note.strip() or a.name)


def resolve_names(db, project_id: int, names) -> str:
    """Nomes ou ids que o agente mandou em "ativos" -> JSON de ids deste projeto (o que não existe é ignorado)."""
    if not isinstance(names, list):
        return ""
    rows = project_assets(db, project_id)
    by_name = {_fold(a.name): a.id for a in rows if a.name}
    ids = {a.id for a in rows}
    out: list[int] = []
    for n in names[:MAX_SENT]:
        aid = n if isinstance(n, int) and n in ids else by_name.get(_fold(str(n)).strip())
        if aid and aid not in out:
            out.append(aid)
    return json.dumps(out) if out else ""


def catalog(db, project_id: int) -> str:
    """Bloco do prompt dos agentes: o que existe na biblioteca deste projeto e quando usar."""
    rows = project_assets(db, project_id)
    if not rows:
        return ""
    lines = []
    for a in rows[:40]:
        extra = f" · link: {a.url}" if is_link(a) else ""
        if is_text(a):
            extra = f" · texto: {text_of(a)[:160]}"
        when = f" · quando usar: {a.use_when.strip()[:160]}" if a.use_when.strip() else ""
        always = " · vai em toda arte" if a.always else ""
        lines.append(f"- {label(a)} [{CATEGORIES.get(a.category, 'Outro')}]: {a.note.strip()[:160]}{when}{always}{extra}")
    return ("ATIVOS DO PROJETO (biblioteca do owner, use pelo nome exato em \"ativos\" no bloco conteudo; vídeos podem ir em "
            "\"link_midia\" com \"fonte_midia\": \"link\"):\n" + "\n".join(lines))
