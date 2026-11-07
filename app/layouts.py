"""Estúdio de layouts: cada projeto tem referências visuais e layouts gerados que servem de base para
stories, carrosséis e reels.

Infra para qualquer projeto: nada aqui sabe de Zombie Party, A Ilha ou Take One. A imagem é gerada pelo
modelo de imagem configurado em /settings (atividade "image", só OpenRouter) e cada geração é um clique do
owner: nunca roda sozinha nem em laço.
"""
import asyncio
import base64
import json
import logging
import re
import secrets
import time

import httpx

from . import assets as ast
from . import calllog, endpoints, modelcfg
from .config import get_settings
from .db import ContentItem, GrowthProject, LayoutAsset, new_session
from .providers.openrouter import OPENROUTER_URL, _api_key, _error_detail, _explain
from .providers.base import ProviderError

log = logging.getLogger(__name__)
summarize_bytes = calllog.summarize_bytes

# formato -> (nome, proporção, para que serve)
FORMATS: dict[str, tuple[str, str, str]] = {
    "story": ("Story", "9:16", "vertical em tela cheia"),
    "carrossel": ("Carrossel", "4:5", "slide de carrossel do feed"),
    "reel": ("Reel", "9:16", "capa/quadro de vídeo vertical"),
}

# Carrossel sempre roda em três layouts: capa, páginas do meio e CTA final. Story e reel têm um papel só ("").
ROLES: dict[str, tuple[str, str]] = {
    "capa": ("Capa", "the COVER slide (first slide): the strongest, most eye-catching composition"),
    "pagina": ("Páginas", "an INNER content slide (pages 2..n): calmer, with a larger empty area for a paragraph of text, "
                          "consistent with the cover"),
    "cta": ("CTA final", "the FINAL call-to-action slide: closing composition with a clear empty area for the call to action"),
}


def roles_for(fmt: str) -> tuple[str, ...]:
    return tuple(ROLES) if fmt == "carrossel" else ("",)


def norm_role(fmt: str, role) -> str:
    r = str(role or "").strip().lower()
    r = {"pagina": "pagina", "página": "pagina", "paginas": "pagina", "páginas": "pagina", "meio": "pagina",
         "capa": "capa", "cover": "capa", "cta": "cta", "final": "cta"}.get(r, r)
    if fmt != "carrossel":
        return ""
    return r if r in ROLES else "capa"


def role_label(fmt: str, role: str) -> str:
    return ROLES[role][0] if fmt == "carrossel" and role in ROLES else FORMATS.get(fmt, ("",))[0]


MAX_REFERENCES = 60  # ativos por projeto (a biblioteca em Conteúdo › Ativos); por geração vão no máximo MAX_REFS_SENT
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


def build_prompt(project_name: str, brief: str, fmt: str, feedback: str = "", notes: list[str] | None = None,
                 role: str = "") -> str:
    name, ratio, use = FORMATS[fmt]
    if fmt == "carrossel" and role in ROLES:
        use = f"{use}, {ROLES[role][1]}"
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
    route = endpoints.route_for(model, "image")
    note = "" if endpoints.is_known(model) else " · rota NÃO registrada, usando a padrão"
    with calllog.track("openrouter", model, "image", f"imagem {ratio} · rota {route}{note} · {len(refs[:MAX_REFS_SENT])} "
                       f"referência(s) · prompt de {len(prompt)} caracteres") as call:
        data, mime = await _request_image(model, prompt, refs, ratio)
        call.done(summarize_bytes(mime, len(data)))
        return data, mime


# Alguns provedores (o Seed, do Seedream) recusam as referências em data URL com 400 "Unsupported image URL scheme". Aí a
# chamada é refeita com links HTTPS de uso único para as mesmas imagens, servidos por /api/public/ref/{token} por 10 minutos.
SHARE_TTL = 600.0
_SHARED: dict[str, tuple[bytes, str, float]] = {}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{40,64}")


def _public_base() -> str:
    base = (get_settings().public_url or "").strip().rstrip("/")
    return base if base.startswith("https://") else ""


def share_image(data: bytes, mime: str) -> str:
    """Link HTTPS temporário para uma referência (precisa de GOS_PUBLIC_URL). "" se não houver URL pública."""
    base = _public_base()
    if not base:
        return ""
    now = time.monotonic()
    for t in [t for t, (_, _, exp) in _SHARED.items() if exp <= now]:
        _SHARED.pop(t, None)
    token = secrets.token_urlsafe(32)
    _SHARED[token] = (data, mime, now + SHARE_TTL)
    return f"{base}/api/public/ref/{token}"


def shared_image(token: str) -> tuple[bytes, str] | None:
    if not _TOKEN_RE.fullmatch(token or ""):
        return None
    item = _SHARED.get(token)
    if item is None or item[2] <= time.monotonic():
        _SHARED.pop(token, None)
        return None
    return item[0], item[1]


def unshare(urls: list[str]) -> None:
    for u in urls:
        _SHARED.pop(u.rsplit("/", 1)[-1], None)


def _is_scheme_error(status: int, msg: str) -> bool:
    m = msg.lower()
    return status == 400 and ("url scheme" in m or "data url" in m)


def extract_images_endpoint(data: dict) -> tuple[bytes, str] | str:
    """Resposta do /images: data[0].b64_json (ou data URL). Devolve (bytes, mime), ou o link https se vier só url."""
    items = data.get("data") if isinstance(data, dict) else None
    for it in items if isinstance(items, list) else []:
        it = it if isinstance(it, dict) else {}
        raw = it.get("b64_json") or it.get("image") or ""
        url = it.get("url") or ""
        if raw:
            try:
                return decode_image(raw)
            except ValueError:
                continue
        if url.startswith("data:"):
            try:
                return decode_image(url)
            except ValueError:
                continue
        if url.startswith("https://"):
            return url
    raise ProviderError("o modelo não devolveu nenhuma imagem; tente de novo ou escolha outro modelo de imagem em /settings")


async def _request_image_endpoint(key: str, model: str, prompt: str, ratio: str,
                                  refs: list[tuple[bytes, str]] | None = None) -> tuple[bytes, str]:
    """Modelos só de imagem (rota /images). As referências vão em "input_references" (image_url), como na doc do OpenRouter;
    antes só o prompt ia e as imagens eram ignoradas sem aviso. Se o provedor recusar data URL, refaz com links HTTPS."""
    refs = (refs or [])[:MAX_REFS_SENT]
    urls = [data_url(d, m) for d, m in refs]
    links: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            while True:
                body = {"model": model, "prompt": prompt, "n": 1, "aspect_ratio": ratio}
                if urls:
                    body["input_references"] = [{"type": "image_url", "image_url": {"url": u}} for u in urls]
                resp = await client.post(endpoints.URLS[endpoints.IMAGES], json=body, headers={"Authorization": f"Bearer {key}"})
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                payload = payload if isinstance(payload, dict) else {}
                if resp.status_code == 200:
                    break
                _, msg = _error_detail(payload)
                if refs and not links and _is_scheme_error(resp.status_code, msg):
                    links = [share_image(d, m) for d, m in refs]
                    if not all(links):
                        raise ProviderError(f"{model} não aceita as imagens de referência embutidas e pede links HTTPS: defina "
                                            "GOS_PUBLIC_URL=https://os.takeonestudio.io no .env do VPS (ou escolha outro modelo "
                                            "de imagem em /settings)")
                    urls = links
                    continue
                if urls and resp.status_code == 400 and "input_reference" in msg.lower():
                    raise ProviderError(f"{model} não aceita imagens de referência (input_references): escolha outro modelo de "
                                        "imagem em /settings para usar ativos, ou tire as referências desta arte")
                raise _explain(resp.status_code, msg, model)
            calllog.note_usage(payload.get("usage"))
            got = extract_images_endpoint(payload)
            if isinstance(got, str):  # só veio um link: baixa uma vez
                dl = await client.get(got)
                if dl.status_code != 200:
                    raise ProviderError(f"não consegui baixar a imagem gerada ({dl.status_code})")
                return decode_image(base64.b64encode(dl.content).decode())
            return got
    except httpx.HTTPError as e:
        raise ProviderError(f"não consegui falar com o OpenRouter: {type(e).__name__}") from e
    except ValueError as e:
        raise ProviderError(str(e)) from e
    finally:
        unshare(links)


