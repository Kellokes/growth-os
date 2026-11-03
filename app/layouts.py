"""Estúdio de layouts: cada projeto tem referências visuais e layouts gerados que servem de base para
stories, carrosséis e reels.

Infra para qualquer projeto: nada aqui sabe de Zombie Party, A Ilha ou Take One. A imagem é gerada pelo
modelo de imagem configurado em /settings (atividade "image", só OpenRouter) e cada geração é um clique do
owner: nunca roda sozinha nem em laço.
"""
import base64

import httpx

from . import modelcfg
from .db import ContentItem, GrowthProject, LayoutAsset, new_session
from .providers.openrouter import OPENROUTER_URL, _api_key, _error_detail, _explain
from .providers.base import ProviderError

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
        "This is a visual REFERENCE that will guide future posts, not a finished post: show the composition, "
        "color palette, typography style, graphic elements, margins and where the logo, headline and body text go.",
        "Use placeholder text blocks or lorem-style text; do not invent real claims, prices or people.",
    ]
    if brief.strip():
        lines.append(f"Brand style brief from the owner: {brief.strip()[:3000]}")
    if notes:
        lines.append("Reference images are attached. What each one shows: " + " | ".join(n[:200] for n in notes if n))
    else:
        lines.append("Reference images, if attached, define the visual style: follow their palette, mood and typography.")
    if feedback.strip():
        lines.append(f"Adjustments requested by the owner for this round: {feedback.strip()[:1000]}")
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


async def request_image(model: str, prompt: str, refs: list[tuple[bytes, str]], ratio: str) -> tuple[bytes, str]:
    """Pede UMA imagem ao OpenRouter, com as referências junto."""
    key = _api_key()
    if not key:
        raise ProviderError("OpenRouter sem chave: defina GOS_OPENROUTER_API_KEY no .env do VPS")
    content: list[dict] = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": data_url(d, m)}} for d, m in refs[:MAX_REFS_SENT]]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": ratio},
    }
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
    return extract_image(payload)


def image_choice(db) -> modelcfg.Choice:
    """Modelo de imagem configurado em /settings. ProviderError com o que falta, em linguagem do owner."""
    c = modelcfg.resolve(db, "image")
    if c.provider != "openrouter" or not c.model:
        raise ProviderError("Escolha o modelo de imagem do OpenRouter em Modelos (atividade \"Imagens\") antes de gerar.")
    return c


def asset_json(a: LayoutAsset) -> dict:
    return {
        "id": a.id, "kind": a.kind, "format": a.format, "status": a.status, "note": a.note, "model": a.model,
        "mime": a.mime, "url": f"/api/layouts/assets/{a.id}", "created_at": a.created_at.isoformat(),
    }


# ---------- imagem de cada peça da Central de Conteúdo ----------

AUTOGEN_PER_TICK = 3  # imagens automáticas por passada do agendador (limita o custo)


def approved_layout(db, project_id: int, fmt: str) -> LayoutAsset | None:
    return (db.query(LayoutAsset).filter(LayoutAsset.project_id == project_id, LayoutAsset.kind == "layout",
                                         LayoutAsset.format == fmt, LayoutAsset.status == "approved").first())


def build_piece_prompt(project_name: str, brief: str, c: ContentItem, feedback: str = "") -> str:
    name, ratio, _ = FORMATS[c.visual_format]
    lines = [
        f"Create the final {name} image ({ratio}) for a social media post of the brand \"{project_name}\".",
        "The FIRST attached image is the approved brand layout: keep its composition, color palette, typography and "
        "logo placement. Any other attached images are brand references.",
        f"Post title: {c.title[:200]}" if c.title else "",
        f"Post text (for context; put only a short headline on the image): {c.body[:600]}",
        f"What the image must show: {c.media[:1000]}" if c.media.strip() else "",
        "Any text on the image must be in Brazilian Portuguese and spelled correctly.",
        f"Brand style brief: {brief.strip()[:1500]}" if brief.strip() else "",
        f"Adjustments requested by the owner: {feedback.strip()[:500]}" if feedback.strip() else "",
    ]
    return "\n".join(x for x in lines if x)


def set_piece_image(db, c: ContentItem, data: bytes, mime: str, model: str = "", prompt: str = "") -> None:
    """Guarda a imagem da peça e apaga a anterior (uma imagem por peça)."""
    old = db.get(LayoutAsset, c.image_id) if c.image_id else None
    a = LayoutAsset(project_id=c.project_id, kind="piece", format=c.visual_format, status="", mime=mime, data=data,
                    model=model, prompt=prompt)
    db.add(a)
    db.flush()
    c.image_id, c.image_error = a.id, ""
    if old is not None:
        db.delete(old)
    db.commit()


async def generate_piece_image(db, c: ContentItem, feedback: str = "") -> None:
    """Gera a imagem da peça no layout aprovado do projeto. ProviderError com o motivo em linguagem do owner."""
    if not c.visual_format:
        raise ProviderError("esta peça não é visual (story, carrossel ou reel)")
    layout = approved_layout(db, c.project_id, c.visual_format)
    if layout is None:
        raise ProviderError(f"Defina o layout de {FORMATS[c.visual_format][0]} deste projeto em Layouts antes de gerar a imagem.")
    choice = image_choice(db)
    project = db.get(GrowthProject, c.project_id)
    refs_rows = (db.query(LayoutAsset).filter(LayoutAsset.project_id == c.project_id, LayoutAsset.kind == "reference")
                 .order_by(LayoutAsset.id).limit(MAX_REFS_SENT - 1).all())
    refs = [(layout.data, layout.mime)] + [(a.data, a.mime) for a in refs_rows]
    prompt = build_piece_prompt(project.name if project else "", (project.style_brief if project else "") or "", c, feedback)
    data, mime = await request_image(choice.model, prompt, refs, FORMATS[c.visual_format][1])
    set_piece_image(db, c, data, mime, choice.model, prompt)


async def autogen_pending() -> int:
    """Passada do agendador: imagem para peças que aguardam aprovação, até AUTOGEN_PER_TICK por vez.

    Cada peça tem uma tentativa automática: se falhar, o motivo fica em image_error e só o botão do owner tenta de novo.
    Sem layout aprovado ou sem modelo de imagem configurado, a peça espera sem gastar nada.
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
            if approved_layout(db, c.project_id, c.visual_format) is None:
                continue
            try:
                image_choice(db)
                await generate_piece_image(db, c)
            except ProviderError as e:
                db.rollback()
                c.image_error = str(e)[:500]
                db.commit()
            done += 1
    finally:
        db.close()
    return done
