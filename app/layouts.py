"""Estúdio de layouts: cada projeto tem referências visuais e layouts gerados que servem de base para
stories, carrosséis e reels.

Infra para qualquer projeto: nada aqui sabe de Zombie Party, A Ilha ou Take One. A imagem é gerada pelo
modelo de imagem configurado em /settings (atividade "image", só OpenRouter) e cada geração é um clique do
owner: nunca roda sozinha nem em laço.
"""
import asyncio
import base64
import re

import httpx

from . import calllog, modelcfg
from .db import ContentItem, GrowthProject, LayoutAsset, new_session
from .providers.openrouter import OPENROUTER_URL, _api_key, _error_detail, _explain
from .providers.base import ProviderError

summarize_bytes = calllog.summarize_bytes

# formato -> (nome, proporção, para que serve)
FORMATS: dict[str, tuple[str, str, str]] = {
    "story": ("Story", "9:16", "vertical em tela cheia"),
    "carrossel": ("Carrossel", "4:5", "slide de carrossel do feed"),
    "reel": ("Reel", "9:16", "capa/quadro de vídeo vertical"),
}

MAX_REFERENCES = 8
MAX_IMAGE_BYTES = 4_000_000
MAX_PER_REQUEST = 4  # layouts por clique em "Gerar": limita o custo da API
MAX_REFS_SENT = 6  # referências que vão junto de cada geração
REQUEST_TIMEOUT = 180.0