async def _request_image(model: str, prompt: str, refs: list[tuple[bytes, str]], ratio: str) -> tuple[bytes, str]:
    key = _api_key()
    if not key:
        raise ProviderError("OpenRouter sem chave: defina GOS_OPENROUTER_API_KEY no .env do VPS")
    bad = endpoints.incompatible(model, "image")
    if bad:
        raise ProviderError(bad)
    if endpoints.route_for(model, "image") == endpoints.IMAGES:
        return await _request_image_endpoint(key, model, prompt, ratio, refs)
    refs = refs[:MAX_REFS_SENT]
    urls = [data_url(d, m) for d, m in refs]
    links: list[str] = []
    try:
        i = 0
        while i < len(MODALITY_TRIES):
            content: list[dict] = [{"type": "text", "text": prompt}]
            content += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
            body = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "modalities": MODALITY_TRIES[i],
                "image_config": {"aspect_ratio": ratio},
                "usage": {"include": True},
            }
            calllog.note_prompt(messages=body["messages"])
            status, payload = await _post_chat(key, body)
            if status == 200:
                calllog.note_usage(payload.get("usage"))
                return extract_image(payload)
            _, msg = _error_detail(payload)
            if i + 1 < len(MODALITY_TRIES) and _is_modality_error(status, msg):
                i += 1
                continue
            if refs and not links and _is_scheme_error(status, msg):
                links = [share_image(d, m) for d, m in refs]
                if not all(links):
                    raise ProviderError(f"{model} não aceita as imagens de referência embutidas e pede links HTTPS: defina "
                                        "GOS_PUBLIC_URL=https://os.takeonestudio.io no .env do VPS (ou escolha outro modelo de "
                                        "imagem em /settings)")
                urls = links
                continue
            # Modelo fora do registro que o OpenRouter manda para o /images: troca de rota (e o log mostra a mensagem original).
            if not endpoints.is_known(model) and endpoints.wrong_route_hint(msg) == endpoints.IMAGES:
                return await _request_image_endpoint(key, model, prompt, ratio, refs)
            raise _explain(status, msg, model)
        raise ProviderError("o OpenRouter recusou o pedido de imagem")  # inalcançável: o laço sempre retorna ou levanta
    finally:
        unshare(links)


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
        "name": a.name or "", "role": a.role or "", "is_default": bool(a.is_default),
        "media_slot": "{{midia}}" in (a.html or ""), "uploaded": a.model == "upload",
        "boxes": template_boxes(a) if a.kind == "template" else {}, "has_art": bool(a.plate_id) if a.kind == "template" else False,
        "art_url": f"/api/layouts/assets/{a.plate_id}" if a.kind == "template" and a.plate_id else "",
    }


def asset_label(a: LayoutAsset) -> str:
    return a.name or f"{role_label(a.format, a.role or '')} #{a.id}"


def has_alpha(data: bytes, mime: str) -> bool:
    """PNG com canal alfa (ou tRNS) ou WebP com alfa: a foto/vídeo da peça pode ficar ATRÁS da arte."""
    if mime == "image/png" and len(data) > 26:
        return data[25] in (4, 6) or b"tRNS" in data[:4096]
    if mime == "image/webp" and len(data) > 21 and data[12:16] == b"VP8X":
        return bool(data[20] & 0x10)
    return False


# ---------- template HTML do layout ----------
# A IA desenha o layout UMA vez; depois ele vira um template HTML. As peças só trocam o texto e o fundo,
# então todas saem com o mesmo desenho e o texto nunca é "escrito" pela IA dentro da imagem.

CANVAS = {"story": (1080, 1920), "carrossel": (1080, 1350), "reel": (1080, 1920)}
REQUIRED_TOKENS = ("{{bg}}", "{{titulo}}", "{{texto}}")
_TOKEN = re.compile(r"\{\{(bg|titulo|texto|marca|midia)\}\}")
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
        "- PHOTO WINDOW (optional): if the artwork has a clear window or area meant for a photo/scene (not the text panel), add ONE "
        "empty absolutely positioned <div> exactly over it, with overflow hidden and the same rounded corners, containing only the "
        "placeholder {{midia}} (a photo or video of the post goes there). If there is no such window, do not use {{midia}}.",
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
    bad = endpoints.incompatible(model, "text")
    if bad:
        raise ProviderError(bad)
    key = _api_key()
    if not key:
        raise ProviderError("OpenRouter sem chave: defina GOS_OPENROUTER_API_KEY no .env do VPS")
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url(*image)}}]}],
            "usage": {"include": True}}
    calllog.note_prompt(messages=body["messages"])
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
    calllog.note_reply(content or "")
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


TEXT_POSITIONS = {"embaixo": "flex-end", "meio": "center", "topo": "flex-start"}


def simple_template(fmt: str, position: str = "embaixo", media_under: bool = False) -> str:
    """Template pronto, sem IA: a arte que o owner subiu em tela cheia e o texto numa faixa com sombra (embaixo, no meio ou
    em cima). Se a arte tem transparência, a foto/vídeo da peça fica atrás dela."""
    w, h = CANVAS[fmt]
    align = TEXT_POSITIONS.get(position, "flex-end")
    shade = {"flex-end": "linear-gradient(to top, rgba(0,0,0,.72), rgba(0,0,0,0) 45%)",
             "flex-start": "linear-gradient(to bottom, rgba(0,0,0,.72), rgba(0,0,0,0) 45%)",
             "center": "radial-gradient(ellipse at center, rgba(0,0,0,.55), rgba(0,0,0,0) 70%)"}[align]
    media = '<div class="photo">{{midia}}</div>' if media_under else ""
    return f"""<html><head><style>
html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; }}
.photo {{ position: absolute; inset: 0; }}
.art {{ position: absolute; inset: 0; background-image: {{{{bg}}}}; background-size: cover; background-position: center; }}
.shade {{ position: absolute; inset: 0; background: {shade}; }}
.box {{ position: absolute; inset: 7% 8%; display: flex; flex-direction: column; justify-content: {align}; gap: 28px;
  text-align: center; color: #fff; }}
.t {{ font-family: 'Montserrat', sans-serif; font-weight: 800; font-size: 76px; line-height: 1.12; overflow-wrap: break-word; }}
.s {{ font-family: 'Montserrat', sans-serif; font-weight: 500; font-size: 38px; line-height: 1.28; overflow-wrap: break-word; }}
</style></head><body>{media}<div class="art"></div><div class="shade"></div>
<div class="box"><div class="t">{{{{titulo}}}}</div><div class="s">{{{{texto}}}}</div></div></body></html>"""


# ---------- editor de caixas: o owner marca na arte onde vão o título, o texto e a imagem da IA ----------

BOX_KEYS = ("titulo", "texto", "midia")
BOX_DEFAULTS = {
    "titulo": {"x": 8, "y": 55, "w": 84, "h": 14, "max": 60, "size": 72, "align": "center", "color": "#ffffff", "weight": 800},
    "texto": {"x": 8, "y": 70, "w": 84, "h": 14, "max": 140, "size": 38, "align": "center", "color": "#ffffff", "weight": 500},
}
DEFAULT_LIMITS = (80, 140)
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
IMAGE_RATIOS = {"1:1": 1.0, "2:3": 2 / 3, "3:2": 1.5, "3:4": 0.75, "4:3": 4 / 3, "4:5": 0.8, "5:4": 1.25, "9:16": 9 / 16,
                "16:9": 16 / 9, "21:9": 21 / 9}


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return round(min(max(x, lo), hi), 2) if x == x else default


def clean_boxes(raw) -> dict:
    """Caixas do editor, em % da arte (x, y, w, h). Título, texto e imagem são todos opcionais (um Reels pode não ter texto),
    mas precisa haver ao menos uma caixa. ValueError se inválido."""
    if not isinstance(raw, dict):
        raise ValueError("caixas inválidas")
    out: dict = {}
    for key in BOX_KEYS:
        b = raw.get(key)
        if not isinstance(b, dict):
            continue
        x, y = _num(b.get("x"), 0, 95, 8), _num(b.get("y"), 0, 95, 50)
        box = {"x": x, "y": y, "w": _num(b.get("w"), 5, 100 - x, 50), "h": _num(b.get("h"), 3, 100 - y, 10)}
        if key == "midia":
            box["radius"] = int(_num(b.get("radius"), 0, 200, 0))
        else:
            d = BOX_DEFAULTS[key]
            box["max"] = int(_num(b.get("max"), 5, 400, d["max"]))
            box["size"] = int(_num(b.get("size"), 16, 180, d["size"]))
            box["align"] = b.get("align") if b.get("align") in ("left", "center", "right") else d["align"]
            box["valign"] = b.get("valign") if b.get("valign") in ("top", "middle", "bottom") else "middle"
            box["color"] = b.get("color") if isinstance(b.get("color"), str) and _COLOR.fullmatch(b["color"]) else d["color"]
            box["weight"] = 800 if int(_num(b.get("weight"), 100, 900, d["weight"])) >= 650 else 500
        out[key] = box
    if not out:
        raise ValueError("adicione ao menos uma caixa (título, texto ou imagem)")
    return out


