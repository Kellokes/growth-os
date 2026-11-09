"""Importação em lote: um ZIP com as imagens/vídeos + posts.csv vira várias peças já aprovadas e agendadas.

Fluxo: `build` lê o ZIP e valida tudo (sem gravar nada) -> prévia com os erros por linha; `commit` grava as peças
(só se não houver erro). O envio ao Postiz é o de sempre: peça social aprovada com data futura é agendada sozinha.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import content as cnt
from . import media as med
from .db import ContentItem, GrowthProject, LayoutAsset, utcnow

MAX_ZIP_BYTES = 80_000_000
MAX_UNPACKED_BYTES = 250_000_000
MAX_ENTRIES = 400
MAX_ROWS = 100
MAX_CAROUSEL = 9  # capa + 8 páginas (o limite das páginas do carrossel no OS)
MAX_BODY = 5000
TYPES = ("post", "carrossel", "story", "reel", "texto")
_TYPE_ALIASES = {"carousel": "carrossel", "carrosel": "carrossel", "stories": "story", "reels": "reel", "video": "reel",
                 "foto": "post", "imagem": "post", "feed": "post", "text": "texto", "tweet": "texto", "thread": "texto"}
_COLS = {
    "projeto": "projeto", "project": "projeto",
    "canal": "canal", "rede": "canal", "plataforma": "canal", "canais": "canal",
    "tipo": "tipo", "formato": "tipo", "type": "tipo",
    "quando": "quando", "data": "quando", "agendar": "quando", "horario": "quando", "data_hora": "quando",
    "texto": "texto", "legenda": "texto", "copy": "texto", "caption": "texto",
    "titulo": "titulo", "title": "titulo", "nome": "titulo",
    "arquivos": "arquivos", "imagens": "arquivos", "midia": "arquivos", "imagem": "arquivos", "arquivo": "arquivos",
    "conta": "conta", "perfil": "conta", "canal_postiz": "conta", "account": "conta",
}
COLUMNS = ("projeto", "canal", "tipo", "quando", "texto", "titulo", "arquivos", "conta")


class ImportError_(ValueError):
    """Erro do ZIP inteiro (não de uma linha)."""


@dataclass
class Row:
    line: int
    project: str = ""
    channel: str = ""
    kind: str = ""
    when: datetime | None = None
    title: str = ""
    body: str = ""
    account: str = ""
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate: bool = False
    project_id: int | None = None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _base(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def read_zip(raw: bytes) -> tuple[bytes, dict[str, bytes], list[str]]:
    """-> (manifesto em bytes, {nome do arquivo: bytes}, avisos). Só o nome base conta (pastas dentro do ZIP não importam)."""
    if len(raw) > MAX_ZIP_BYTES:
        raise ImportError_(f"ZIP grande demais (máximo {MAX_ZIP_BYTES // 1_000_000} MB)")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise ImportError_("arquivo não é um ZIP válido") from e
    infos = [i for i in z.infolist() if not i.is_dir() and not _base(i.filename).startswith((".", "~"))
             and "__MACOSX" not in i.filename]
    if len(infos) > MAX_ENTRIES:
        raise ImportError_(f"ZIP com arquivos demais (máximo {MAX_ENTRIES})")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise ImportError_("o conteúdo do ZIP é grande demais")
    files: dict[str, bytes] = {}
    manifest = None
    warnings: list[str] = []
    for i in infos:
        base = _base(i.filename)
        if i.flag_bits & 0x1:
            raise ImportError_("ZIP com senha não é aceito")
        low = base.lower()
        with z.open(i) as fh:
            data = fh.read(i.file_size + 1)
        if low in ("posts.csv", "posts.json") and manifest is None:
            manifest = (low, data)
            continue
        if low in files or low in {k.lower() for k in files}:
            raise ImportError_(f"dois arquivos com o mesmo nome no ZIP: {base}")
        files[base] = data
    if manifest is None:
        raise ImportError_("não achei o posts.csv dentro do ZIP")
    return manifest[1] if manifest[0] == "posts.csv" else _json_to_csv(manifest[1]), files, warnings


def _json_to_csv(data: bytes) -> bytes:
    try:
        rows = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ImportError_("posts.json inválido") from e
    if isinstance(rows, dict):
        rows = rows.get("posts") or []
    if not isinstance(rows, list):
        raise ImportError_("posts.json deve ser uma lista de posts")
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(COLUMNS)
    for r in rows:
        r = r if isinstance(r, dict) else {}
        norm = {_COLS.get(_norm(k), _norm(k)): v for k, v in r.items()}
        vals = []
        for col in COLUMNS:
            v = norm.get(col, "")
            vals.append("|".join(map(str, v)) if isinstance(v, list) else str(v if v is not None else ""))
        w.writerow(vals)
    return out.getvalue().encode("utf-8")


def parse_manifest(data: bytes) -> list[tuple[int, dict[str, str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")  # CSV exportado do Excel antigo
    head = text.split("\n", 1)[0]
    delim = max((";", ",", "\t"), key=head.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_("o posts.csv está vazio") from None
    cols = [_COLS.get(_norm(h), "") for h in header]
    if "texto" not in cols or "canal" not in cols:
        raise ImportError_("o posts.csv precisa ao menos das colunas canal e texto (cabeçalho na primeira linha)")
    out = []
    for n, vals in enumerate(reader, start=2):
        if not any(v.strip() for v in vals):
            continue
        out.append((n, {c: v.strip() for c, v in zip(cols, vals) if c}))
        if len(out) > MAX_ROWS:
            raise ImportError_(f"posts demais de uma vez (máximo {MAX_ROWS} linhas por ZIP)")
    if not out:
        raise ImportError_("o posts.csv não tem nenhum post")
    return out


def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;|,+]", s or "") if x.strip()]


def _find_project(db: Session, key: str) -> GrowthProject | None:
    k = _norm(key)
    for p in db.scalars(select(GrowthProject)):
        if k and k in (_norm(p.slug), _norm(p.name)):
            return p
    return None


def _validate_files(row: Row, kind: str, files: dict[str, bytes], lower: dict[str, str]) -> dict[str, str]:
    """Confere os arquivos citados na linha. -> {nome real: mime}."""
    mimes: dict[str, str] = {}
    real = []
    for name in row.files:
        hit = lower.get(_base(name).lower())
        if hit is None:
            row.errors.append(f"arquivo não está no ZIP: {name}")
            continue
        real.append(hit)
        try:
            mimes[hit] = med.check_media(files[hit])
        except ValueError as e:
            row.errors.append(f"{hit}: {e}")
    row.files = real
    if row.errors:
        return mimes
    videos = [n for n in real if med.is_video(mimes[n])]
    imgs = [n for n in real if n not in videos]
    if kind == "texto" and real:
        row.errors.append("tipo texto não leva arquivos (use post, carrossel, story ou reel)")
    elif kind == "post" and not (len(real) == 1 and imgs):
        row.errors.append("post precisa de exatamente 1 imagem")
    elif kind == "carrossel" and not (2 <= len(imgs) <= MAX_CAROUSEL and not videos):
        row.errors.append(f"carrossel precisa de 2 a {MAX_CAROUSEL} imagens (na ordem dos slides), sem vídeo")
    elif kind == "story" and len(real) != 1:
        row.errors.append("story precisa de exatamente 1 arquivo (imagem ou vídeo)")
    elif kind == "reel" and not (len(videos) == 1 and len(real) <= 2):
        row.errors.append("reel precisa de 1 vídeo (e, se quiser, 1 imagem de capa)")
    return mimes


def build(db: Session, raw_zip: bytes, default_project: str = "") -> tuple[list[Row], dict[str, bytes], dict[str, str]]:
    """Lê e valida o ZIP sem gravar nada. -> (linhas já separadas por canal, arquivos, mimes)."""
    manifest, files, _ = read_zip(raw_zip)
    lower = {k.lower(): k for k in files}
    now = cnt.local_now()
    rows: list[Row] = []
    mimes: dict[str, str] = {}
    used: set[str] = set()
    for line, r in parse_manifest(manifest):
        base = Row(line=line)
        proj = _find_project(db, r.get("projeto") or default_project)
        if proj is None:
            base.errors.append(f"projeto não encontrado: {r.get('projeto') or default_project or '(vazio)'}")
        else:
            base.project, base.project_id = proj.slug, proj.id
        raw_kind = _norm(r.get("tipo", "")) or ""
        kind = _TYPE_ALIASES.get(raw_kind, raw_kind)
        if kind not in TYPES:
            base.errors.append(f"tipo inválido: {r.get('tipo') or '(vazio)'} (use {', '.join(TYPES)})")
        base.kind = kind
        base.title = r.get("titulo", "")[:200]
        base.body = r.get("texto", "").strip()
        if not base.body:
            base.errors.append("falta o texto (legenda)")
        elif len(base.body) > MAX_BODY:
            base.errors.append(f"texto com mais de {MAX_BODY} caracteres")
        base.account = r.get("conta", "")[:120]
        if r.get("quando", "").strip():
            base.when = cnt.parse_when(r["quando"])
            if base.when is None:
                base.errors.append(f"data inválida: {r['quando']} (use AAAA-MM-DD HH:MM)")
            elif base.when <= now:
                base.errors.append("a data já passou (a peça seria publicada na hora)")
        else:
            base.errors.append("falta a data e hora (quando), horário de São Paulo")
        base.files = _split(r.get("arquivos", ""))
        channels = [cnt.norm_channel(c) for c in _split(r.get("canal", ""))]
        if not channels:
            base.errors.append("falta o canal")
        for ch in dict.fromkeys(channels):
            if cnt.CHANNELS.get(ch, ("", ""))[1] != "social":
                base.errors.append(f"canal sem envio automático pelo Postiz: {ch} (use redes sociais)")
        if kind in TYPES and not base.errors:
            m = _validate_files(base, kind, files, lower)
            mimes.update(m)
        elif kind in TYPES:
            for name in base.files:  # mesmo com outro erro, aponta arquivo faltando
                if lower.get(_base(name).lower()) is None:
                    base.errors.append(f"arquivo não está no ZIP: {name}")
        used.update(base.files)
        for ch in dict.fromkeys(channels) or [""]:
            row = Row(**{**base.__dict__, "channel": ch, "errors": list(base.errors), "warnings": [],
                         "files": list(base.files)})
            if ch and base.project_id and base.when and not row.errors:
                dup = db.scalar(select(ContentItem.id).where(
                    ContentItem.project_id == base.project_id, ContentItem.channel == ch,
                    ContentItem.scheduled_at == base.when, ContentItem.body == base.body,
                    ContentItem.status.in_(("pending", "approved", "sent", "failed"))))
                if dup:
                    row.duplicate = True
                    row.warnings.append(f"já existe a peça #{dup} igual: será ignorada")
            rows.append(row)
    unused = sorted(set(files) - used)
    if unused:
        rows[0].warnings.append("arquivos do ZIP que nenhuma linha usa: " + ", ".join(unused[:10]))
    return rows, files, mimes


def preview_json(rows: list[Row]) -> dict:
    return {
        "rows": [{"line": r.line, "project": r.project, "channel": r.channel, "kind": r.kind,
                  "when": r.when.isoformat(timespec="minutes") if r.when else "", "title": r.title,
                  "body": r.body[:140], "account": r.account, "files": r.files, "errors": r.errors,
                  "warnings": r.warnings, "duplicate": r.duplicate} for r in rows],
        "ok": not any(r.errors for r in rows),
        "to_create": sum(1 for r in rows if not r.errors and not r.duplicate),
        "duplicates": sum(1 for r in rows if r.duplicate),
        "errors": sum(1 for r in rows if r.errors),
    }


def _asset(db: Session, c: ContentItem, kind: str, name: str, data: bytes, mime: str) -> LayoutAsset:
    a = LayoutAsset(project_id=c.project_id, kind=kind, format=c.visual_format, mime=mime, data=data, model="import",
                    prompt=name[:200])
    db.add(a)
    db.flush()
    return a


def commit(db: Session, rows: list[Row], files: dict[str, bytes], mimes: dict[str, str]) -> list[ContentItem]:
    """Grava as peças (aprovadas, agendadas). Nada é gravado se alguma linha tem erro."""
    if any(r.errors for r in rows):
        raise ImportError_("corrija os erros do ZIP antes de importar")
    made: list[ContentItem] = []
    for r in rows:
        if r.duplicate:
            continue
        c = ContentItem(project_id=r.project_id, status="approved", decided_at=utcnow(), channel=r.channel,
                        title=r.title, body=r.body, scheduled_at=r.when, account=r.account,
                        visual_format=r.kind if r.kind in ("carrossel", "story", "reel") else "",
                        image_mode="reference", media_source="upload")
        db.add(c)
        db.flush()
        imgs = [n for n in r.files if not med.is_video(mimes[n])]
        vids = [n for n in r.files if med.is_video(mimes[n])]
        if vids:
            c.video_id = _asset(db, c, "video", vids[0], files[vids[0]], mimes[vids[0]]).id
        if imgs:
            c.image_id = _asset(db, c, "piece", imgs[0], files[imgs[0]], mimes[imgs[0]]).id
        if r.kind == "carrossel":
            c.img_title = r.title[:200]
            c.slides = json.dumps([
                {"role": "pagina", "title": f"Slide {i}", "text": "",
                 "image_id": _asset(db, c, "piece", n, files[n], mimes[n]).id}
                for i, n in enumerate(imgs[1:], start=2)], ensure_ascii=False)
        cnt.touch(c)
        made.append(c)
    db.commit()
    return made


def prompt_text(slugs: list[str], project: str = "") -> str:
    """Prompt para colar numa IA (ChatGPT, Claude...) pedindo o ZIP no formato que o OS importa."""
    proj = project or (slugs[0] if slugs else "meu-projeto")
    return f"""Monte um ZIP com posts de redes sociais prontos para eu importar no meu sistema, que agenda tudo sozinho.