def detect_mime(data: bytes) -> str | None:
    """Só PNG, JPEG e WebP, reconhecidos pelo conteúdo (não pelo que o navegador diz)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image(b64: str) -> tuple[bytes, str]:
    """Base64 (aceita o prefixo data:) -> (bytes, mime). ValueError com a mensagem para o owner."""
    raw = b64.split(",", 1)[1] if b64.startswith("data:") and "," in b64 else b64
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as e:
        raise ValueError("imagem inválida") from e
    if not data:
        raise ValueError("imagem vazia")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"imagem grande demais (máximo {MAX_IMAGE_BYTES // 1_000_000} MB)")
    mime = detect_mime(data)
    if mime is None:
        raise ValueError("use uma imagem PNG, JPEG ou WebP")
    return data, mime


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def build_prompt(project_name: str, brief: str, fmt: str, feedback: str = "", notes: list[str] | None = None) -> str:
    name, ratio, use = FORMATS[fmt]
    lines = [
        f"Create a brand layout template for social media: {name} ({ratio}, {use}) for the brand \"{project_name}\".",
        "This is a visual REFERENCE of the ARTWORK only: show the composition, color palette, graphic elements, frame, margins "
        "and the position of the logo. It is NOT a finished post and it has NO words on it.",
        "ABSOLUTE RULE, NO TEXT: do not render any letters, words, numbers, symbols that look like writing, headline, subtitle, "
        "caption, label, button, URL, date, small print, placeholder or fake/garbled words anywhere. The ONLY lettering allowed is "
        "the one that is part of the brand logo itself (copy it exactly from the reference images). Everything else is pure artwork: "
        "frame, borders, ornaments, scenery, textures. Leave the large central/lower area completely empty, calm and dark or plain "
        "(no lines, no placeholder bars), because text will be added later by software. Do not invent real claims, prices or people.",
    ]
    if brief.strip():
        lines.append(f"Brand style brief from the owner: {brief.strip()[:3000]}")
    if notes:
        lines.append("Reference images are attached. What each one shows: " + " | ".join(n[:200] for n in notes if n))
    else:
        lines.append("Reference images, if attached, define the visual style: follow their palette, mood and ornaments, but ignore any text they contain except the logo.")
    if feedback.strip():
        lines.append(f"Adjustments requested by the owner for this round: {feedback.strip()[:1000]}")
    lines.append("FINAL CHECK before answering: the image has zero text except the logo's own lettering. If the style brief or "
                 "adjustments above mention titles, typography or text, they refer to the later software step: do NOT draw them.")
    return "\n".join(lines)


def extract_image(data: dict) -> tuple[bytes, str]:
    """Primeira imagem da resposta do OpenRouter (message.images[].image_url.url em data URL)."""
    try:
        images = data["choices"][0]["message"].get("images") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        images = []
    for img in images:
        url = ((img or {}).get("image_url") or {}).get("url") or (img or {}).get("url") or ""
        if url.startswith("data:") and "," in url:
            try:
                return decode_image(url)
            except ValueError:
                continue
    raise ProviderError("o modelo não devolveu nenhuma imagem; tente de novo ou escolha outro modelo de imagem em /settings")


# Modelos que geram imagem e texto (Gemini, GPT Image) pedem ["image", "text"]; os que só geram imagem (Seedream, FLUX...)
# recusam isso com 404 "No endpoints found that support the requested output modalities". Tenta uma, depois a outra.
MODALITY_TRIES = (["image", "text"], ["image"])


async def _post_chat(key: str, body: dict) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(OPENROUTER_URL, json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise ProviderError(f"não consegui falar com o OpenRouter: {type(e).__name__}") from e
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    return resp.status_code, payload if isinstance(payload, dict) else {}


def _is_modality_error(status: int, msg: str) -> bool:
    return status in (400, 404) and "modalit" in msg.lower()


async def request_image(model: str, prompt: str, refs: list[tuple[bytes, str]], ratio: str) -> tuple[bytes, str]:
    """Pede UMA imagem ao OpenRouter, com as referências junto."""
    with calllog.track("openrouter", model, "image", f"imagem {ratio} · {len(refs[:MAX_REFS_SENT])} referência(s) · "
                       f"prompt de {len(prompt)} caracteres") as call:
        data, mime = await _request_image(model, prompt, refs, ratio)
        call.done(summarize_bytes(mime, len(data)))
        return data, mime


async def _request_image(model: str, prompt: str, refs: list[tuple[bytes, str]], ratio: str) -> tuple[bytes, str]:
    key = _api_key()
    if not key:
        raise ProviderError("OpenRouter sem chave: defina GOS_OPENROUTER_API_KEY no .env do VPS")
    content: list[dict] = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": data_url(d, m)}} for d, m in refs[:MAX_REFS_SENT]]
    for i, modalities in enumerate(MODALITY_TRIES):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": modalities,
            "image_config": {"aspect_ratio": ratio},
            "usage": {"include": True},
        }
        status, payload = await _post_chat(key, body)
        if status == 200:
            calllog.note_usage(payload.get("usage"))
            return extract_image(payload)
        _, msg = _error_detail(payload)
        if i + 1 < len(MODALITY_TRIES) and _is_modality_error(status, msg):
            continue
        raise _explain(status, msg, model)
    raise ProviderError("o OpenRouter recusou o pedido de imagem")  # inalcançável: o laço sempre retorna ou levanta


def image_choice(db, log: bool = False, project_id: int | None = None) -> modelcfg.Choice:
    """Modelo de imagem configurado em /settings. ProviderError com o que falta, em linguagem do owner."""
    c = modelcfg.resolve(db, "image")
    if c.provider != "openrouter" or not c.model:
        msg = "Escolha o modelo de imagem do OpenRouter em Modelos (atividade \"Imagens\") antes de gerar."
        if log:  # só nas ações de gerar; as telas também chamam isto para mostrar o aviso e não devem encher o log
            calllog.record_failure(c.provider, c.model, "image", f"{msg} (configurado: {c.provider or 'nenhum'}; imagem só roda no OpenRouter)",
                                   "chamada não enviada", project_id)
        raise ProviderError(msg)
    return c


def asset_json(a: LayoutAsset) -> dict:
    return {
        "id": a.id, "kind": a.kind, "format": a.format, "status": a.status, "note": a.note, "model": a.model,
        "mime": a.mime, "url": f"/api/layouts/assets/{a.id}", "created_at": a.created_at.isoformat(),
    }


# ---------- template HTML do layout ----------
# A IA desenha o layout UMA vez; depois ele vira um template HTML. As peças só trocam o texto e o fundo,
# então todas saem com o mesmo desenho e o texto nunca é "escrito" pela IA dentro da imagem.

CANVAS = {"story": (1080, 1920), "carrossel": (1080, 1350), "reel": (1080, 1920)}
REQUIRED_TOKENS = ("{{bg}}", "{{titulo}}", "{{texto}}")
_TOKEN = re.compile(r"\{\{(bg|titulo|texto|marca)\}\}")
_FORBIDDEN = re.compile(r"<\s*(script|iframe|object|embed|link|meta|base|form)\b|\bon\w+\s*=|javascript:|@import|https?:|//\w", re.I)
MAX_HTML = 60_000


# Fontes que o renderizador de vídeo traz embutidas: qualquer outra cai em fonte genérica e o texto sai diferente do previsto.
BUNDLED_FONTS = ("Inter", "Montserrat", "Outfit", "Nunito", "Oswald", "League Gothic", "Archivo Black", "Poppins", "Roboto",
                 "Open Sans", "Lato", "Playfair Display", "EB Garamond", "Space Mono", "IBM Plex Mono", "JetBrains Mono")
DEFAULT_FONT = "Montserrat"
_FONT_DECL = re.compile(r"font-family\s*:\s*([^;}\"']*(?:\"[^\"]*\"|'[^']*')?[^;}]*)", re.I)


def normalize_fonts(html: str) -> str:
    """Todo font-family cujo primeiro nome não é embutido vira Montserrat: mesma tipografia na imagem e no vídeo."""
    ok = {f.lower() for f in BUNDLED_FONTS}

    def fix(m: re.Match) -> str:
        value = m.group(1).strip()
        first = value.split(",")[0].strip().strip("\"'").lower()
        if first in ok or first.startswith("var("):
            return m.group(0)
        return f"font-family: '{DEFAULT_FONT}', sans-serif"

    return _FONT_DECL.sub(fix, html)


_HARMLESS = (
    re.compile(r"<!--.*?-->", re.S),  # comentários
    re.compile(r"<!doctype[^>]*>", re.I),
    re.compile(r"<meta\b[^>]*>", re.I),  # charset/viewport: a composição põe os próprios
    re.compile(r"<title\b[^>]*>.*?</title>", re.I | re.S),
    re.compile(r"\s(?:xmlns(?::\w+)?|xml:space)\s*=\s*(?:\"[^\"]*\"|'[^']*')", re.I),  # namespace do SVG: só um nome, não carrega nada
)


def strip_harmless(html: str) -> str:
    """Tira o que os modelos põem por hábito e não é risco: doctype, meta, título, comentários e xmlns."""
    for rx in _HARMLESS:
        html = rx.sub("", html)
    return html.strip()


def sanitize_template(html: str) -> str:
    """O HTML veio de uma IA: nada de script, nem de recurso externo. Exige os espaços do título, texto e fundo."""
    html = strip_harmless((html or "").strip())
    if not html or len(html) > MAX_HTML:
        raise ValueError("o template veio vazio ou grande demais")
    m = _FORBIDDEN.search(html)
    if m:
        seen = html[m.start():m.start() + 40].replace("\n", " ")
        raise ValueError(f"o template tem script, link externo ou recurso proibido (achei: {seen})")
    missing = [t for t in REQUIRED_TOKENS if t not in html]
    if missing:
        raise ValueError("faltam no template: " + ", ".join(missing))
    return normalize_fonts(html)


def build_template_prompt(project_name: str, fmt: str, feedback: str = "") -> str:
    w, h = CANVAS[fmt]
    lines = [
        f"Build ONE self-contained HTML document with a fixed canvas of {w}x{h} pixels for the brand \"{project_name}\" "
        f"({FORMATS[fmt][0]}). The attached layout image already has its artwork (logo, frame, ornaments, scenery) and I will "
        "supply that artwork myself as the full-size background. It contains NO text: it has a clean empty panel or area reserved "
        "for the text. Your job is ONLY the TEXT: place the headline and the supporting line inside that reserved area, with a color, "
        "weight, size and alignment that suit the artwork and stay readable on it. Do NOT redraw the "
        "logo, frame, borders, ornaments, dividers or any shape: they are in the background already.",
        "TEXT PLACEMENT (most important for a professional result): find the EMPTY text panel in the layout, the area with no photo, "
        "scenery, face, logo or ornament, and put BOTH the headline and the supporting line fully INSIDE it. Never place text over a "
        "photograph, a person, a card, the logo or the frame. Use absolutely positioned box exactly over that panel, with inner "
        "padding of at least 6% of its width, text centered vertically, a clear gap between headline and supporting line, line-height "
        "1.15 to 1.3, headline 60 to 84px bold, supporting line 32 to 40px regular, light text on dark panels (dark text on light ones) "
        "with strong contrast. Long text must wrap inside the box, never touch its edges and never leave the panel.",
        "Rules:",
        f"- html and body: margin 0, width {w}px, height {h}px, overflow hidden. Only an inline <style> block; no <script>, "
        "no <link>, no <meta>, no <iframe>, no comments, no xmlns, no `//` or `http` anywhere, "
        "no external URLs, no web fonts (use system font stacks such as Impact, 'Arial Black', Arial, sans-serif).",
        "- The full-size background element must use exactly: background-image: {{bg}}; background-size: cover; "
        "background-position: center;  (the placeholder {{bg}} is replaced by the real image).",
        "- Placeholders (exact text, used once each): {{titulo}} for the headline, {{texto}} for the supporting line. "
        "Do not use {{marca}}: the logo is part of the background. Style them so long text wraps and never overflows (word-wrap, max sizes).",
        "- Fonts: use ONLY these families, first in the font-family list: " + ", ".join(BUNDLED_FONTS[:13]) + ". Any other family is replaced.",
        "- Do not put any other text on the page, and keep the page background transparent apart from the {{bg}} layer.",
        "Answer with the HTML document only, inside a ```html code block.",
    ]
    if feedback.strip():
        lines.append(f"Adjustments requested by the owner: {feedback.strip()[:1000]}")
    return "\n".join(lines)


def extract_html(text: str) -> str:
    m = re.search(r"```html\s*\n(.*?)```", text or "", re.S) or re.search(r"(<!doctype html.*</html>)", text or "", re.S | re.I)
    if not m:
        raise ProviderError("o modelo não devolveu um documento HTML; tente de novo ou escolha outro modelo em Modelos")
    return m.group(1).strip()


async def request_html(model: str, prompt: str, image: tuple[bytes, str]) -> str:
    """Pede ao modelo (com visão) o HTML do layout. Devolve o texto da resposta."""
    with calllog.track("openrouter", model, "layout_html", f"layout {image[1]} {len(image[0]) / 1024:.0f} KB → HTML · "
                       f"prompt de {len(prompt)} caracteres") as call:
        text = await _request_html(model, prompt, image)
        call.done(f"{len(text)} caracteres de HTML")
        return text


async def _request_html(model: str, prompt: str, image: tuple[bytes, str]) -> str:
    key = _api_key()
    if not key:
        raise ProviderError("OpenRouter sem chave: defina GOS_OPENROUTER_API_KEY no .env do VPS")
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url(*image)}}]}],
            "usage": {"include": True}}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(OPENROUTER_URL, json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise ProviderError(f"não consegui falar com o OpenRouter: {type(e).__name__}") from e
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code != 200:
        _, msg = _error_detail(payload)
        raise _explain(resp.status_code, msg, model)
    calllog.note_usage(payload.get("usage"))
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    if isinstance(content, list):
        content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def html_choice(db, log: bool = False, project_id: int | None = None) -> modelcfg.Choice:
    c = modelcfg.resolve(db, "layout_html")
    msg = ""
    if not c.provider:
        msg = "Escolha em Modelos o provider de \"Layouts · converter em HTML\" (Codex, Claude ou um modelo com visão do OpenRouter)."
    elif c.provider == "openrouter" and not c.model:
        msg = "Escolha em Modelos um modelo do OpenRouter com visão para \"Layouts · converter em HTML\" antes de converter."
    if msg:
        if log:  # só nas ações de converter; as telas também chamam isto para mostrar o aviso e não devem encher o log
            calllog.record_failure(c.provider, c.model, "layout_html", msg, "chamada não enviada", project_id)
        raise ProviderError(msg)
    return c


async def request_html_for(choice: modelcfg.Choice, prompt: str, image: tuple[bytes, str]) -> str:
    """Pede o HTML ao provider configurado (OpenRouter, Codex ou Claude), sempre com a imagem do layout."""
    if choice.provider == "openrouter":
        return await request_html(choice.model, prompt, image)
    from .providers import all_providers

    provider = all_providers().get(choice.provider)
    if provider is None:
        raise ProviderError(f"provider desconhecido: {choice.provider}")
    ok, why = provider.available()
    if not ok:
        raise ProviderError(why)
    with calllog.track(choice.provider, choice.model, "layout_html", f"layout {image[1]} {len(image[0]) / 1024:.0f} KB → HTML · "
                       f"prompt de {len(prompt)} caracteres") as call:
        text = await provider.configured(choice.model, choice.reasoning).complete_with_image(prompt, *image)
        call.done(f"{len(text)} caracteres de HTML")
        return text


def approved_asset(db, project_id: int, kind: str, fmt: str) -> LayoutAsset | None:
    return (db.query(LayoutAsset).filter(LayoutAsset.project_id == project_id, LayoutAsset.kind == kind,
                                         LayoutAsset.format == fmt, LayoutAsset.status == "approved").first())


def approved_layout(db, project_id: int, fmt: str) -> LayoutAsset | None:
    return approved_asset(db, project_id, "layout", fmt)


def approved_template(db, project_id: int, fmt: str) -> LayoutAsset | None:
    return approved_asset(db, project_id, "template", fmt)


async def generate_template(db, layout: LayoutAsset, feedback: str = "") -> LayoutAsset:
    """Layout aprovado (já sem texto, pedido assim desde a geração) -> HTML só com o texto (1 chamada). A arte é o próprio layout."""
    choice = html_choice(db, log=True, project_id=layout.project_id)
    plate_data, plate_mime = layout.data, layout.mime
    project = db.get(GrowthProject, layout.project_id)
    name = project.name if project else ""
    prompt = build_template_prompt(name, layout.format, feedback)
    with calllog.context("layout_html", layout.project_id):
        text = await request_html_for(choice, prompt, (layout.data, layout.mime))
    try:
        html = sanitize_template(extract_html(text))
    except ValueError as e:
        raise ProviderError(f"template recusado: {e}. Tente de novo.") from e
    from . import motion  # import tardio: motion importa este módulo

    with calllog.context("layout_html", layout.project_id):
        preview = await asyncio.to_thread(motion.render_still, html, layout.format, (plate_data, plate_mime), "TÍTULO DO POST",
                                          "Frase de apoio do post, em uma ou duas linhas.", name)
    plate = LayoutAsset(project_id=layout.project_id, kind="plate", format=layout.format, status="proposed", mime=plate_mime,
                        data=plate_data, model=layout.model, prompt=layout.prompt)
    db.add(plate)
    db.flush()
    t = LayoutAsset(project_id=layout.project_id, kind="template", format=layout.format, status="proposed", mime="image/png",
                    data=preview, html=html, note=feedback.strip(), prompt=prompt, model=choice.model, plate_id=plate.id)
    db.add(t)
    db.commit()
    return t


def delete_template(db, t: LayoutAsset) -> None:
    """Apaga o template e a arte sem texto que veio com ele."""
    if t.plate_id and (plate := db.get(LayoutAsset, t.plate_id)) is not None:
        db.delete(plate)
    db.delete(t)


# ---------- imagem de cada peça: fundo gerado + texto no template ----------

AUTOGEN_PER_TICK = 3  # imagens automáticas por passada do agendador (limita o custo)


def piece_texts(c: ContentItem) -> tuple[str, str]:
    """O que vai escrito NA imagem: o que o agente pediu ou, na falta, o título e o começo do texto."""
    body = c.body.strip()
    if c.img_title or c.title:
        return (c.img_title or c.title)[:80], (c.img_text or body)[:140]
    first, _, rest = body.partition("\n")
    return (c.img_title or first)[:80], (c.img_text or rest.strip())[:140]


def build_background_prompt(project_name: str, brief: str, c: ContentItem, feedback: str = "") -> str:
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [
        f"Create a BACKGROUND image ({ratio}) for a {name} social media post of the brand \"{project_name}\".",
        "ABSOLUTELY NO text, letters, numbers, logos, watermarks or interface elements anywhere. Leave calm, uncluttered "
        "areas where text will be placed on top later.",
        f"Subject: {c.media[:1000]}" if c.media.strip() else f"Subject: {(c.title or c.body)[:300]}",
        f"Brand style brief: {brief.strip()[:1500]}" if brief.strip() else "",
        "Attached images, if any, are brand references: follow their palette and mood.",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def build_scenery_prompt(project_name: str, brief: str, c: ContentItem, feedback: str = "") -> str:
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [
        f"Edit the attached artwork of the brand \"{project_name}\" ({name}). KEEP EXACTLY as they are: the logo/emblem, the frame, "
        "borders, ornaments, dividers, the panel or area reserved for text, the composition and the colors of those elements. "
        f"CHANGE ONLY the photographic scenery/background to fit this subject: {(c.media.strip() or c.title or c.body)[:600]}.",
        "Put the new photo ONLY in the photo window(s) of the artwork. The panel reserved for text must stay EXACTLY as it is in the "
        "attached artwork: empty, plain and dark, with no photo, people, objects, texture or scenery inside it. Do not extend the "
        "photo into the text panel.",
        "ABSOLUTELY NO text, letters or numbers anywhere; keep the text area clean.",
        f"Brand style brief: {brief.strip()[:1000]}" if brief.strip() else "",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def build_reference_prompt(project_name: str, brief: str, c: ContentItem, feedback: str = "") -> str:
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [
        f"Create a finished {name} image ({ratio}) for a social media post of the brand \"{project_name}\".",
        f"Subject: {(c.image_prompt.strip() or c.media.strip() or c.title or c.body)[:1000]}",
        "The attached images are the brand references: keep their palette, mood and visual identity, and the brand logo if it appears.",
        f"Brand style brief: {brief.strip()[:1500]}" if brief.strip() else "",
        "Do not write any headline or sentence on the image; no garbled letters.",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def propose_prompt(project_name: str, brief: str, c: ContentItem) -> str:
    """Prompt que o gerador de conteúdo propõe quando o agente não mandou um: assunto da peça + estilo do projeto."""
    name, ratio, _ = FORMATS[c.visual_format]
    subject = (c.media.strip() or c.title or c.body)[:600]
    style = f" Style: {brief.strip()[:500]}." if brief.strip() else ""
    return f"{name} image ({ratio}) for the brand \"{project_name}\". {subject}.{style} No text, letters or logos in the image."


def build_free_prompt(project_name: str, brief: str, c: ContentItem, feedback: str = "") -> str:
    base = c.image_prompt.strip() or propose_prompt(project_name, brief, c)
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [base, f"Format: {name} {ratio}.",
             f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else ""]
    return "\n".join(x for x in lines if x)


def piece_template(db, c: ContentItem) -> LayoutAsset | None:
    """Template da peça: o escolhido (do mesmo projeto e formato) ou, sem escolha, o aprovado do projeto."""
    if c.template_id:
        t = db.get(LayoutAsset, c.template_id)
        if (t is None or t.kind != "template" or t.project_id != c.project_id or t.format != c.visual_format
                or t.status not in ("approved", "proposed")):
            raise ProviderError("o template escolhido não existe mais ou não serve para este formato: escolha outro")
        return t
    return approved_template(db, c.project_id, c.visual_format)


def _replace_asset(db, c: ContentItem, attr: str, asset: LayoutAsset) -> None:
    old = db.get(LayoutAsset, getattr(c, attr)) if getattr(c, attr) else None
    db.add(asset)
    db.flush()
    setattr(c, attr, asset.id)
    if old is not None:
        db.delete(old)


def set_piece_image(db, c: ContentItem, data: bytes, mime: str, model: str = "", prompt: str = "") -> None:
    """Guarda a imagem final da peça e apaga a anterior (uma imagem por peça)."""
    _replace_asset(db, c, "image_id", LayoutAsset(project_id=c.project_id, kind="piece", format=c.visual_format, mime=mime,
                                                   data=data, model=model, prompt=prompt))
    c.image_error = ""
    db.commit()


def drop_video(db, c: ContentItem) -> None:
    """O vídeo guarda o texto e o fundo de quando foi feito: se algum mudou, ele sai (o owner gera de novo)."""
    if c.video_id:
        old = db.get(LayoutAsset, c.video_id)
        if old is not None:
            db.delete(old)
        c.video_id = None


def render_piece(db, c: ContentItem, bg: LayoutAsset, template: LayoutAsset) -> bytes:
    project = db.get(GrowthProject, c.project_id)
    title, text = piece_texts(c)
    from . import motion

    return motion.render_still(template.html, c.visual_format, (bg.data, bg.mime), title, text, project.name if project else "")


def rerender_piece(db, c: ContentItem) -> None:
    """Texto da imagem mudou: refaz só a imagem final, com o mesmo fundo (sem gastar IA). Erros ficam em image_error."""
    if not (c.image_id and c.bg_id and c.visual_format):
        return
    if (c.image_mode or "template") != "template":
        return
    try:
        template = piece_template(db, c)
    except ProviderError:
        return
    bg = db.get(LayoutAsset, c.bg_id)
    if bg is None or template is None:
        return
    try:
        set_piece_image(db, c, render_piece(db, c, bg, template), "image/png", "template")
        drop_video(db, c)
        db.commit()
    except ProviderError as e:
        db.rollback()
        c.image_error = str(e)[:500]
        db.commit()


async def generate_piece_image(db, c: ContentItem, feedback: str = "") -> None:
    """Gera um fundo novo (a única parte paga) e monta a imagem final no template aprovado do projeto."""
    if not c.visual_format:
        raise ProviderError("esta peça não é visual (story, carrossel ou reel)")
    mode = c.image_mode or "template"
    if mode != "template":
        return await _generate_free_image(db, c, mode, feedback)
    template = piece_template(db, c)
    if template is None:
        raise ProviderError(f"Defina o template de {FORMATS[c.visual_format][0]} deste projeto em Layouts antes de gerar a imagem.")
    choice = image_choice(db, log=True, project_id=c.project_id)
    project = db.get(GrowthProject, c.project_id)
    refs_rows = (db.query(LayoutAsset).filter(LayoutAsset.project_id == c.project_id, LayoutAsset.kind == "reference")
                 .order_by(LayoutAsset.id).limit(MAX_REFS_SENT).all())
    plate = db.get(LayoutAsset, template.plate_id) if template.plate_id else None
    if plate is not None:  # arte aprovada sem texto: o modelo só troca o cenário e mantém logo e moldura
        prompt = build_scenery_prompt(project.name if project else "", (project.style_brief if project else "") or "", c, feedback)
        refs = [(plate.data, plate.mime)]
    else:  # template antigo, sem arte: fundo novo como antes
        prompt = build_background_prompt(project.name if project else "", (project.style_brief if project else "") or "", c, feedback)
        refs = [(a.data, a.mime) for a in refs_rows]
    with calllog.context("image", c.project_id):
        data, mime = await request_image(choice.model, prompt, refs, FORMATS[c.visual_format][1])
    bg = LayoutAsset(project_id=c.project_id, kind="background", format=c.visual_format, mime=mime, data=data,
                     model=choice.model, prompt=prompt)
    with calllog.context("image", c.project_id):
        final = render_piece(db, c, bg, template)  # renderiza antes de guardar: se o renderizador cair, o fundo não fica órfão
    _replace_asset(db, c, "bg_id", bg)
    drop_video(db, c)
    set_piece_image(db, c, final, "image/png", choice.model, prompt)


async def _generate_free_image(db, c: ContentItem, mode: str, feedback: str = "") -> None:
    """Sem template: a imagem sai pronta do modelo, só das referências do projeto (reference) ou só de um prompt (prompt)."""
    choice = image_choice(db, log=True, project_id=c.project_id)
    project = db.get(GrowthProject, c.project_id)
    name, brief = (project.name if project else ""), ((project.style_brief if project else "") or "")
    refs: list[tuple[bytes, str]] = []
    if mode == "reference":
        q = db.query(LayoutAsset).filter(LayoutAsset.project_id == c.project_id, LayoutAsset.kind == "reference")
        rows = [db.get(LayoutAsset, c.ref_id)] if c.ref_id else q.order_by(LayoutAsset.id).limit(MAX_REFS_SENT).all()
        rows = [a for a in rows if a is not None and a.kind == "reference" and a.project_id == c.project_id]
        if not rows:
            raise ProviderError("Suba ao menos uma referência deste projeto em Layouts (ou escolha outro modo de imagem).")
        refs = [(a.data, a.mime) for a in rows]
        prompt = build_reference_prompt(name, brief, c, feedback)
    else:
        prompt = build_free_prompt(name, brief, c, feedback)
    with calllog.context("image", c.project_id):
        data, mime = await request_image(choice.model, prompt, refs, FORMATS[c.visual_format][1])
    if c.bg_id:  # sem template não há fundo separado: o fundo e o vídeo de antes não valem mais
        old = db.get(LayoutAsset, c.bg_id)
        if old is not None:
            db.delete(old)
        c.bg_id = None
    drop_video(db, c)
    set_piece_image(db, c, data, mime, choice.model, prompt)


async def generate_piece_video(db, c: ContentItem) -> None:
    """Anima o template aprovado com o texto e o fundo da peça (sem IA, sem custo) e guarda o MP4."""
    if not (c.visual_format and c.bg_id):
        raise ProviderError("o vídeo usa o fundo gerado da peça: gere a imagem primeiro")
    bg, template = db.get(LayoutAsset, c.bg_id), piece_template(db, c)
    if bg is None or template is None:
        raise ProviderError("faltam o fundo ou o template aprovado do projeto para montar o vídeo")
    project = db.get(GrowthProject, c.project_id)
    title, text = piece_texts(c)
    from . import motion

    with calllog.context("video_render", c.project_id):
        video = await motion.render_video(template.html, c.visual_format, (bg.data, bg.mime), title, text, project.name if project else "")
    _replace_asset(db, c, "video_id", LayoutAsset(project_id=c.project_id, kind="video", format=c.visual_format,
                                                   mime="video/mp4", data=video, model="hyperframes"))
    db.commit()


async def autogen_pending() -> int:
    """Passada do agendador: imagem para peças que aguardam aprovação, até AUTOGEN_PER_TICK por vez.

    Cada peça tem uma tentativa automática: se falhar, o motivo fica em image_error e só o botão do owner tenta de novo.
    Sem template aprovado ou sem modelo de imagem configurado, a peça espera sem gastar nada.
    """
    done = 0
    db = new_session()
    try:
        rows = (db.query(ContentItem).filter(ContentItem.status == "pending", ContentItem.visual_format != "",
                                             ContentItem.image_id.is_(None), ContentItem.image_error == "")
                .order_by(ContentItem.id).limit(50).all())
        for c in rows:
            if done >= AUTOGEN_PER_TICK:
                break
            try:
                if (c.image_mode or "template") == "template" and piece_template(db, c) is None:
                    continue
            except ProviderError as e:  # template escolhido sumiu: registra e não tenta de novo sozinho
                c.image_error = str(e)[:500]
                db.commit()
                continue
            try:
                image_choice(db, log=True, project_id=c.project_id)
                await generate_piece_image(db, c)
            except ProviderError as e:
                db.rollback()
                c.image_error = str(e)[:500]
                db.commit()
            done += 1
    finally:
        db.close()
    return done