def template_boxes(t: LayoutAsset | None) -> dict:
    if t is None or not t.boxes:
        return {}
    try:
        return clean_boxes(json.loads(t.boxes))
    except (ValueError, TypeError):
        return {}


def box_template(fmt: str, boxes: dict) -> str:
    """HTML do template feito no editor: a arte em tela cheia, a imagem na caixa dela e o texto nas caixas, sem sair delas."""
    w, h = CANVAS[fmt]
    css = [f"html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; }}",
           ".art { position: absolute; inset: 0; background-image: {{bg}}; background-size: cover; background-position: center; }",
           ".bx { position: absolute; display: flex; flex-direction: column; overflow: hidden; box-sizing: border-box; }"]
    parts = ['<div class="art"></div>']
    m = boxes.get("midia")
    if m:
        css.append(f".photo {{ position: absolute; left: {m['x']}%; top: {m['y']}%; width: {m['w']}%; height: {m['h']}%; "
                   f"overflow: hidden; border-radius: {m['radius']}px; }}")
        parts.append('<div class="photo">{{midia}}</div>')
    flex = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}
    for key, cls in (("titulo", "t"), ("texto", "s")):
        b = boxes.get(key)
        if b is None:
            continue
        lh = 1.12 if key == "titulo" else 1.28
        css.append(f".{cls} {{ left: {b['x']}%; top: {b['y']}%; width: {b['w']}%; height: {b['h']}%; "
                   f"justify-content: {flex[b['valign']]}; text-align: {b['align']}; color: {b['color']}; "
                   f"font-family: 'Montserrat', sans-serif; font-weight: {b['weight']}; font-size: {b['size']}px; "
                   f"line-height: {lh}; overflow-wrap: break-word; }}")
        parts.append(f'<div class="bx {cls}">{{{{{key}}}}}</div>')
    return "<html><head><style>\n" + "\n".join(css) + "\n</style></head><body>" + "".join(parts) + "</body></html>"


def text_limits(t: LayoutAsset | None) -> tuple[int, int]:
    """Máximo de caracteres do título e do texto no template (os do editor, ou os de sempre)."""
    b = template_boxes(t)
    return (b.get("titulo", {}).get("max", DEFAULT_LIMITS[0]), b.get("texto", {}).get("max", DEFAULT_LIMITS[1])) if b else DEFAULT_LIMITS


