"""Motion da peça: o mesmo template HTML aprovado do projeto, com o texto e o fundo da peça, animado e renderizado em MP4.

O vídeo sai do serviço `motion` (HyperFrames + Chromium + ffmpeg, rede interna sem internet). Aqui só se monta a composição
HyperFrames a partir do template, do fundo e do texto da peça. A animação básica é fixa e determinística (entrada do título e do
texto e zoom lento): nenhum código gerado por IA roda no servidor. Animações livres escritas por agentes ficam em PR do projeto.
"""
import asyncio
import base64
import html as htmllib
import re
import threading
import time

import httpx

from . import calllog
from .config import get_settings
from .layouts import CANVAS, _TOKEN
from .providers.base import ProviderError

DURATION = 6.0  # segundos
MAX_VIDEO_BYTES = 25_000_000
MAX_IMAGE_BYTES = 15_000_000
REQUEST_TIMEOUT = 240.0
BG_PATH = "assets/bg"  # + extensão

# O serviço motion renderiza UM trabalho por vez e responde 429 na hora a quem chega com ele ocupado (um vídeo leva minutos).
# Por isso aqui as chamadas entram em fila (uma por vez neste processo) e, se mesmo assim vier 429 (outro chamador, um render
# ainda terminando), esperam e tentam de novo em vez de falhar na cara do owner.
_SLOT = threading.Lock()
BUSY_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)  # pausas entre as tentativas (os testes zeram)


def _post(path: str, payload: dict) -> httpx.Response:
    """POST no serviço motion, uma chamada por vez e repetindo o 429 (ocupado). Síncrono: o render de vídeo roda em thread."""
    url = get_settings().motion_url.rstrip("/") + path
    with _SLOT:
        for delay in (*BUSY_DELAYS, None):
            resp = httpx.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 429 or delay is None:
                return resp
            time.sleep(delay)
    return resp  # inalcançável

_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_BODY = re.compile(r"<body\b[^>]*>(.*)</body>", re.I | re.S)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/mp4": "mp4", "video/quicktime": "mov"}
MEDIA_PATH = "assets/media"  # + extensão: foto ou vídeo da peça, que entra no {{midia}} do template (ou atrás de tudo)
MAX_DURATION = 30.0  # vídeo da peça com vídeo de fonte: no máximo 30 s (teto do arquivo final)


def _parts(template: str) -> tuple[str, str]:
    """Estilos e miolo do corpo do template (que já passou por sanitize_template: sem script nem recurso externo)."""
    styles = "\n".join(_STYLE.findall(template))
    m = _BODY.search(template)
    body = m.group(1) if m else _STYLE.sub("", template)
    return styles, body.strip()


SAMPLE_BG = "linear-gradient(135deg, #1f2937, #4b5563)"  # prévia do template, sem fundo gerado
STILL_AT = 5.5  # segundo em que a imagem estática é capturada (texto já inteiro na tela)


def _media_tag(media: tuple[str, str] | None, d: str, cls: str) -> str:
    """<img> ou <video> da mídia da peça (caminho dentro do job, nunca URL externa). Vídeo sem som: o áudio fica fora."""
    if media is None:
        return ""
    path, mime = media
    if mime.startswith("video/"):
        return (f'<video id="m-video" class="{cls}" src="{path}" muted playsinline data-start="0" data-duration="{d}" '
                f'data-track-index="1"></video>')
    return f'<img class="{cls}" src="{path}" alt="">'


def has_media_slot(template: str) -> bool:
    return "{{midia}}" in (template or "")


def build_composition(template: str, fmt: str, bg_value: str, title: str, text: str, brand: str = "",
                      duration: float = DURATION, zoom: bool = True, media: tuple[str, str] | None = None) -> str:
    """-> index.html. Texto sempre escapado; bg_value é um `url("assets/bg.png")` do projeto ou um gradiente.

    media = (caminho no job, mime): entra no {{midia}} do template; se o template não tem esse espaço, um vídeo vai em tela
    cheia ATRÁS da arte (a arte precisa ter transparência para ele aparecer, ou o fundo é trocado pelo vídeo).
    """
    w, h = CANVAS[fmt]
    d = f"{duration:g}"
    slot = has_media_slot(template)
    values = {
        "bg": bg_value,
        "titulo": f'<span class="m-title">{htmllib.escape(title)}</span>',
        "texto": f'<span class="m-text">{htmllib.escape(text)}</span>',
        "marca": htmllib.escape(brand),
        "midia": _media_tag(media, d, "m-media") if slot else "",
    }
    styles, body = _parts(template)
    body = _TOKEN.sub(lambda m: values[m.group(1)], body)
    styles = _TOKEN.sub(lambda m: values[m.group(1)] if m.group(1) != "midia" else "", styles)
    under = "" if slot or media is None else _media_tag(media, d, "m-media m-under")
    # a imagem estática não faz zoom: no fim do vídeo o quadro está 5% ampliado e cortaria as bordas da arte
    kb = f'tl.fromTo("#m-kb", {{ scale: 1 }}, {{ scale: 1.05, duration: {d}, ease: "none" }}, 0);\n' if zoom else ""
    doc = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width={w}, height={h}">
