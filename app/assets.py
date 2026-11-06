"""Ativos do projeto: a biblioteca de referências que entra nas artes (logo, artefatos, cartas do jogo, personagens, vídeos do site).

Cada ativo é uma linha de layout_assets com kind="reference" (as referências antigas do estúdio de layouts viram ativos sem
categoria). O owner diz o que é (note), quando usar (use_when) e se vai em toda arte (always, ex.: a logo).

Na hora de gerar a imagem de uma peça, entram como imagens de referência, com a descrição de cada uma no prompt:
  - os escolhidos na peça (content_items.asset_ids), ou, sem escolha,
  - os marcados "sempre" + os que combinam com o assunto da peça (nome ou "quando usar" aparece no texto).
Vídeos (links) não vão para o modelo de imagem: ficam no catálogo dos agentes e podem virar a mídia da peça (fonte "link").
Tudo é por projeto: nunca entra ativo de outro projeto.
"""
import hashlib
import json
import re
import unicodedata

from . import calllog
from .db import ContentItem, GrowthProject, LayoutAsset
from .providers import ChatTurn, ProviderError

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
        "note": a.note or "", "use_when": a.use_when or "", "tags": a.tags or "", "always": bool(a.always),
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


# Grupos de palavras que significam a mesma coisa (PT/EN, singular/plural): "tabuleiro" no post casa com um ativo "Board".
_SYN = [
    {"tabuleiro", "board", "tabuleiros", "mapa", "map"},
    {"carta", "cartas", "card", "cards", "baralho", "deck"},
    {"personagem", "personagens", "character", "characters", "heroi", "herois", "vilao"},
    {"dado", "dados", "dice", "die"},
    {"peca", "pecas", "token", "tokens", "pino", "pawn", "miniatura"},
    {"logo", "logotipo", "marca", "logomarca"},
    {"cenario", "cenarios", "local", "scenery", "ambiente"},
    {"caixa", "box", "embalagem", "packaging"},
    {"produto", "produtos", "product"},
    {"regra", "regras", "rules", "manual"},
]
_SYN_OF = {w: i for i, grp in enumerate(_SYN) for w in grp}
_CATEGORY_WORDS = {"logo": "logo", "carta": "carta", "personagem": "personagem", "cenario": "cenario", "produto": "produto"}


def _stem(w: str) -> str:
    if w in _SYN_OF:
        return f"~{_SYN_OF[w]}"
    return w[:5] if len(w) > 5 else w.rstrip("s")


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]{3,}", _fold(text)) if w not in _STOP}


def _terms(a: LayoutAsset) -> list[tuple[set[str], int]]:
    """(palavras, peso) do ativo: tags e nome pesam mais que o "quando usar", a categoria e a descrição."""
    cat = _tokens(_CATEGORY_WORDS.get(a.category or "", ""))
    return [(_tokens(a.tags or ""), 3), (_tokens(a.name or ""), 3), (_tokens(a.use_when or ""), 2), (cat, 2),
            (_tokens(a.note or ""), 1)]


def score(a: LayoutAsset, subject: str) -> int:
    """Quanto o ativo combina com o texto (0 = não combina). Soma o peso de cada grupo de palavras que aparece no texto."""
    words = _tokens(subject)
    return sum(w for terms, w in _terms(a) if terms & words)


def split_tags(raw: str) -> str:
    """Tags separadas por vírgula, sem repetir, curtas: "Tabuleiro, board,  mapa" -> "tabuleiro, board, mapa"."""
    out: list[str] = []
    for t in re.split(r"[,;\n]+", raw or ""):
        t = re.sub(r"\s+", " ", t).strip().lower()[:40]
        if t and t not in out:
            out.append(t)
    return ", ".join(out[:20])



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


def piece_subject(c: ContentItem, extra: str = "") -> str:
    return " ".join([c.title, c.body, c.media, c.img_title, c.img_text, c.image_prompt, extra])