def fit_text(text: str, limit: int) -> str:
    """Corta no limite sem partir palavra, com reticências."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if text[limit - 1] != " ":  # a palavra continua depois do corte: volta até o espaço anterior
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(" ,;:.-")
    return (cut or text[: limit - 1]) + "…"


def nearest_ratio(w: float, h: float) -> str:
    """Proporção aceita pelos modelos de imagem mais perto da caixa da imagem."""
    r = w / h if h else 1.0
    return min(IMAGE_RATIOS, key=lambda k: abs(IMAGE_RATIOS[k] - r))


def limits_brief(db, project_id: int) -> str:
    """Para os agentes: quantos caracteres cabem no título e no texto da arte de cada formato (template padrão)."""
    parts = []
    for fmt in FORMATS:
        for role in (["capa", "pagina", "cta"] if fmt == "carrossel" else [""]):
            t = approved_template(db, project_id, fmt, role)
            if t is not None and t.boxes:
                lt, ls = text_limits(t)
                bx = template_boxes(t)
                desc = ", ".join([f"título até {lt}" if "titulo" in bx else "sem título",
                                  f"texto até {ls}" if "texto" in bx else "sem texto"])
                parts.append(f"{FORMATS[fmt][0]}{(' · ' + role_label(fmt, role)) if role else ''}: {desc}")
    if not parts:
        return ""
    return ("LIMITES DE TEXTO NA ARTE (\"titulo_imagem\"/\"texto_imagem\", páginas e CTA; acima disso o texto é cortado): "
            + "; ".join(parts) + ".")


def save_box_template(db, src: LayoutAsset, boxes: dict) -> LayoutAsset:
    """Editor -> template. Em um template que já existe, troca as caixas no lugar (mantém nome, padrão e a arte); numa arte
    aprovada, cria um template novo, já aprovado (foi o owner que montou)."""
    from . import motion

    boxes = clean_boxes(boxes)
    html = box_template(src.format, boxes)
    if src.kind == "template":
        plate = _plate(db, src)
        if plate is None:
            raise ValueError("este template não tem a arte guardada: monte a partir da arte em Artes")
        art = (plate.data, plate.mime)
    elif src.kind == "layout" and src.status == "approved":
        art = (src.data, src.mime)
    else:
        raise ValueError("aprove a arte antes de montar o template")
    project = db.get(GrowthProject, src.project_id)
    sample_t = fit_text("Título do post com o tamanho máximo que esta caixa aceita", boxes["titulo"]["max"]) if "titulo" in boxes else ""
    sample_s = fit_text("Frase de apoio do post, ocupando o espaço que você reservou para o texto nesta arte, até o limite "
                        "de caracteres definido no editor de caixas.", boxes["texto"]["max"]) if "texto" in boxes else ""
    preview = motion.render_still(html, src.format, art, sample_t, sample_s, project.name if project else "")
    if src.kind == "template":
        src.html, src.data, src.mime, src.boxes, src.model = html, preview, "image/png", json.dumps(boxes), "editor"
        src.note = "caixas do editor"
        db.commit()
        return src
    t = _save_template(db, src, html, preview, "caixas do editor", "", "editor")
    t.boxes = json.dumps(boxes)
    t.status = "approved"
    if (plate := _plate(db, t)) is not None:
        plate.status = "approved"
    db.commit()
    ensure_default(db, t.project_id, t.format, t.role or "")
    db.commit()
    return t


def approved_asset(db, project_id: int, kind: str, fmt: str) -> LayoutAsset | None:
    return (db.query(LayoutAsset).filter(LayoutAsset.project_id == project_id, LayoutAsset.kind == kind,
                                         LayoutAsset.format == fmt, LayoutAsset.status == "approved")
            .order_by(LayoutAsset.id.desc()).first())


def approved_layout(db, project_id: int, fmt: str) -> LayoutAsset | None:
    return approved_asset(db, project_id, "layout", fmt)


def role_templates(db, project_id: int, fmt: str, role: str = "") -> list[LayoutAsset]:
    """Templates aprovados do formato e papel (o padrão primeiro). Templates antigos de carrossel, sem papel, valem como capa."""
    roles = [role] + ([""] if fmt == "carrossel" and role == "capa" else [])
    rows = (db.query(LayoutAsset).filter(LayoutAsset.project_id == project_id, LayoutAsset.kind == "template",
                                         LayoutAsset.format == fmt, LayoutAsset.status == "approved",
                                         LayoutAsset.role.in_(roles))
            .order_by(LayoutAsset.is_default.desc(), LayoutAsset.id.desc()).all())
    return rows


def approved_template(db, project_id: int, fmt: str, role: str = "") -> LayoutAsset | None:
    """O template padrão do projeto no formato (e papel, no carrossel). Sem padrão marcado, o aprovado mais novo.
    Páginas e CTA sem template próprio usam o da capa: o carrossel nunca fica parado por falta de um dos três."""
    role = norm_role(fmt, role) if fmt == "carrossel" else ""
    rows = role_templates(db, project_id, fmt, role)
    if not rows and role in ("pagina", "cta"):
        rows = role_templates(db, project_id, fmt, "capa")
    return rows[0] if rows else None


def set_default(db, t: LayoutAsset) -> None:
    """Marca o template como o padrão do formato e papel (desmarca os outros)."""
    for o in db.query(LayoutAsset).filter(LayoutAsset.project_id == t.project_id, LayoutAsset.kind == "template",
                                          LayoutAsset.format == t.format, LayoutAsset.role == (t.role or ""),
                                          LayoutAsset.is_default == 1, LayoutAsset.id != t.id).all():
        o.is_default = 0
    t.is_default = 1


def ensure_default(db, project_id: int, fmt: str, role: str) -> None:
    """Se o formato e papel ficou sem padrão, o aprovado mais novo vira o padrão."""
    rows = (db.query(LayoutAsset).filter(LayoutAsset.project_id == project_id, LayoutAsset.kind == "template",
                                         LayoutAsset.format == fmt, LayoutAsset.role == (role or ""),
                                         LayoutAsset.status == "approved").order_by(LayoutAsset.id.desc()).all())
    if rows and not any(r.is_default for r in rows):
        rows[0].is_default = 1


def make_simple_template(db, layout: LayoutAsset, position: str = "embaixo") -> LayoutAsset:
    """Arte (gerada ou que o owner subiu) -> template com texto padrão, sem IA e sem custo."""
    project = db.get(GrowthProject, layout.project_id)
    under = has_alpha(layout.data, layout.mime)
    html = simple_template(layout.format, position, under)
    from . import motion

    preview = motion.render_still(html, layout.format, (layout.data, layout.mime), "TÍTULO DO POST",
                                  "Frase de apoio do post, em uma ou duas linhas.", project.name if project else "")
    return _save_template(db, layout, html, preview, f"texto padrão ({position})", "", "texto-padrao")


def _save_template(db, layout: LayoutAsset, html: str, preview: bytes, note: str, prompt: str, model: str) -> LayoutAsset:
    plate = LayoutAsset(project_id=layout.project_id, kind="plate", format=layout.format, status="proposed", mime=layout.mime,
                        data=layout.data, model=layout.model, prompt=layout.prompt, role=layout.role or "")
    db.add(plate)
    db.flush()
    t = LayoutAsset(project_id=layout.project_id, kind="template", format=layout.format, status="proposed", mime="image/png",
                    data=preview, html=html, note=note, prompt=prompt, model=model, plate_id=plate.id,
                    role=layout.role or "", name=layout.name or "")
    db.add(t)
    db.commit()
    return t


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
    return _save_template(db, layout, html, preview, feedback.strip(), prompt, choice.model)


def delete_template(db, t: LayoutAsset) -> None:
    """Apaga o template e a arte sem texto que veio com ele."""
    if t.plate_id and (plate := db.get(LayoutAsset, t.plate_id)) is not None:
        db.delete(plate)
    db.delete(t)


# ---------- imagem de cada peça: fundo gerado + texto no template ----------

AUTOGEN_PER_TICK = 3  # imagens automáticas por passada do agendador (limita o custo)


def piece_texts(c: ContentItem, limits: tuple[int, int] = DEFAULT_LIMITS) -> tuple[str, str]:
    """O que vai escrito NA imagem: o que o agente pediu ou, na falta, o título e o começo do texto. Corta no limite do
    template sem partir palavra."""
    body = c.body.strip()
    if c.img_title or c.title:
        title, text = (c.img_title or c.title), (c.img_text or body)
    else:
        first, _, rest = body.partition("\n")
        title, text = (c.img_title or first), (c.img_text or rest.strip())
    return fit_text(title, limits[0]), fit_text(text, limits[1])


NO_TEXT = "ABSOLUTELY NO text, letters or numbers anywhere in the image."
KEEP_REFS = ("Reference images are attached: keep each reference's content exactly as it is, INCLUDING any text, numbers, "
             "messages or screen content that appears in it (for example a phone showing messages keeps those messages); do "
             "not redraw, translate or garble it. Do not add any other new text, letters, numbers or logos.")


def text_rule(has_refs: bool) -> str:
    """Regra de texto da imagem: sem referência, nada de texto; com referência, o que está nela (inclusive texto) se mantém."""
    return KEEP_REFS if has_refs else NO_TEXT


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
        "Make ONE single new scene and combine the attached references into it. Do NOT add new frames, picture boxes, cards, "
        "panels, collages or several separate images: if the artwork has no photo window, only change the background "
        "scenery behind the existing elements.",
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
        "Keep the content of the reference images exactly as it is, including any text, numbers or messages in them (a phone "
        "showing messages keeps those messages). Do not add any headline or new sentence on the image.",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def propose_prompt(project_name: str, brief: str, c: ContentItem, has_refs: bool = False) -> str:
    """Prompt que o gerador de conteúdo propõe quando o agente não mandou um: assunto da peça + estilo do projeto."""
    name, ratio, _ = FORMATS[c.visual_format]
    subject = (c.media.strip() or c.title or c.body)[:600]
    style = f" Style: {brief.strip()[:500]}." if brief.strip() else ""
    return f"{name} image ({ratio}) for the brand \"{project_name}\". {subject}.{style} {text_rule(has_refs)}"


def build_free_prompt(project_name: str, brief: str, c: ContentItem, feedback: str = "", has_refs: bool = False) -> str:
    base = c.image_prompt.strip() or propose_prompt(project_name, brief, c, has_refs)
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [base, f"Format: {name} {ratio}.",
             f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else ""]
    return "\n".join(x for x in lines if x)


def _with_assets(prompt: str, assets: list, first_index: int, db=None, c: ContentItem | None = None) -> str:
    if db is not None and c is not None:
        prompt = ast.rewrite_mentions(db, c, prompt, assets, first_index)
    lines = ast.prompt_lines(assets, first_index)
    return f"{prompt}\n{lines}" if lines else prompt


def style_refs(db, project_id: int) -> list[LayoutAsset]:
    """Referências de estilo do projeto (logo, paleta, as antigas sem categoria e as marcadas "sempre"), para gerar artes."""
    rows = ast.project_assets(db, project_id)
    return [a for a in rows if ast.is_image(a) and (a.always or (a.category or "") in ("", "logo", "estilo"))]


def build_merge_prompt(project_name: str, c: ContentItem, feedback: str = "") -> str:
    """Foto do owner (upload ou link) encaixada na arte do layout, quando o template não tem a janela {{midia}}."""
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [
        f"Edit the FIRST attached image, the artwork of the brand \"{project_name}\" ({name}, {ratio}). Put the photo from the "
        "SECOND attached image inside the photo window/scenery area of the artwork, keeping that photo recognizable (same people, "
        "objects and scene: crop and fit it, do not redraw it).",
        "KEEP EXACTLY as they are: the logo, frame, borders, ornaments, dividers and the empty panel reserved for text. The text "
        "panel must stay plain and empty. ABSOLUTELY NO new text, letters or numbers anywhere.",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def _json(raw: str, default):
    try:
        v = json.loads(raw or "")
    except (ValueError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


def piece_template_ids(c: ContentItem) -> dict[str, int]:
    """Templates escolhidos por papel ({"capa": 3, "pagina": 5, "cta": 7}); story/reel usam template_id."""
    raw = _json(c.template_ids, {})
    out = {r: int(v) for r, v in raw.items() if r in ROLES and isinstance(v, int) and v > 0}
    if c.template_id and "capa" not in out and c.visual_format == "carrossel":
        out["capa"] = c.template_id
    return out


def piece_template(db, c: ContentItem, role: str = "") -> LayoutAsset | None:
    """Template da peça no papel pedido: o escolhido (do mesmo projeto e formato) ou, sem escolha, o padrão do projeto."""
    fmt = c.visual_format
    role = norm_role(fmt, role or "capa") if fmt == "carrossel" else ""
    tid = piece_template_ids(c).get(role) if fmt == "carrossel" else c.template_id
    if tid:
        t = db.get(LayoutAsset, tid)
        if (t is None or t.kind != "template" or t.project_id != c.project_id or t.format != fmt
                or t.status not in ("approved", "proposed")):
            raise ProviderError(f"o template escolhido ({role_label(fmt, role)}) não existe mais ou não serve para este formato: "
                                "escolha outro")
        return t
    return approved_template(db, c.project_id, fmt, role)


# ---------- carrossel: capa + páginas + CTA ----------

MAX_PAGES = 8
DEFAULT_CTA = {"role": "cta", "title": "Gostou? Salve e compartilhe", "text": ""}


def clean_slides(raw) -> list[dict]:
    """Páginas do meio e o CTA final, na ordem; o CTA é sempre o último e só um. Textos aparados."""
    pages, cta = [], None
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        item = {"role": "cta" if s.get("role") == "cta" else "pagina",
                "title": str(s.get("title") or s.get("titulo") or "").strip()[:120],
                "text": str(s.get("text") or s.get("texto") or "").strip()[:300]}
        if isinstance(s.get("image_id"), int):
            item["image_id"] = s["image_id"]
        if isinstance(s.get("photo_id"), int):
            item["photo_id"] = s["photo_id"]
        if isinstance(s.get("assets"), list):
            ids = [int(x) for x in s["assets"] if isinstance(x, int)][:ast.MAX_SENT]
            if ids:
                item["assets"] = ids
        if str(s.get("media") or s.get("imagem") or "").strip():
            item["media"] = str(s.get("media") or s.get("imagem")).strip()[:600]
        if str(s.get("prompt") or "").strip():
            item["prompt"] = str(s["prompt"]).strip()[:6000]  # prompt da imagem desta página, editado pelo owner
        if s.get("photo_full"):
            item["photo_full"] = True  # a foto da página já é a arte inteira (cenário refeito dentro do template)
        if s.get("manual_assets"):
            item["manual_assets"] = True  # só as referências escolhidas pelo owner, sem escolha automática
        if item["role"] == "cta":
            cta = item
        elif len(pages) < MAX_PAGES and (item["title"] or item["text"]):
            pages.append(item)
    return pages + [cta or dict(DEFAULT_CTA)]


def default_slides(c: ContentItem) -> list[dict]:
    """Sem páginas definidas: cada parágrafo do texto (depois do primeiro) vira uma página, e um CTA padrão no fim."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", c.body or "") if p.strip()]
    pages = [{"role": "pagina", "title": "", "text": p[:300]} for p in (paras[1:] or paras)[:MAX_PAGES]]
    return clean_slides(pages)


