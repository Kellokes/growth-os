"""Mídia que entra no layout de uma peça: foto ou vídeo, de onde quer que venha.

Fontes (content_items.media_source):
  gerar_imagem: a IA de imagem (OpenRouter) faz o cenário dentro da arte do layout (fluxo de sempre);
  gerar_video:  a IA de vídeo (OpenRouter, atividade "Vídeos") gera o clipe; o layout e o texto vão por cima;
  upload:       o owner sobe uma foto ou um vídeo que já tem;
  link:         um arquivo direto (foto ou vídeo) coletado na internet; o servidor baixa uma vez e guarda;
  nenhuma:      só a arte do layout com o texto, sem foto nem vídeo (sem custo de IA).

Segurança do link: só https, sem redirecionar para endereço interno, sem IP privado, com teto de tamanho e o tipo
reconhecido pelos bytes (não pelo que o site diz).
"""
import asyncio
import ipaddress
import socket
import struct
from urllib.parse import urljoin, urlparse

import httpx

from . import calllog, modelcfg
from .db import ContentItem, GrowthProject, LayoutAsset, new_session
from .providers.base import ProviderError

SOURCES = {
    "gerar_imagem": "Gerar imagem com IA",
    "gerar_video": "Gerar vídeo com IA",
    "upload": "Subir arquivo (foto ou vídeo)",
    "link": "Link da internet (foto ou vídeo)",
    "nenhuma": "Sem mídia: só o layout e o texto",
}
_SOURCE_ALIASES = {"imagem": "gerar_imagem", "ia": "gerar_imagem", "video": "gerar_video", "vídeo": "gerar_video",
                   "arquivo": "upload", "url": "link", "sem": "nenhuma", "none": "nenhuma", "layout": "nenhuma"}
MAX_PHOTO_BYTES = 8_000_000
MAX_VIDEO_BYTES = 25_000_000
MAX_REDIRECTS = 3
DOWNLOAD_TIMEOUT = 60.0

VIDEO_URL = "https://openrouter.ai/api/v1/videos"
VIDEO_RATIO = {"story": "9:16", "reel": "9:16", "carrossel": "3:4"}  # 4:5 não existe nos modelos de vídeo


def norm_source(raw) -> str:
    s = str(raw or "").strip().lower()
    s = _SOURCE_ALIASES.get(s, s)
    return s if s in SOURCES else "gerar_imagem"