def _signature(c: ContentItem, rows: list[LayoutAsset]) -> str:
    """Muda quando o assunto da peça ou a biblioteca muda: a escolha da IA só vale para o mesmo assunto e os mesmos ativos."""
    raw = piece_subject(c) + "|" + "|".join(f"{a.id}:{a.name}:{a.tags}:{a.use_when}:{a.note}:{a.category}" for a in rows)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _auto(c: ContentItem, rows: list[LayoutAsset]) -> list[int] | None:
    try:
        v = json.loads(c.asset_auto or "")
    except ValueError:
        return None
    if isinstance(v, dict) and v.get("sig") == _signature(c, rows) and isinstance(v.get("ids"), list):
        return [i for i in v["ids"] if isinstance(i, int)]
    return None


def for_piece(db, c: ContentItem, limit: int = MAX_SENT, extra: str = "") -> list[LayoutAsset]:
    """Ativos de imagem que entram na arte desta peça: os citados com @ no prompt (na ordem), depois os escolhidos pelo owner,
    ou, sem escolha, "sempre" + os que a IA escolheu para o assunto (auto_pick) ou, sem ela, os que combinam por tag/nome/uso.
    extra = texto a mais para casar (ex.: a página do carrossel)."""
    rows = [a for a in project_assets(db, c.project_id) if is_image(a)]
    first = mentioned(db, c)
    ids = chosen_ids(c)
    by_id = {a.id: a for a in rows}
    if ids:
        rest = [by_id[i] for i in ids if i in by_id]
        return (first + [a for a in rest if a not in first])[:limit]
    picked = [a for a in rows if a.always]
    auto = _auto(c, rows)
    if auto is not None:
        picked += [by_id[i] for i in auto if i in by_id and by_id[i] not in picked]
        if extra.strip():  # a IA escolheu pelo post inteiro; a página acrescenta o que só ela cita
            picked += [a for a in sorted(rows, key=lambda a: -score(a, extra)) if a not in picked and score(a, extra) >= 3]
    else:
        subject = piece_subject(c, extra)
        hits = sorted((a for a in rows if not a.always and score(a, subject)), key=lambda a: -score(a, subject))
        picked += hits
    return (first + [a for a in picked if a not in first])[:limit]


def _pick_prompt(c: ContentItem, project_name: str, rows: list[LayoutAsset], extra: str) -> str:
    lines = "\n".join(f"- {label(a)} [{CATEGORIES.get(a.category, 'Outro')}]: {a.note.strip()[:160]}"
                      + (f" · tags: {a.tags}" if a.tags.strip() else "")
                      + (f" · quando usar: {a.use_when.strip()[:160]}" if a.use_when.strip() else "") for a in rows)
    return f"""Você escolhe quais imagens de referência da marca "{project_name}" entram na arte de uma peça de conteúdo, para a IA de imagem
desenhar os objetos REAIS do jogo/produto (tabuleiro, cartas, personagens...) em vez de inventar. Responda SOMENTE com um JSON válido.

Peça:
Título: {c.title[:200]}
Texto: {c.body[:1500]}
Cena/mídia: {(c.media or c.image_prompt)[:400]}
Texto na imagem: {c.img_title[:150]} {c.img_text[:250]}
{extra[:900]}

Biblioteca de referências (use o nome EXATO):
{lines}

Regras: escolha só as referências cujo objeto aparece ou é citado na peça (se fala do tabuleiro, o tabuleiro; se fala das cartas, as cartas);
no máximo {MAX_SENT}; nenhuma se nada combina. Não escolha a logo (ela já vai sozinha quando precisa). Formato: {{"ativos": ["Nome exato"]}}"""