def piece_slides(c: ContentItem) -> list[dict]:
    if c.visual_format != "carrossel":
        return []
    raw = _json(c.slides, [])
    return clean_slides(raw) if raw else default_slides(c)


def _drop_slide_images(db, slides: list[dict], photos: bool = False) -> None:
    """Apaga a imagem final de cada página (e, com photos, também a foto que a IA fez para ela)."""
    for s in slides:
        for key in (("image_id", "photo_id") if photos else ("image_id",)):
            if s.get(key) and (old := db.get(LayoutAsset, s[key])) is not None:
                db.delete(old)
            s.pop(key, None)
        if photos:
            s.pop("photo_full", None)


def drop_slides(db, c: ContentItem, photos: bool = True) -> None:
    """Apaga as imagens das páginas e do CTA (a capa fica em image_id)."""
    raw = _json(c.slides, [])
    _drop_slide_images(db, raw, photos)
    c.slides = json.dumps(raw, ensure_ascii=False) if raw else ""


def set_slides(db, c: ContentItem, slides) -> None:
    """O owner editou as páginas: guarda os textos novos e apaga as imagens antigas (são refeitas em seguida).
    A foto de uma página só fica se a descrição dela não mudou (foto custa; texto novo não precisa de foto nova)."""
    old = _json(c.slides, [])
    new = clean_slides(slides)
    for i, s in enumerate(new):
        o = old[i] if i < len(old) and isinstance(old[i], dict) and old[i].get("role") == s["role"] else {}
        for key in ("media", "prompt", "assets", "manual_assets"):
            if key not in s and o.get(key):
                s[key] = o[key]
        same_img = all(s.get(k, "") == o.get(k, "") for k in ("media", "prompt", "assets", "manual_assets"))
        if o.get("photo_id") and same_img and "photo_id" not in s:
            s["photo_id"], o["photo_id"] = o["photo_id"], None
            if o.get("photo_full"):
                s["photo_full"] = True
    _drop_slide_images(db, old, photos=True)
    c.slides = json.dumps(new, ensure_ascii=False)


def _replace_asset(db, c: ContentItem, attr: str, asset: LayoutAsset) -> None:
    old = db.get(LayoutAsset, getattr(c, attr)) if getattr(c, attr) else None
    db.add(asset)
    db.flush()
    setattr(c, attr, asset.id)
    if old is not None:
        db.delete(old)


def set_piece_image(db, c: ContentItem, data: bytes, mime: str, model: str = "", prompt: str = "") -> None:
    """Guarda a imagem final da peça (a capa, no carrossel) e apaga a anterior (uma imagem por peça)."""
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


def drop_bg(db, c: ContentItem) -> None:
    if c.bg_id:
        old = db.get(LayoutAsset, c.bg_id)
        if old is not None:
            db.delete(old)
        c.bg_id = None


def _plate(db, template: LayoutAsset) -> LayoutAsset | None:
    return db.get(LayoutAsset, template.plate_id) if template.plate_id else None


def cover_layers(db, c: ContentItem, template: LayoutAsset) -> tuple[tuple[bytes, str] | None, tuple[bytes, str] | None]:
    """(fundo, mídia) da capa. Fundo feito pela IA (cenário ou foto encaixada) tem prioridade; senão a arte do layout,
    com a foto/vídeo da peça na janela {{midia}}, atrás da arte transparente, ou (vídeo) no lugar da arte."""
    from . import media as med
    from . import motion

    if c.bg_id and (bg := db.get(LayoutAsset, c.bg_id)) is not None:
        return (bg.data, bg.mime), None
    plate = _plate(db, template)
    art = (plate.data, plate.mime) if plate is not None else None
    src = med.norm_source(c.media_source)
    photo_box = "midia" in template_boxes(template)
    m = med.piece_media(db, c) if src in ("gerar_video", "upload", "link") or (src == "gerar_imagem" and photo_box) else None
    if m is None:
        return art, None
    layer = (m.data, m.mime)
    if motion.has_media_slot(template.html) or (art is not None and has_alpha(*art)):
        return art, layer
    if med.is_video(m.mime):
        return None, layer  # vídeo em tela cheia com o texto do template por cima (a arte opaca esconderia o vídeo)
    return layer, None  # template antigo, sem arte: a foto vira o fundo


def _render(db, c: ContentItem, template: LayoutAsset, bg, media, title: str, text: str) -> bytes:
    project = db.get(GrowthProject, c.project_id)
    from . import motion

    kw = {"media": media} if media is not None else {}
    return motion.render_still(template.html, c.visual_format, bg, title, text, project.name if project else "", **kw)


def render_piece(db, c: ContentItem, bg: LayoutAsset | None, template: LayoutAsset) -> bytes:
    """Capa com um fundo explícito (compatível com o fluxo antigo) ou com as camadas da peça."""
    title, text = piece_texts(c, text_limits(template))
    if bg is not None:
        return _render(db, c, template, (bg.data, bg.mime), None, title, text)
    layers = cover_layers(db, c, template)
    return _render(db, c, template, layers[0], layers[1], title, text)


def slide_wants_photo(db, c: ContentItem, s: dict) -> bool:
    """A página ganha uma foto própria da IA: carrossel no modo template e um template com janela de foto (caixa {{midia}} do
    editor, janela do HTML ou arte transparente). A mídia da capa pode ser qualquer uma (subida, link, vídeo): a janela da página
    nunca fica vazia, desde que haja modelo de imagem. Só "nenhuma" (o owner não quer foto) deixa a janela de fora."""
    from . import media as med
    from . import motion

    if c.visual_format != "carrossel" or (c.image_mode or "template") != "template":
        return False
    if med.norm_source(c.media_source) == "nenhuma":
        return False
    if med.norm_source(c.media_source) != "gerar_imagem":
        try:
            image_choice(db)  # a capa não é gerada por IA: só cria foto de página se o modelo de imagem estiver configurado
        except ProviderError:
            return False
    try:
        t = piece_template(db, c, s["role"])
    except ProviderError:
        return False
    if t is None:
        return False
    plate = _plate(db, t)
    if plate is None:  # sem arte: a foto da página vira o fundo (senão todas as páginas ficam iguais à capa, sem imagem)
        return True
    if "midia" in template_boxes(t) or motion.has_media_slot(t.html) or has_alpha(plate.data, plate.mime):
        return True
    # arte opaca sem janela (upload "texto padrão"): como na capa, a IA refaz o cenário DENTRO da arte da página.
    # Template do editor de caixas sem caixa de imagem é escolha do owner (a arte nunca vai à IA): fica só arte e texto.
    return not template_boxes(t)


def slide_photo_kind(db, c: ContentItem, s: dict) -> str:
    """Como a imagem da página é feita: "livre" (modos sem template: a imagem inteira sai do modelo), "janela" (foto na janela
    de foto do template), "cenario" (a IA refaz o cenário dentro da arte opaca), "fundo" (template sem arte: a foto é o fundo)
    ou "" (o template não tem lugar para foto: só arte e texto)."""
    if c.visual_format != "carrossel":
        return ""
    if (c.image_mode or "template") != "template":
        return "livre"
    if not slide_wants_photo(db, c, s):
        return ""
    t = piece_template(db, c, s["role"])
    plate = _plate(db, t) if t is not None else None
    if plate is None:
        return "fundo"
    if "midia" in template_boxes(t) or _has_slot(t, plate):
        return "janela"
    return "cenario"


def _has_slot(t: LayoutAsset, plate: LayoutAsset) -> bool:
    from . import motion

    return motion.has_media_slot(t.html) or has_alpha(plate.data, plate.mime)