O ZIP deve ter, na raiz (sem subpastas), estes arquivos:
1. posts.csv (UTF-8, separador ponto e vírgula, primeira linha = cabeçalho, textos com aspas se tiverem ; ou quebra de linha)
2. todas as imagens/vídeos que o posts.csv cita

Colunas do posts.csv, nesta ordem:
projeto;canal;tipo;quando;texto;titulo;arquivos;conta

Regras:
- projeto: um destes valores exatos: {", ".join(slugs) if slugs else proj}. Se todos os posts forem do mesmo projeto, use {proj} em todas as linhas.
- canal: instagram, facebook, linkedin, x, threads, tiktok ou youtube. Para o mesmo post em mais de uma rede, separe com | (ex.: instagram|facebook).
- tipo: post (1 imagem), carrossel (2 a 9 imagens, a primeira é a capa), story (1 imagem ou vídeo), reel (1 vídeo mp4, opcionalmente + 1 imagem de capa) ou texto (sem arquivos, só para X, LinkedIn, Threads).
- quando: data e hora no horário de São Paulo, formato AAAA-MM-DD HH:MM (ex.: 2026-11-12 18:30). Sempre no futuro.
- texto: a legenda completa, já com hashtags e emojis, no tom do projeto. Respeite o limite da rede (X: 280 caracteres).
- titulo: nome interno curto da peça (só para eu me achar; não vai para a rede).
- arquivos: nomes dos arquivos do ZIP separados por | , na ordem dos slides (ex.: 02-slide1.jpg|02-slide2.jpg|02-slide3.jpg). Vazio para tipo texto.
- conta: opcional. Nome do perfil/canal no Postiz quando há mais de uma conta da mesma rede (ex.: Take One Studio). Vazio = o sistema escolhe.

Arquivos: nomes sem espaços nem acentos, únicos no ZIP, só jpg, png, webp ou mp4/mov. Imagens com no máximo 8 MB, vídeos com no máximo 25 MB. Formatos recomendados: post/carrossel 1080x1350, story/reel 1080x1920.

Exemplo de posts.csv:
projeto;canal;tipo;quando;texto;titulo;arquivos;conta
{proj};instagram;carrossel;2026-11-12 18:30;"3 erros que travam seu crescimento 👇 #marketing";Carrossel erros;01-capa.jpg|01-s2.jpg|01-s3.jpg;
{proj};linkedin;texto;2026-11-13 09:00;"Hoje quero falar de ...";Post LinkedIn;;

Antes de montar, me pergunte: o projeto, quais redes, quantos posts, de que dia a que dia e em que horários publicar, e o tema/tom de cada um. Depois gere o ZIP e confira se todo arquivo citado existe e se todas as datas estão no futuro."""