def detect(data: bytes) -> str | None:
    """PNG, JPEG, WebP ou MP4/MOV, reconhecidos pelos bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:10] == b"qt" else "video/mp4"
    return None


def is_video(mime: str) -> bool:
    return (mime or "").startswith("video/")


def check_media(data: bytes) -> str:
    """-> mime. ValueError com a mensagem para o owner."""
    if not data:
        raise ValueError("arquivo vazio")
    mime = detect(data)
    if mime is None:
        raise ValueError("use foto PNG, JPEG ou WebP, ou vídeo MP4/MOV")
    limit = MAX_VIDEO_BYTES if is_video(mime) else MAX_PHOTO_BYTES
    if len(data) > limit:
        raise ValueError(f"arquivo grande demais (máximo {limit // 1_000_000} MB para {'vídeo' if is_video(mime) else 'foto'})")
    return mime


def mp4_duration(data: bytes) -> float | None:
    """Duração do MP4/MOV lida da caixa mvhd (sem ffmpeg). None se não achar."""
    i = data.find(b"mvhd")
    if i < 4 or len(data) < i + 32:
        return None
    version = data[i + 4]
    try:
        if version == 1:
            timescale, duration = struct.unpack(">IQ", data[i + 24:i + 36])
        else:
            timescale, duration = struct.unpack(">II", data[i + 16:i + 24])
    except struct.error:
        return None
    if not timescale:
        return None
    return duration / timescale


# ---------- link da internet ----------

def _public_host(host: str) -> None:
    """Recusa nome que resolve para rede interna (SSRF): loopback, privada, link-local, reservada, multicast."""
    if not host:
        raise ValueError("link sem endereço")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError("não achei esse endereço") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("link para endereço interno não é aceito")


def check_url(url: str) -> str:
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ValueError("use um link https direto para o arquivo (foto ou vídeo)")
    if u.port not in (None, 443):
        raise ValueError("link com porta diferente de 443 não é aceito")
    return url


async def download(url: str) -> tuple[bytes, str]:
    """Baixa um arquivo público por https, seguindo no máximo 3 redirecionamentos (cada um checado de novo)."""
    url = check_url(url)
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await asyncio.to_thread(_public_host, urlparse(url).hostname or "")
            try:
                async with client.stream("GET", url, headers={"User-Agent": "GrowthOS/1.0", "Accept": "image/*,video/*"}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        url = check_url(urljoin(url, resp.headers.get("location", "")))
                        continue
                    if resp.status_code != 200:
                        raise ValueError(f"o site respondeu {resp.status_code}")
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > MAX_VIDEO_BYTES:
                            raise ValueError(f"arquivo grande demais (máximo {MAX_VIDEO_BYTES // 1_000_000} MB)")
            except httpx.HTTPError as e:
                raise ValueError(f"não consegui baixar o link ({type(e).__name__})") from e
            data = bytes(buf)
            try:
                return data, check_media(data)
            except ValueError as e:
                if data[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
                    raise ValueError("o link abre uma página, não o arquivo: copie o endereço da foto ou do vídeo em si") from e
                raise
    raise ValueError("redirecionamentos demais")


# ---------- guardar a mídia da peça ----------

def set_media(db, c: ContentItem, data: bytes, mime: str, origin: str, prompt: str = "") -> LayoutAsset:
    """Troca a mídia da peça (uma por peça). A imagem final e o vídeo de antes deixam de valer e são refeitos."""
    old = db.get(LayoutAsset, c.media_id) if c.media_id else None
    a = LayoutAsset(project_id=c.project_id, kind="media", format=c.visual_format, mime=mime, data=data, model=origin,
                    prompt=prompt[:4000])
    db.add(a)
    db.flush()
    c.media_id = a.id
    if old is not None:
        db.delete(old)
    c.image_error = ""
    db.commit()
    return a


def piece_media(db, c: ContentItem) -> LayoutAsset | None:
    if not c.media_id:
        return None
    a = db.get(LayoutAsset, c.media_id)
    return a if a is not None and a.kind == "media" and a.project_id == c.project_id else None


# ---------- vídeo gerado pela IA (OpenRouter /videos, assíncrono) ----------

def video_choice(db, log: bool = False, project_id: int | None = None) -> modelcfg.Choice:
    c = modelcfg.resolve(db, "video")
    if c.provider != "openrouter" or not c.model:
        msg = "Escolha o modelo de vídeo do OpenRouter em Modelos (atividade \"Vídeos\") antes de gerar vídeo."
        if log:
            calllog.record_failure(c.provider, c.model, "video", msg, "chamada não enviada", project_id)
        raise ProviderError(msg)
    return c


def video_prompt(project_name: str, brief: str, c: ContentItem, assets: str = "") -> str:
    subject = (c.image_prompt.strip() or c.media.strip() or c.title or c.body)[:1200]
    lines = [f"Short vertical social media video clip for the brand \"{project_name}\". {subject}",
             f"Style: {brief.strip()[:600]}" if brief.strip() else "",
             f"Brand objects that may appear (describe them faithfully): {assets[:800]}" if assets else "",
             "No text, captions, letters, subtitles, logos or watermarks in the video: text is added later on top."]
    return "\n".join(x for x in lines if x)


def _key() -> str:
    from .providers.openrouter import _api_key

    key = _api_key()
    if not key:
        raise ProviderError("OpenRouter sem chave: defina GOS_OPENROUTER_API_KEY no .env do VPS")
    return key


def _error(resp: httpx.Response, model: str) -> ProviderError:
    from .providers.openrouter import _error_detail, _explain

    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    _, msg = _error_detail(payload if isinstance(payload, dict) else {})
    return _explain(resp.status_code, msg, model)


async def submit_video(model: str, prompt: str, ratio: str) -> str:
    """Pede o vídeo; devolve o id do job (o vídeo leva minutos e é buscado pelo agendador)."""
    key = _key()
    with calllog.track("openrouter", model, "video", f"vídeo {ratio} · prompt de {len(prompt)} caracteres") as call:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(VIDEO_URL, json={"model": model, "prompt": prompt, "aspect_ratio": ratio},
                                         headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:
            raise ProviderError(f"não consegui falar com o OpenRouter: {type(e).__name__}") from e
        if resp.status_code not in (200, 201, 202):
            raise _error(resp, model)
        job = str((resp.json() or {}).get("id") or "")
        if not job or not all(ch.isalnum() or ch in "-_" for ch in job):
            raise ProviderError("o OpenRouter não devolveu o id do vídeo")
        call.done(f"job {job}")
        return job


async def poll_video(job: str) -> tuple[str, bytes | None, str]:
    """-> (status, mp4 ou None, erro). status: pending | in_progress | completed | failed | cancelled | expired."""
    key = _key()
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(f"{VIDEO_URL}/{job}", headers=headers)
            if resp.status_code != 200:
                return "failed", None, f"o OpenRouter respondeu {resp.status_code} ao consultar o vídeo"
            info = resp.json() or {}
            status = str(info.get("status") or "")
            if status != "completed":
                return status, None, str(info.get("error") or "")[:400]
            got = await client.get(f"{VIDEO_URL}/{job}/content", params={"index": 0}, headers=headers)
            if got.status_code != 200:
                return "failed", None, f"não consegui baixar o vídeo pronto ({got.status_code})"
            return "completed", got.content, ""
    except httpx.HTTPError as e:
        return "in_progress", None, f"consulta falhou ({type(e).__name__}); tento de novo"


async def start_video(db, c: ContentItem) -> None:
    choice = video_choice(db, log=True, project_id=c.project_id)
    project = db.get(GrowthProject, c.project_id)
    from .assets import for_piece, rewrite_mentions, text_lines

    prompt = video_prompt(project.name if project else "", (project.style_brief if project else "") or "", c,
                          text_lines(for_piece(db, c)))
    prompt = rewrite_mentions(db, c, prompt, [], 1)  # vídeo não recebe imagem: @Texto entra inteiro, @Imagem vira só o nome
    with calllog.context("video", c.project_id):
        c.media_job = await submit_video(choice.model, prompt, VIDEO_RATIO.get(c.visual_format, "9:16"))
    c.image_error = ""
    db.commit()


async def poll_pending_videos() -> int:
    """Passada do agendador: busca os vídeos que terminaram e monta a imagem da peça em cima do layout."""
    from . import layouts as lay

    done = 0
    db = new_session()
    try:
        for c in db.query(ContentItem).filter(ContentItem.media_job != "").limit(10).all():
            status, mp4, err = await poll_video(c.media_job)
            if status in ("pending", "in_progress", ""):
                continue
            c.media_job = ""
            if status == "completed" and mp4:
                try:
                    set_media(db, c, mp4, check_media(mp4), "openrouter-video")
                    await lay.generate_piece_image(db, c)
                except (ValueError, ProviderError) as e:
                    db.rollback()
                    c.image_error = f"vídeo gerado, mas falhou ao montar: {e}"[:500]
            else:
                c.image_error = f"a geração do vídeo terminou como {status}" + (f": {err}" if err else "")
            db.commit()
            done += 1
    finally:
        db.close()
    return done
