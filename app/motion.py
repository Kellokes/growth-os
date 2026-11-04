"""Motion da peça: o mesmo template HTML aprovado do projeto, com o texto e o fundo da peça, animado e renderizado em MP4.

O vídeo sai do serviço `motion` (HyperFrames + Chromium + ffmpeg, rede interna sem internet). Aqui só se monta a composição
HyperFrames a partir do template, do fundo e do texto da peça. A animação básica é fixa e determinística (entrada do título e do
texto e zoom lento): nenhum código gerado por IA roda no servidor. Animações livres escritas por agentes ficam em PR do projeto.
"""
import base64
import html as htmllib
import re

import httpx

from .config import get_settings
from .layouts import CANVAS, _TOKEN
from .providers.base import ProviderError

DURATION = 6.0  # segundos
MAX_VIDEO_BYTES = 25_000_000
MAX_IMAGE_BYTES = 15_000_000
REQUEST_TIMEOUT = 240.0
BG_PATH = "assets/bg"  # + extensão

_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_BODY = re.compile(r"<body\b[^>]*>(.*)</body>", re.I | re.S)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _parts(template: str) -> tuple[str, str]:
    """Estilos e miolo do corpo do template (que já passou por sanitize_template: sem script nem recurso externo)."""
    styles = "\n".join(_STYLE.findall(template))
    m = _BODY.search(template)
    body = m.group(1) if m else _STYLE.sub("", template)
    return styles, body.strip()


SAMPLE_BG = "linear-gradient(135deg, #1f2937, #4b5563)"  # prévia do template, sem fundo gerado
STILL_AT = 5.5  # segundo em que a imagem estática é capturada (texto já inteiro na tela)


def build_composition(template: str, fmt: str, bg_value: str, title: str, text: str, brand: str = "",
                      duration: float = DURATION) -> str:
    """-> index.html. Texto sempre escapado; bg_value é um `url("assets/bg.png")` do projeto ou um gradiente."""
    w, h = CANVAS[fmt]
    values = {
        "bg": bg_value,
        "titulo": f'<span class="m-title">{htmllib.escape(title)}</span>',
        "texto": f'<span class="m-text">{htmllib.escape(text)}</span>',
        "marca": htmllib.escape(brand),
    }
    styles, body = _parts(template)
    body = _TOKEN.sub(lambda m: values[m.group(1)], body)
    styles = _TOKEN.sub(lambda m: values[m.group(1)], styles)
    d = f"{duration:g}"
    doc = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width={w}, height={h}">
<script src="lib/gsap.min.js"></script>
<style>
html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; }}
{styles}
.m-title, .m-text {{ display: inline-block; max-width: 100%; }}
#m-frame, #m-kb {{ position: absolute; inset: 0; overflow: hidden; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{d}" data-width="{w}" data-height="{h}">
<div id="m-frame" class="clip" data-start="0" data-duration="{d}" data-track-index="0">
<div id="m-kb">
{body}
</div>
</div>
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
tl.fromTo("#m-kb", {{ scale: 1 }}, {{ scale: 1.05, duration: {d}, ease: "none" }}, 0);
tl.fromTo(".m-title", {{ opacity: 0, y: 70 }}, {{ opacity: 1, y: 0, duration: 0.8, ease: "power3.out" }}, 0.3);
tl.fromTo(".m-text", {{ opacity: 0, y: 50 }}, {{ opacity: 1, y: 0, duration: 0.8, ease: "power3.out" }}, 0.8);
window.__timelines["main"] = tl;
tl.seek(0);
</script>
</body>
</html>
"""
    return doc


def _payload(template: str, fmt: str, bg: tuple[bytes, str] | None, title: str, text: str, brand: str) -> dict:
    if bg is None:
        value, binary = SAMPLE_BG, {}
    else:
        bg_file = f"{BG_PATH}.{_EXT.get(bg[1], 'png')}"
        value, binary = f'url("{bg_file}")', {bg_file: base64.b64encode(bg[0]).decode()}
    return {"files": {"index.html": build_composition(template, fmt, value, title, text, brand)}, "binary": binary}


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


def render_still(template: str, fmt: str, bg: tuple[bytes, str] | None, title: str, text: str, brand: str = "") -> bytes:
    """PNG do template com o texto e o fundo da peça (mesma composição do vídeo, no quadro final). Síncrono."""
    payload = {**_payload(template, fmt, bg, title, text, brand), "at": STILL_AT}
    try:
        resp = httpx.post(get_settings().motion_url.rstrip("/") + "/still", json=payload, timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as e:
        raise _unreachable(e) from e
    return _check(resp, (0, b"\x89PNG"), MAX_IMAGE_BYTES)


async def render_video(template: str, fmt: str, bg: tuple[bytes, str] | None, title: str, text: str, brand: str = "") -> bytes:
    """MP4 animado da peça. ProviderError com o motivo em linguagem do owner."""
    payload = {**_payload(template, fmt, bg, title, text, brand), "quality": "standard"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(get_settings().motion_url.rstrip("/") + "/render", json=payload)
    except httpx.HTTPError as e:
        raise _unreachable(e) from e
    return _check(resp, (4, b"ftyp"), MAX_VIDEO_BYTES)