<script src="lib/gsap.min.js"></script>
<style>
html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; }}
{styles}
.m-title, .m-text {{ display: inline-block; max-width: 100%; overflow-wrap: break-word; text-wrap: balance; text-shadow: 0 2px 10px rgba(0,0,0,.55), 0 0 2px rgba(0,0,0,.6); }}
#m-frame, #m-kb {{ position: absolute; inset: 0; overflow: hidden; }}
.m-media {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
.m-under {{ position: absolute; inset: 0; z-index: 0; }}
#m-art {{ position: absolute; inset: 0; z-index: 1; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{d}" data-width="{w}" data-height="{h}">
<div id="m-frame" class="clip" data-start="0" data-duration="{d}" data-track-index="0">
<div id="m-kb">
{under}<div id="m-art">
{body}
</div>
</div>
</div>
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
{kb}tl.fromTo(".m-title", {{ opacity: 0, y: 70 }}, {{ opacity: 1, y: 0, duration: 0.8, ease: "power3.out" }}, 0.3);
tl.fromTo(".m-text", {{ opacity: 0, y: 50 }}, {{ opacity: 1, y: 0, duration: 0.8, ease: "power3.out" }}, 0.8);
window.__timelines["main"] = tl;
tl.seek(0);
</script>
</body>
</html>
"""
    return doc


def _payload(template: str, fmt: str, bg: tuple[bytes, str] | None, title: str, text: str, brand: str, zoom: bool = True,
             media: tuple[bytes, str] | None = None, duration: float = DURATION) -> dict:
    binary: dict[str, str] = {}
    if bg is None:
        # sem arte: com vídeo atrás, o fundo fica transparente para o vídeo aparecer; sem nada, um gradiente de prévia
        value = "none" if media is not None and media[1].startswith("video/") else SAMPLE_BG
    else:
        bg_file = f"{BG_PATH}.{_EXT.get(bg[1], 'png')}"
        value, binary = f'url("{bg_file}")', {bg_file: base64.b64encode(bg[0]).decode()}
    tag = None
    if media is not None:
        media_file = f"{MEDIA_PATH}.{_EXT.get(media[1], 'bin')}"
        binary[media_file] = base64.b64encode(media[0]).decode()
        tag = (media_file, media[1])
    html = build_composition(template, fmt, value, title, text, brand, duration=duration, zoom=zoom, media=tag)
    return {"files": {"index.html": html}, "binary": binary}


def video_seconds(media: tuple[bytes, str] | None) -> float:
    """Duração do vídeo final: a do vídeo de fonte (entre 3 e 30 s) ou os 6 s padrão."""
    if media is None or not media[1].startswith("video/"):
        return DURATION
    from .media import mp4_duration

    secs = mp4_duration(media[0]) or DURATION
    return round(max(3.0, min(secs, MAX_DURATION)), 2)


def _check(resp: httpx.Response, magic: tuple[int, bytes], limit: int) -> bytes:
    if resp.status_code != 200:
        try:
            detail = str(resp.json().get("error", ""))[:300]
        except ValueError:
            detail = ""
        raise ProviderError(f"o serviço de vídeo falhou ({resp.status_code}){': ' + detail if detail else ''}")
    data = resp.content
    start = magic[0]
    if data[start:start + len(magic[1])] != magic[1] or len(data) > limit:
        raise ProviderError("o serviço de vídeo devolveu um arquivo inválido ou grande demais")
    return data


def _unreachable(e: Exception) -> ProviderError:
    return ProviderError(f"o serviço de vídeo não respondeu ({type(e).__name__}); confira se o container motion está de pé")


def render_still(template: str, fmt: str, bg: tuple[bytes, str] | None, title: str, text: str, brand: str = "",
                 media: tuple[bytes, str] | None = None) -> bytes:
    """PNG do template com o texto e o fundo da peça (mesma composição do vídeo, no quadro final). Síncrono.

    Com vídeo de fonte, o quadro é capturado aos 2 s (o texto já entrou e o vídeo ainda está rodando)."""
    at = 2.0 if media is not None and media[1].startswith("video/") else STILL_AT
    payload = {**_payload(template, fmt, bg, title, text, brand, zoom=False, media=media), "at": at}
    with calllog.track("motion", "hyperframes", "piece_render", f"imagem {fmt} · template + texto de {len(title) + len(text)} caracteres") as call:
        try:
            resp = _post("/still", payload)
        except httpx.HTTPError as e:
            raise _unreachable(e) from e
        png = _check(resp, (0, b"\x89PNG"), MAX_IMAGE_BYTES)
        call.done(calllog.summarize_bytes("PNG", len(png)))
        return png


async def render_video(template: str, fmt: str, bg: tuple[bytes, str] | None, title: str, text: str, brand: str = "",
                       media: tuple[bytes, str] | None = None) -> bytes:
    """MP4 animado da peça (com o vídeo de fonte por baixo/dentro do layout, se houver). ProviderError em linguagem do owner."""
    secs = video_seconds(media)
    payload = {**_payload(template, fmt, bg, title, text, brand, zoom=media is None, media=media, duration=secs),
               "quality": "standard"}
    with calllog.track("motion", "hyperframes", "video_render", f"vídeo {fmt} · {secs:g} s · texto de {len(title) + len(text)} caracteres") as call:
        try:
            resp = await asyncio.to_thread(_post, "/render", payload)
        except httpx.HTTPError as e:
            raise _unreachable(e) from e
        mp4 = _check(resp, (4, b"ftyp"), MAX_VIDEO_BYTES)
        call.done(calllog.summarize_bytes("MP4", len(mp4)))
        return mp4