def build_slide_photo_prompt(project_name: str, brief: str, c: ContentItem, s: dict, ratio: str, feedback: str = "",
                             has_refs: bool = False) -> str:
    subject = (s.get("media") or "").strip() or " — ".join(x for x in (s.get("title"), s.get("text")) if x) or c.media or c.title
    lines = [
        f"Create ONE photograph ({ratio}) for slide of a social media carousel of the brand \"{project_name}\". It goes inside a "
        "picture window of an existing layout, so deliver ONLY the photo, edge to edge.",
        f"Slide subject: {subject[:700]}",
        f"Carousel theme (keep the same look across all slides): {(c.media.strip() or c.title or c.body)[:400]}",
        "ONE single continuous scene, never panels, frames, collage or split screen. " + text_rule(has_refs),
        f"Brand style brief (mood and palette only): {brief.strip()[:600]}" if brief.strip() else "",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def build_free_slide_prompt(project_name: str, brief: str, c: ContentItem, s: dict, ratio: str, mode: str,
                            has_refs: bool = False, feedback: str = "") -> str:
    subject = (s.get("media") or "").strip() or " — ".join(x for x in (s.get("title"), s.get("text")) if x) or c.media or c.title
    lines = [
        f"Create a finished carousel slide image ({ratio}) for a social media post of the brand \"{project_name}\".",
        f"Slide subject: {subject[:700]}",
        f"Carousel theme (keep the same look across all slides): {(c.image_prompt.strip() or c.media.strip() or c.title or c.body)[:400]}",
        "ONE single continuous scene. Use the attached assets as the real objects of the game/brand: reproduce them faithfully, "
        "never invent a different board, card, character or logo." if mode == "reference" else "",
        f"Brand style brief: {brief.strip()[:800]}" if brief.strip() else "",
        ("Keep the content of the reference images as it is, including any text or messages in them; do not add any headline "
         "or new sentence on the image." if has_refs else "Do not write any headline or sentence on the image; no garbled letters."),
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def build_slide_scenery_prompt(project_name: str, brief: str, c: ContentItem, s: dict, feedback: str = "",
                               has_refs: bool = False) -> str:
    """Página sobre arte opaca sem janela: a IA troca só o cenário fotográfico da arte da página (como na capa)."""
    name, ratio, _ = FORMATS[c.visual_format]
    subject = (s.get("media") or "").strip() or " — ".join(x for x in (s.get("title"), s.get("text")) if x) or c.media or c.title
    lines = [
        f"Edit the attached artwork of the brand \"{project_name}\" ({name}, one page of a carousel). KEEP EXACTLY as they are: the "
        "logo/emblem, the frame, borders, ornaments, dividers, the panel or area reserved for text, the composition and the colors "
        f"of those elements. CHANGE ONLY the photographic scenery/background to fit this page: {subject[:600]}.",
        "The panel reserved for text must stay EXACTLY as it is in the attached artwork: empty and plain, with no photo, people, "
        "objects or scenery inside it. Make ONE single new scene; do NOT add frames, picture boxes, cards, panels or collage.",
        f"Carousel theme (same look on every page): {(c.media.strip() or c.title or c.body)[:300]}",
        text_rule(has_refs),
        f"Brand style brief: {brief.strip()[:800]}" if brief.strip() else "",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def slide_plan(db, c: ContentItem, s: dict, feedback: str = "") -> dict:
    """O que vai ao modelo para a imagem de UMA página: tipo, prompt montado, ativos escolhidos e o ratio. Não chama modelo
    nenhum (usa só a escolha automática já guardada), então serve de prévia e é a mesma conta da geração."""
    kind = slide_photo_kind(db, c, s)
    plan = {"kind": kind, "prompt": "", "picked": [], "extra": [], "ratio": FORMATS[c.visual_format][1], "reason": ""}
    if not kind:
        plan["reason"] = ("o template desta página não tem caixa de imagem: abra o editor em Layouts e marque onde vai a imagem"
                          if (c.image_mode or "template") == "template" else "")
        from . import media as med

        if (c.image_mode or "template") == "template" and med.norm_source(c.media_source) == "nenhuma":
            plan["reason"] = "a fonte da mídia da peça está em \"Só o layout\" (sem foto)"
        return plan
    project = db.get(GrowthProject, c.project_id)
    name, brief = (project.name if project else ""), ((project.style_brief if project else "") or "")
    picked = ast.for_slide(db, c, s)
    plan["picked"] = picked
    if kind == "livre":
        mode = c.image_mode
        if mode == "reference":
            picked += [a for a in style_refs(db, c.project_id) if a not in picked]
            picked = (picked or [a for a in ast.project_assets(db, c.project_id) if ast.is_image(a)])[:MAX_REFS_SENT]
            plan["picked"] = picked
        plan["prompt"] = _with_assets(build_free_slide_prompt(name, brief, c, s, plan["ratio"], mode, bool(picked), feedback),
                                      picked, 1, db, c)
        return plan
    t = piece_template(db, c, s["role"])
    box = template_boxes(t).get("midia") if t is not None else None
    w, h = CANVAS[c.visual_format]
    if kind == "janela":
        plan["ratio"] = nearest_ratio(box["w"] * w, box["h"] * h) if box else plan["ratio"]
        prompt = build_slide_photo_prompt(name, brief, c, s, plan["ratio"], feedback, bool(picked))
        plan["prompt"] = prompt + ("\n" + ast.prompt_lines(picked, 1) if picked else "")
    elif kind == "cenario":
        plate = _plate(db, t)
        plan["extra"] = [(plate.data, plate.mime)]
        prompt = build_slide_scenery_prompt(name, brief, c, s, feedback, bool(picked))
        plan["prompt"] = _with_assets(prompt, picked, 2, db, c)
    else:  # fundo
        prompt = build_slide_photo_prompt(name, brief, c, s, plan["ratio"], feedback, bool(picked))
        plan["prompt"] = prompt + ("\n" + ast.prompt_lines(picked, 1) if picked else "")
    return plan


async def generate_slide_image(db, c: ContentItem, s: dict, feedback: str = "", override: str = "") -> None:
    """Gera a imagem de UMA página (ou do CTA), em qualquer modo, e a coloca no lugar dela:
    modos livres: a imagem inteira sai do modelo (s["image_id"]); modo template: foto/cenário em s["photo_id"] e a página é
    renderizada de novo no template dela (sem IA). O prompt é o do owner (override, ou s["prompt"]) ou o montado."""
    await ast.auto_pick(db, c)
    plan = slide_plan(db, c, s, feedback)
    kind = plan["kind"]
    if not kind:
        raise ProviderError(plan["reason"] or "esta página não tem lugar para imagem")
    choice = image_choice(db, log=True, project_id=c.project_id)
    if kind == "livre" and c.image_mode == "reference" and not plan["picked"]:
        raise ProviderError("Suba ao menos um ativo de imagem deste projeto em Conteúdo › Ativos (ou escolha outro modo de imagem).")
    prompt = (override or s.get("prompt") or "").strip() or plan["prompt"]
    refs = plan["extra"] + [(a.data, a.mime) for a in plan["picked"]]
    with calllog.context("image", c.project_id):
        data, mime = await request_image(choice.model, prompt, refs, plan["ratio"])
    if kind == "livre":
        a = LayoutAsset(project_id=c.project_id, kind="piece", format=c.visual_format, mime=mime, data=data,
                        model=choice.model, prompt=prompt, role=s["role"])
        key = "image_id"
    else:
        a = LayoutAsset(project_id=c.project_id, kind="media", format=c.visual_format, mime=mime, data=data,
                        model=choice.model, prompt=prompt, role=s["role"])
        key = "photo_id"
    db.add(a)
    db.flush()
    if s.get(key) and (old := db.get(LayoutAsset, s[key])) is not None:
        db.delete(old)
    s[key] = a.id
    if kind == "cenario":
        s["photo_full"] = True
    else:
        s.pop("photo_full", None)


async def generate_free_slide(db, c: ContentItem, s: dict, mode: str, feedback: str = "") -> None:
    await generate_slide_image(db, c, s, feedback)


def _slide_layers(db, t: LayoutAsset, s: dict, cover_bg):
    """(fundo, foto) de uma página: a arte do template com a foto da página na janela; sem arte, a foto vira o fundo."""
    from . import motion

    plate = _plate(db, t)
    art = (plate.data, plate.mime) if plate is not None else cover_bg
    photo = db.get(LayoutAsset, s["photo_id"]) if s.get("photo_id") else None
    if photo is None:
        return art, None
    layer = (photo.data, photo.mime)
    if s.get("photo_full"):
        return layer, None  # a IA já refez a arte inteira da página: o texto vai por cima
    if motion.has_media_slot(t.html) or (plate is not None and has_alpha(plate.data, plate.mime)) or "midia" in template_boxes(t):
        return art, layer
    return layer, None


def render_slide(db, c: ContentItem, s: dict, cover_bg) -> None:
    """Imagem final de UMA página (ou do CTA), sem IA: arte do template + texto (+ a foto da página, se já existe).
    Troca a imagem anterior da página. Levanta ProviderError se o template não existe ou o renderizador falha."""
    t = piece_template(db, c, s["role"])
    if t is None:
        raise ProviderError(f"defina o template de {role_label('carrossel', s['role'])} em Layouts")
    bg, layer = _slide_layers(db, t, s, cover_bg)
    lt, ls = text_limits(t)
    png = _render(db, c, t, bg, layer, fit_text(s["title"], lt), fit_text(s["text"], ls))
    a = LayoutAsset(project_id=c.project_id, kind="piece", format=c.visual_format, mime="image/png", data=png,
                    model="template", role=s["role"])
    db.add(a)
    db.flush()
    if s.get("image_id") and (old := db.get(LayoutAsset, s["image_id"])) is not None:
        db.delete(old)
    s["image_id"] = a.id


def render_slides(db, c: ContentItem, cover_bg: tuple[bytes, str] | None) -> str:
    """Páginas e CTA do carrossel, cada um no seu template (sem IA: só a arte do layout, o texto e a foto que a página já tem).
    -> erros, se houver."""
    slides = piece_slides(c)
    _drop_slide_images(db, slides)
    errors = []
    for s in slides:
        try:
            render_slide(db, c, s, cover_bg)
        except ProviderError as e:
            errors.append(f"{role_label('carrossel', s['role'])}: {e}")
    c.slides = json.dumps(slides, ensure_ascii=False)
    return "; ".join(errors)[:500]


async def generate_slide_photo(db, c: ContentItem, s: dict, feedback: str = "", override: str = "") -> None:
    """Foto da página (1 imagem paga), guardada em s["photo_id"]."""
    await generate_slide_image(db, c, s, feedback, override)


def _finish(db, c: ContentItem, template: LayoutAsset, final: bytes, model: str, prompt: str) -> None:
    """Guarda a capa e, no carrossel, refaz as páginas e o CTA."""
    _replace_asset(db, c, "image_id", LayoutAsset(project_id=c.project_id, kind="piece", format=c.visual_format,
                                                   mime="image/png", data=final, model=model, prompt=prompt))
    err = ""
    if c.visual_format == "carrossel":
        err = render_slides(db, c, cover_layers(db, c, template)[0])
    c.image_error = err
    db.commit()


def rerender_piece(db, c: ContentItem) -> None:
    """Texto da imagem mudou: refaz a imagem final (e as páginas), com o mesmo fundo (sem gastar IA). Erros em image_error."""
    if not (c.image_id and c.visual_format):
        return
    if (c.image_mode or "template") != "template":
        return
    try:
        template = piece_template(db, c)
    except ProviderError:
        return
    if template is None:
        return
    try:
        final = render_piece(db, c, None, template)
        _finish(db, c, template, final, "template", "")
        drop_video(db, c)
        db.commit()
    except ProviderError as e:
        db.rollback()
        c.image_error = str(e)[:500]
        db.commit()


async def build_slide(db, c: ContentItem, i: int, feedback: str = "", override: str = "", force: bool = False) -> None:
    """Deixa UMA página (índice i em piece_slides) pronta: a imagem dela é gerada (se falta, ou sempre com force) e a página é
    montada no template dela. Modos livres: a imagem do modelo já é a página. Quem chama grava c.slides e faz commit."""
    slides = piece_slides(c)
    s = slides[i]
    kind = slide_photo_kind(db, c, s)
    if kind == "livre":
        if force or not s.get("image_id"):
            await generate_slide_image(db, c, s, feedback, override)
    else:
        if kind and (force or not s.get("photo_id")):
            await generate_slide_image(db, c, s, feedback, override)
        cover = await asyncio.to_thread(cover_layers, db, c, piece_template(db, c))
        await asyncio.to_thread(render_slide, db, c, s, cover[0])
    c.slides = json.dumps(slides, ensure_ascii=False)
    db.commit()


async def generate_slide_photos(db, c: ContentItem) -> str:
    """Imagem de cada página/CTA do carrossel que ainda não tem (as que já têm ficam), em qualquer modo de imagem.
    -> erros (vazio = tudo certo). Uma página que falha não impede as outras."""
    if c.visual_format != "carrossel" or not c.image_id:
        return ""
    errors = []
    for i in range(len(piece_slides(c))):
        s = piece_slides(c)[i]
        kind = slide_photo_kind(db, c, s)
        if kind == "livre" and s.get("image_id"):
            continue
        if kind != "livre" and (s.get("photo_id") or not kind):
            continue
        try:
            await build_slide(db, c, i)
        except ProviderError as e:
            db.rollback()
            errors.append(f"{role_label('carrossel', s['role'])}: {e}")
    return "; ".join(errors)[:500]


async def generate_piece_image(db, c: ContentItem, feedback: str = "", prompt: str = "") -> None:
    """Capa da peça e, no carrossel, a foto de cada página e do CTA (ver generate_cover)."""
    await generate_cover(db, c, feedback, prompt)
    if c.visual_format == "carrossel" and c.image_id and not c.media_job and not prompt.strip():
        err = await generate_slide_photos(db, c)
        if err:
            c.image_error = err
            db.commit()


async def generate_cover(db, c: ContentItem, feedback: str = "", override: str = "") -> None:
    """Imagem da peça no layout escolhido, com a mídia da fonte escolhida:
    gerar_imagem: a IA refaz só o cenário dentro da arte (1 imagem paga); gerar_video: pede o vídeo (fica pronto em minutos);
    upload/link: a foto ou o vídeo entram no layout (sem IA se o template tem a janela {{midia}}); nenhuma: só arte e texto."""
    from . import media as med

    if not c.visual_format:
        raise ProviderError("esta peça não é visual (story, carrossel ou reel)")
    mode = c.image_mode or "template"
    await ast.auto_pick(db, c)  # escolhe sozinho as referências do jogo que a peça cita (tabuleiro, cartas...)
    if mode != "template":
        return await _generate_free_image(db, c, mode, feedback, override)
    template = piece_template(db, c)
    if template is None:
        raise ProviderError(f"Defina o template de {role_label(c.visual_format, 'capa')} deste projeto em Layouts antes de gerar a imagem.")
    src = med.norm_source(c.media_source)
    project = db.get(GrowthProject, c.project_id)
    name, brief = (project.name if project else ""), ((project.style_brief if project else "") or "")
    plate = _plate(db, template)
    boxes = template_boxes(template)
    if src == "gerar_imagem" and boxes:
        return await _generate_box_photo(db, c, template, boxes, name, brief, feedback, override)
    if src == "gerar_imagem":
        choice = image_choice(db, log=True, project_id=c.project_id)
        picked = ast.for_piece(db, c)
        if plate is not None:  # arte aprovada sem texto: o modelo só troca o cenário e mantém logo e moldura
            prompt = _with_assets(build_scenery_prompt(name, brief, c, feedback), picked, 2, db, c)
            refs = [(plate.data, plate.mime)] + [(a.data, a.mime) for a in picked]
        else:  # template antigo, sem arte: fundo novo a partir dos ativos da peça (ou do estilo do projeto)
            rows = picked or style_refs(db, c.project_id)
            prompt = _with_assets(build_background_prompt(name, brief, c, feedback), picked, 1, db, c)
            refs = [(a.data, a.mime) for a in rows[:MAX_REFS_SENT]]
        prompt = override.strip() or prompt
        with calllog.context("image", c.project_id):
            data, mime = await request_image(choice.model, prompt, refs, FORMATS[c.visual_format][1])
        bg = LayoutAsset(project_id=c.project_id, kind="background", format=c.visual_format, mime=mime, data=data,
                         model=choice.model, prompt=prompt)
        with calllog.context("image", c.project_id):
            final = await asyncio.to_thread(render_piece, db, c, bg, template)  # se o renderizador cair, o fundo não fica órfão
        _replace_asset(db, c, "bg_id", bg)
        drop_video(db, c)
        return await asyncio.to_thread(_finish, db, c, template, final, choice.model, prompt)

    m = med.piece_media(db, c) if src != "nenhuma" else None
    if src == "gerar_video" and m is None:
        if c.media_job:
            raise ProviderError("o vídeo ainda está sendo gerado; a imagem aparece sozinha quando ele ficar pronto")
        return await med.start_video(db, c)
    if src == "link" and m is None and c.media_url:
        try:
            data, mime = await med.download(c.media_url)
        except ValueError as e:
            raise ProviderError(f"link: {e}") from e
        m = med.set_media(db, c, data, mime, "link", c.media_url)
    if src in ("upload", "link") and m is None:
        raise ProviderError("suba a foto ou o vídeo da peça" if src == "upload" else "cole o link da foto ou do vídeo")
    drop_bg(db, c)
    drop_video(db, c)
    model, prompt = "template", ""
    from . import motion

    if (m is not None and not med.is_video(m.mime) and plate is not None and not motion.has_media_slot(template.html)
            and not has_alpha(plate.data, plate.mime)):
        # o layout não tem janela para a foto: a IA encaixa a foto na arte (1 imagem paga), mantendo logo e moldura
        choice = image_choice(db, log=True, project_id=c.project_id)
        picked = ast.for_piece(db, c, MAX_REFS_SENT - 2)
        prompt = override.strip() or _with_assets(build_merge_prompt(name, c, feedback), picked, 3, db, c)
        with calllog.context("image", c.project_id):
            data, mime = await request_image(choice.model, prompt, [(plate.data, plate.mime), (m.data, m.mime)]
                                             + [(a.data, a.mime) for a in picked], FORMATS[c.visual_format][1])
        bg = LayoutAsset(project_id=c.project_id, kind="background", format=c.visual_format, mime=mime, data=data,
                         model=choice.model, prompt=prompt)
        with calllog.context("image", c.project_id):
            final = await asyncio.to_thread(render_piece, db, c, bg, template)
        _replace_asset(db, c, "bg_id", bg)
        model = choice.model
    else:
        with calllog.context("piece_render", c.project_id):
            final = await asyncio.to_thread(render_piece, db, c, None, template)
    await asyncio.to_thread(_finish, db, c, template, final, model, prompt)


def build_photo_prompt(project_name: str, brief: str, c: ContentItem, ratio: str, feedback: str = "", has_refs: bool = False) -> str:
    """Foto para a caixa de imagem do template: só a foto, sem arte, moldura nem texto (a arte e o texto já estão no layout)."""
    lines = [
        f"Create ONE photograph ({ratio}) for a social media post of the brand \"{project_name}\". It goes inside a picture "
        "box of an existing layout, so deliver ONLY the photo, edge to edge.",
        f"Subject: {(c.image_prompt.strip() or c.media.strip() or c.title or c.body)[:800]}",
        "ONE single continuous scene: combine every attached reference INTO that one scene. Never show them as separate "
        "pictures, panels, frames, cards, collage, grid or split screen; no logos or user interface around it.",
        text_rule(has_refs),
        f"Brand style brief (mood and palette only): {brief.strip()[:800]}" if brief.strip() else "",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


async def _generate_box_photo(db, c: ContentItem, template: LayoutAsset, boxes: dict, name: str, brief: str,
                              feedback: str, override: str = "") -> None:
    """Template do editor: a IA não mexe na arte. Só cria a foto da caixa de imagem (se houver) e o texto entra nas caixas."""
    from . import media as med

    drop_bg(db, c)
    drop_video(db, c)
    box = boxes.get("midia")
    if box is None:  # template sem caixa de imagem: só a arte e o texto, sem custo
        with calllog.context("piece_render", c.project_id):
            final = await asyncio.to_thread(render_piece, db, c, None, template)
        return await asyncio.to_thread(_finish, db, c, template, final, "template", "")
    choice = image_choice(db, log=True, project_id=c.project_id)
    w, h = CANVAS[c.visual_format]
    ratio = nearest_ratio(box["w"] * w, box["h"] * h)
    picked = ast.for_piece(db, c)
    prompt = ast.rewrite_mentions(db, c, build_photo_prompt(name, brief, c, ratio, feedback, bool(picked)), picked, 1)
    if picked:
        prompt += "\n" + ast.prompt_lines(picked, 1)
    prompt = override.strip() or prompt
    with calllog.context("image", c.project_id):
        data, mime = await request_image(choice.model, prompt, [(a.data, a.mime) for a in picked], ratio)
    med.set_media(db, c, data, mime, "gerar_imagem", prompt)
    with calllog.context("piece_render", c.project_id):
        final = await asyncio.to_thread(render_piece, db, c, None, template)
    await asyncio.to_thread(_finish, db, c, template, final, choice.model, prompt)


async def _generate_free_image(db, c: ContentItem, mode: str, feedback: str = "", override: str = "") -> None:
    """Sem template: a imagem sai pronta do modelo, só das referências do projeto (reference) ou só de um prompt (prompt)."""
    choice = image_choice(db, log=True, project_id=c.project_id)
    project = db.get(GrowthProject, c.project_id)
    name, brief = (project.name if project else ""), ((project.style_brief if project else "") or "")
    refs: list[tuple[bytes, str]] = []
    if mode == "reference":
        if c.ref_id:
            rows = [db.get(LayoutAsset, c.ref_id)]
        else:  # os ativos da peça e o estilo do projeto (logo, paleta)
            rows = ast.for_piece(db, c, MAX_REFS_SENT)
            rows += [a for a in style_refs(db, c.project_id) if a not in rows]
        rows = [a for a in rows if a is not None and a.kind == "reference" and a.project_id == c.project_id
                and ast.is_image(a)]
        if not rows and not c.ref_id:  # nada combinou com o assunto: vai a biblioteca do projeto
            rows = [a for a in ast.project_assets(db, c.project_id) if ast.is_image(a)]
        rows = rows[:MAX_REFS_SENT]
        if not rows:
            raise ProviderError("Suba ao menos um ativo de imagem deste projeto em Conteúdo › Ativos (ou escolha outro modo de imagem).")
        refs = [(a.data, a.mime) for a in rows]
        prompt = _with_assets(build_reference_prompt(name, brief, c, feedback), rows, 1, db, c)
    else:
        picked = ast.for_piece(db, c) if ast.chosen_ids(c) or ast.mentioned(db, c) else []  # modo prompt: só os ativos que o owner escolheu
        refs = [(a.data, a.mime) for a in picked]
        prompt = _with_assets(build_free_prompt(name, brief, c, feedback, bool(picked)), picked, 1, db, c)
    prompt = override.strip() or prompt
    with calllog.context("image", c.project_id):
        data, mime = await request_image(choice.model, prompt, refs, FORMATS[c.visual_format][1])
    drop_bg(db, c)  # sem template não há fundo separado: o fundo e o vídeo de antes não valem mais
    drop_video(db, c)
    drop_slides(db, c)
    set_piece_image(db, c, data, mime, choice.model, prompt)


def can_video(db, c: ContentItem) -> bool:
    """Dá para montar o vídeo animado: modo template com a capa pronta (fundo gerado, mídia ou só a arte)."""
    return bool(c.visual_format and c.image_id and (c.image_mode or "template") == "template")


async def generate_piece_video(db, c: ContentItem) -> None:
    """Anima o template da peça com o texto e o fundo (e o vídeo de fonte, se houver) e guarda o MP4. Sem IA, sem custo."""
    if not can_video(db, c):
        raise ProviderError("o vídeo usa a imagem da peça no layout: gere a imagem primeiro")
    template = piece_template(db, c)
    if template is None:
        raise ProviderError("falta o template do projeto para montar o vídeo")
    bg, layer = cover_layers(db, c, template)
    project = db.get(GrowthProject, c.project_id)
    title, text = piece_texts(c)
    from . import motion

    kw = {"media": layer} if layer is not None else {}
    with calllog.context("video_render", c.project_id):
        video = await motion.render_video(template.html, c.visual_format, bg, title, text, project.name if project else "", **kw)
    _replace_asset(db, c, "video_id", LayoutAsset(project_id=c.project_id, kind="video", format=c.visual_format,
                                                   mime="video/mp4", data=video, model="hyperframes"))
    db.commit()


def _waiting_for_owner(db, c: ContentItem) -> bool:
    """Peças que a passada automática não mexe: vídeo de IA (caro, só no clique), mídia que o owner ainda não mandou."""
    from . import media as med

    if (c.image_mode or "template") != "template":
        return False
    src = med.norm_source(c.media_source)
    if src == "gerar_video":
        return True
    if src == "upload":
        return med.piece_media(db, c) is None
    if src == "link":
        return med.piece_media(db, c) is None and not c.media_url
    return False


async def autogen_pending() -> int:
    """Passada do agendador: leva as peças que aguardam aprovação até "pronta para aprovar" (app/pipeline.py).

    Texto que falta, capa, imagem de cada página e CTA: cada parte tem até MAX_ATTEMPTS tentativas, com espera entre elas, e
    só a parte que falhou é refeita. No máximo AUTOGEN_PER_TICK peças por passada (limita o custo). Sem template, sem modelo
    de imagem ou esperando a mídia do owner, a peça espera sem gastar nada (o status diz o quê).
    """
    from . import pipeline

    done = 0
    db = new_session()
    try:
        rows = db.query(ContentItem).filter(ContentItem.status == "pending").order_by(ContentItem.id).limit(300).all()
        for c in rows:
            if done >= AUTOGEN_PER_TICK:
                break
            try:
                if not pipeline.should_run(db, c):
                    continue
                await pipeline.run_piece(db, c)
            except Exception:  # noqa: BLE001 - uma peça com problema não trava as outras
                db.rollback()
                log.exception("linha de produção da peça %s", c.id)
            done += 1
    finally:
        db.close()
    return done