async def auto_pick(db, c: ContentItem, extra: str = "") -> None:
    """Pede a um modelo de TEXTO para escolher, na biblioteca do projeto, as referências que a peça precisa (guarda em
    c.asset_auto). Sem escolha manual do owner, uma vez por assunto. Se não houver modelo ou ele falhar, vale o casamento por
    tags/nome (for_piece). Nunca levanta erro: a imagem sai de qualquer jeito."""
    if chosen_ids(c):
        return
    rows = [a for a in project_assets(db, c.project_id) if is_image(a) and not a.always]
    if not rows or not piece_subject(c, extra).strip():
        return
    sig = _signature(c, [a for a in project_assets(db, c.project_id) if is_image(a)])
    try:
        if json.loads(c.asset_auto or "").get("sig") == sig:
            return
    except (ValueError, AttributeError):
        pass
    try:
        from . import growth

        provider = growth.pick_provider(None, "agents_run", db)
        project = db.get(GrowthProject, c.project_id)
        prompt = _pick_prompt(c, project.name if project else "", rows, extra)
        with calllog.context("agents_run", c.project_id, "ativos"):
            with calllog.track(provider.id, provider.model, "agents_run", f"escolher ativos da peça #{c.id}") as call:
                reply = await provider.complete([ChatTurn("user", prompt)])
                call.done(reply)
        m = re.search(r"\{.*\}", reply or "", re.S)
        names = json.loads(m.group(0)).get("ativos") if m else None
        if not isinstance(names, list):
            return
    except (ProviderError, ValueError, AttributeError):
        return
    by_name = {_fold(a.name).strip(): a for a in rows if a.name}
    got: list[LayoutAsset] = []
    for n in names:
        a = by_name.get(_fold(str(n)).strip())
        if a is not None and a not in got:
            got.append(a)
    c.asset_auto = json.dumps({"sig": sig, "ids": [a.id for a in got[:MAX_SENT]], "names": [label(a) for a in got[:MAX_SENT]]},
                              ensure_ascii=False)
    db.commit()


def for_subject(db, project_id: int, text: str, forced: list[int] | None = None, limit: int = MAX_SENT) -> list[LayoutAsset]:
    """Ativos de imagem certos para UM assunto (uma página, o CTA): os escolhidos de antemão (forced, na ordem), os "sempre" e os que
    o assunto cita (nome, "quando usar", descrição ou tipo: falou em tabuleiro, vem o tabuleiro; falou em cartas, vêm as cartas).
    Quem combina com mais palavras vem primeiro."""
    rows = [a for a in project_assets(db, project_id) if is_image(a)]
    by_id = {a.id: a for a in rows}
    out = [by_id[i] for i in (forced or []) if i in by_id]
    scored = [(-score(a, text), a.id, a) for a in rows if a not in out and not a.always and score(a, text)]
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
    picked = for_subject(db, c.project_id, text or c.media or c.title, forced, limit)
    if not any(not a.always and a.id not in forced for a in picked):  # a página não cita nada: vale o que a IA escolheu para a peça
        by_id = {a.id: a for a in project_assets(db, c.project_id) if is_image(a)}
        picked += [by_id[i] for i in (_auto(c, list(by_id.values())) or []) if i in by_id and by_id[i] not in picked]
    return picked[:limit]


def prompt_lines(assets: list[LayoutAsset], first_index: int = 1) -> str:
    """Descrição dos ativos anexados, na ordem das imagens, para o modelo saber o que é cada um e como usar."""
    if not assets:
        return ""
    parts = []
    for i, a in enumerate(assets, start=first_index):
        what = a.note.strip() or CATEGORIES.get(a.category, "")
        use = f" Use it: {a.use_when.strip()[:200]}." if a.use_when.strip() else ""
        parts.append(f"image {i} = {label(a)} ({CATEGORIES.get(a.category, 'asset')}): {what[:200]}.{use}")
    return ("Brand assets attached (reproduce them faithfully when they appear: same shapes, proportions, colors, art style and "
            "details; never redesign, restyle or invent a different logo, board, card, character or object): " + " | ".join(parts))


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
        when += f" · tags: {a.tags}" if (a.tags or "").strip() else ""
        always = " · vai em toda arte" if a.always else ""
        lines.append(f"- {label(a)} [{CATEGORIES.get(a.category, 'Outro')}]: {a.note.strip()[:160]}{when}{always}{extra}")
    return ("ATIVOS DO PROJETO (biblioteca do owner, use pelo nome exato em \"ativos\" no bloco conteudo; vídeos podem ir em "
            "\"link_midia\" com \"fonte_midia\": \"link\"):\n" + "\n".join(lines))
