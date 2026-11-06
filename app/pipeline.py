"""Linha de produção das peças dos agentes: o agente escreve, o sistema completa tudo e o owner só aprova.

Uma peça de agente só chega "pronta para aprovar" quando TODAS as partes dela estão preenchidas:
  texto      legenda, título/texto da capa, páginas (título + texto de cada uma), CTA final, (e-mail) assunto e destinatário
  capa       imagem da capa no layout
  páginas    imagem de cada página (e a foto própria dela, quando o template tem janela de foto) e do CTA

O que falta é feito em ordem, cada parte isolada das outras: se a foto da página 4 falhar, só ela é refeita (até
MAX_ATTEMPTS vezes, com espera entre as tentativas); o resto fica como está. Parte que depende do owner (subir mídia,
definir o template, escolher o modelo de imagem) fica "esperando" e não gasta tentativa. O resultado é um status claro:
ready (pronta para aprovar), working (o sistema está fazendo), needs_you (travou e diz exatamente o quê).
"""

import json
import re
from datetime import timedelta

from sqlalchemy.orm import Session as DB

from . import calllog
from . import content as cnt
from .db import ContentItem, GrowthProject, utcnow
from .providers import ChatTurn, ProviderError

MAX_ATTEMPTS = 3
RETRY_DELAY_S = (120, 600, 1800)  # espera depois da 1ª, 2ª e 3ª falha da mesma parte
MIN_PAGES = 3  # carrossel de agente com menos páginas que isso é completado


# ---------- estado (tentativas e erros por parte) ----------

def _state(c: ContentItem) -> dict:
    try:
        v = json.loads(c.pipeline or "")
    except (ValueError, TypeError):
        v = {}
    v = v if isinstance(v, dict) else {}
    for k in ("attempts", "errors", "next"):
        if not isinstance(v.get(k), dict):
            v[k] = {}
    return v


def _save(c: ContentItem, st: dict) -> None:
    c.pipeline = json.dumps(st, ensure_ascii=False) if any(st[k] for k in ("attempts", "errors", "next")) else ""


def reset(c: ContentItem, key: str = "") -> None:
    """O owner mandou tentar de novo (ou editou algo): zera as tentativas de uma parte, ou de todas."""
    st = _state(c)
    for k in ("attempts", "errors", "next"):
        if key:
            st[k].pop(key, None)
        else:
            st[k] = {}
    _save(c, st)


def _record_failure(c: ContentItem, key: str, msg: str) -> None:
    st = _state(c)
    n = int(st["attempts"].get(key, 0)) + 1
    st["attempts"][key] = n
    st["errors"][key] = msg[:400]
    delay = RETRY_DELAY_S[min(n, len(RETRY_DELAY_S)) - 1]
    st["next"][key] = (utcnow() + timedelta(seconds=delay)).isoformat()
    _save(c, st)


def _record_ok(c: ContentItem, key: str) -> None:
    st = _state(c)
    for k in ("attempts", "errors", "next"):
        st[k].pop(key, None)
    _save(c, st)


# ---------- o que falta no texto ----------

def _raw_slides(c: ContentItem) -> list[dict]:
    try:
        v = json.loads(c.slides or "")
    except (ValueError, TypeError):
        return []
    return [s for s in v if isinstance(s, dict)] if isinstance(v, list) else []


def _is_template_carousel(c: ContentItem) -> bool:
    return c.visual_format == "carrossel" and (c.image_mode or "template") == "template"


def _needs_copy_check(c: ContentItem) -> bool:
    """Só as peças que um agente escreveu são completadas e cobradas; o que o owner escreve é do jeito que ele quis."""
    return bool(c.agent_slug)


def copy_gaps(c: ContentItem) -> list[str]:
    """Campos do formulário que ainda estão vazios (texto que o agente deveria ter mandado)."""
    gaps: list[str] = []
    if not c.body.strip():
        gaps.append("legenda/texto")
    if cnt.CHANNELS.get(c.channel, ("", "", ""))[1] == "email":
        if not c.title.strip():
            gaps.append("assunto do e-mail")
        if not c.recipient.strip():
            gaps.append("destinatário do e-mail")
    if c.visual_format:
        if not c.img_title.strip():
            gaps.append("título da capa")
        if not c.img_text.strip():
            gaps.append("texto da capa")
    if _is_template_carousel(c):
        raw = _raw_slides(c)
        pages = [s for s in raw if s.get("role") != "cta"]
        if len(pages) < MIN_PAGES:
            gaps.append(f"páginas ({len(pages)} de {MIN_PAGES}+)")
        for i, s in enumerate(pages, 2):
            if not str(s.get("title") or "").strip():
                gaps.append(f"título da página {i}")
            if not str(s.get("text") or "").strip():
                gaps.append(f"texto da página {i}")
        cta = next((s for s in raw if s.get("role") == "cta"), None)
        if not cta or not str(cta.get("title") or "").strip():
            gaps.append("CTA final")
    return gaps


def _words(text: str, n: int) -> str:
    ws = re.sub(r"\s+", " ", text or "").strip().split(" ")
    out = " ".join(ws[:n]).strip(" .,:;-—")
    return out


def _sentence(text: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    s = m.group(1) if m and len(m.group(1)) >= 12 else t
    return s[:limit].rstrip()


def derive_copy(c: ContentItem) -> bool:
    """Completa sem IA o que dá para tirar do que o agente já escreveu. -> True se mudou algo."""
    changed = False
    first, _, rest = c.body.strip().partition("\n")
    if cnt.CHANNELS.get(c.channel, ("", "", ""))[1] == "email" and not c.title.strip() and first.strip():
        c.title, changed = _words(first, 12)[:200], True
    if c.visual_format:
        if not c.img_title.strip():
            src = c.title or first
            if src.strip():
                c.img_title, changed = _words(src, 6)[:200], True
        if not c.img_text.strip():
            src = rest.strip() or c.body
            if src.strip() and src.strip() != c.img_title.strip():
                c.img_text, changed = _sentence(src, 140)[:400], True
    if _is_template_carousel(c):
        raw = _raw_slides(c)
        for s in raw:
            title, text = str(s.get("title") or "").strip(), str(s.get("text") or "").strip()
            if text and not title and s.get("role") != "cta":
                s["title"], changed = _words(text, 5), True
        if changed or raw:
            c.slides = json.dumps(raw, ensure_ascii=False) if raw else c.slides
    return changed


def copy_prompt(project: GrowthProject | None, c: ContentItem) -> str:
    have = {"titulo_imagem": c.img_title, "texto_imagem": c.img_text, "paginas": [
        {"titulo": s.get("title", ""), "texto": s.get("text", ""), "imagem": s.get("media", "")}
        for s in _raw_slides(c) if s.get("role") != "cta"],
        "cta": next(({"titulo": s.get("title", ""), "texto": s.get("text", ""), "imagem": s.get("media", "")}
                     for s in _raw_slides(c) if s.get("role") == "cta"), None)}
    return f"""Você completa uma peça de conteúdo que um agente de marketing deixou incompleta. Responda SOMENTE com um JSON válido, sem comentários.
Marca: {project.name if project else ""}. Canal: {c.channel}. Formato: {c.visual_format or "texto"}.
Estilo da marca: {((project.style_brief if project else "") or "")[:600]}
Título da peça: {c.title}
Legenda/texto da peça:
{c.body[:3000]}
Descrição da mídia: {c.media[:400]}

O que já está preenchido (NÃO mude, só complete o que está vazio):
{json.dumps(have, ensure_ascii=False)}

Complete o que falta ({"; ".join(copy_gaps(c))}). Regras: português do Brasil, mesmo tom da legenda, sem inventar fatos que a legenda não traz;
título da capa até 6 palavras; texto da capa até 140 caracteres; carrossel com {MIN_PAGES} a 6 páginas, cada uma com "titulo" (até 6 palavras),
"texto" (até 220 caracteres, um passo ou ideia só, apoiando a informação da legenda) e "imagem" (uma frase em inglês descrevendo a cena da foto
dessa página, sem texto escrito); "cta" com chamada final curta e "imagem" também. Formato:
{{"titulo_imagem": "", "texto_imagem": "", "paginas": [{{"titulo": "", "texto": "", "imagem": ""}}], "cta": {{"titulo": "", "texto": "", "imagem": ""}}}}"""


def _json_of(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ProviderError("o modelo não devolveu o JSON do conteúdo")
    try:
        v = json.loads(m.group(0))
    except ValueError as e:
        raise ProviderError("o modelo devolveu um JSON inválido") from e
    if not isinstance(v, dict):
        raise ProviderError("o modelo devolveu um JSON inválido")
    return v


def merge_copy(c: ContentItem, data: dict) -> None:
    """Junta o que o modelo mandou ao que já existe: só preenche vazios e acrescenta o que falta, nunca troca o que o agente escreveu."""
    if c.visual_format:
        if not c.img_title.strip():
            c.img_title = str(data.get("titulo_imagem") or "").strip()[:200]
        if not c.img_text.strip():
            c.img_text = str(data.get("texto_imagem") or "").strip()[:400]
    if not _is_template_carousel(c):
        return
    raw = _raw_slides(c)
    pages = [s for s in raw if s.get("role") != "cta"]
    cta = next((s for s in raw if s.get("role") == "cta"), None)
    new_pages = [p for p in (data.get("paginas") or []) if isinstance(p, dict)]
    for i, p in enumerate(new_pages):
        item = {"title": str(p.get("titulo") or "").strip()[:120], "text": str(p.get("texto") or "").strip()[:300],
                "media": str(p.get("imagem") or "").strip()[:600]}
        if i < len(pages):
            for k, v in item.items():
                if v and not str(pages[i].get(k) or "").strip():
                    pages[i][k] = v
        elif len(pages) < 8 and (item["title"] or item["text"]):
            pages.append({"role": "pagina", **{k: v for k, v in item.items() if v}})
    d = data.get("cta") if isinstance(data.get("cta"), dict) else {}
    item = {"title": str(d.get("titulo") or "").strip()[:120], "text": str(d.get("texto") or "").strip()[:300],
            "media": str(d.get("imagem") or "").strip()[:600]}
    if cta is None:
        cta = {"role": "cta"}
    for k, v in item.items():
        if v and not str(cta.get(k) or "").strip():
            cta[k] = v
    from . import layouts as lay

    c.slides = json.dumps(lay.clean_slides(pages + [cta]), ensure_ascii=False)


async def complete_copy(db: DB, c: ContentItem) -> None:
    """Preenche o texto que falta: primeiro o que dá para tirar da legenda (grátis), depois uma chamada ao modelo de texto."""
    derive_copy(c)
    db.commit()
    if not copy_gaps(c):
        return
    from . import growth

    provider = growth.pick_provider(None, "agents_run", db)  # ProviderError se não houver nenhum
    project = db.get(GrowthProject, c.project_id)
    with calllog.context("agents_run", c.project_id, f"agente:{c.agent_slug}"):
        with calllog.track(provider.id, provider.model, "agents_run", f"completar peça #{c.id}: {'; '.join(copy_gaps(c))}"[:400]) as call:
            reply = await provider.complete([ChatTurn("user", copy_prompt(project, c))])
            call.done(reply)
    merge_copy(c, _json_of(reply))
    derive_copy(c)
    db.commit()
    left = copy_gaps(c)
    if left:
        raise ProviderError("o modelo não completou: " + "; ".join(left))


# ---------- partes e status ----------

def _part(key: str, label: str, ok: bool, st: dict, waiting: str = "") -> dict:
    return {"key": key, "label": label, "ok": ok, "waiting": "" if ok else waiting,
            "error": "" if ok else st["errors"].get(key, ""), "attempts": int(st["attempts"].get(key, 0))}


def _slide_key(i: int, role: str) -> str:
    return "cta" if role == "cta" else f"pagina:{i}"


def _slide_label(i: int, role: str) -> str:
    return "CTA final" if role == "cta" else f"Página {i + 2}"


def _cover_wait(db: DB, c: ContentItem) -> str:
    """Motivo pelo qual a capa não anda sozinha (depende do owner ou de configuração); vazio = pode gerar."""
    from . import layouts as lay
    if c.media_job:
        return "o vídeo da IA ainda está sendo gerado"
    if lay._waiting_for_owner(db, c):
        return "falta a mídia que você escolheu (suba o arquivo ou cole o link)" if (c.media_source or "") != "gerar_video" \
            else "o vídeo de IA só roda quando você clicar em Gerar"
    mode = c.image_mode or "template"
    try:
        if mode == "template" and lay.piece_template(db, c) is None:
            return f"defina o template de {lay.role_label(c.visual_format, 'capa')} deste projeto em Layouts"
        from . import media as med

        if mode != "template" or med.norm_source(c.media_source) == "gerar_imagem":
            lay.image_choice(db, project_id=c.project_id)
    except ProviderError as e:
        return str(e)
    return ""


def check(db: DB, c: ContentItem) -> dict:
    """Situação da peça: cada parte (ok, esperando, erro) e o status geral. Não faz nada, só olha."""
    from . import layouts as lay

    st = _state(c)
    parts: list[dict] = []
    if _needs_copy_check(c):
        gaps = copy_gaps(c)
        p = _part("copy", "Texto", not gaps, st)
        if gaps:
            p["detail"] = "falta: " + "; ".join(gaps)
        parts.append(p)
    if c.visual_format:
        wait = "" if c.image_id else _cover_wait(db, c)
        parts.append(_part("capa", "Capa" if c.visual_format == "carrossel" else "Imagem", bool(c.image_id), st, wait))
        if _is_template_carousel(c):
            for i, s in enumerate(lay.piece_slides(c)):
                photo = lay.slide_wants_photo(db, c, s)
                ok = bool(s.get("image_id")) and (not photo or bool(s.get("photo_id")))
                wait = ""
                if not ok and not c.image_id:
                    wait = "espera a capa"
                elif not ok:
                    try:
                        missing = lay.piece_template(db, c, s["role"]) is None
                    except ProviderError as e:
                        wait, missing = str(e), False
                    if missing:
                        wait = f"defina o template de {lay.role_label('carrossel', s['role'])} em Layouts"
                parts.append(_part(_slide_key(i, s["role"]), _slide_label(i, s["role"]), ok, st, wait))
    done = sum(1 for p in parts if p["ok"])
    pending = [p for p in parts if not p["ok"]]
    if not pending:
        state = "ready"
    elif any(p["waiting"] and p["waiting"] != "espera a capa" for p in pending) or \
            any(p["attempts"] >= MAX_ATTEMPTS for p in pending):
        state = "needs_you"
    else:
        state = "working"
    label = {"ready": "Pronta para aprovar", "working": f"Preparando · {done}/{len(parts)}",
             "needs_you": "Precisa de você"}[state]
    if state == "needs_you":
        stuck = [p for p in pending if p["waiting"] != "espera a capa"]
        label += ": " + "; ".join(f"{p['label']} ({p['waiting'] or p['error'] or 'falhou'})" for p in stuck)[:300]
    return {"state": state, "label": label, "done": done, "total": len(parts), "parts": parts}


def blockers(db: DB, c: ContentItem) -> list[str]:
    """O que impede aprovar uma peça de agente (lista vazia = pode aprovar)."""
    if not _needs_copy_check(c):
        return []
    return [f"{p['label']}: {p.get('detail') or p['waiting'] or p['error'] or 'ainda não está pronta'}"
            for p in check(db, c)["parts"] if not p["ok"]]


def _runnable(c: ContentItem, p: dict, st: dict) -> bool:
    if p["ok"] or p["waiting"] or p["attempts"] >= MAX_ATTEMPTS:
        return False
    nxt = st["next"].get(p["key"])
    return not nxt or nxt <= utcnow().isoformat()


def should_run(db: DB, c: ContentItem) -> bool:
    if c.status != "pending" or c.media_job:
        return False
    st = _state(c)
    return any(_runnable(c, p, st) for p in check(db, c)["parts"])


# ---------- execução ----------

async def _attempt(db: DB, c: ContentItem, key: str, fn) -> bool:
    """Roda uma parte. Falha de uma parte não derruba as outras: anota o erro e segue."""
    try:
        await fn()
    except ProviderError as e:
        db.rollback()
        _record_failure(c, key, str(e))
        db.commit()
        return False
    except Exception as e:  # noqa: BLE001 - uma foto que falha não pode parar a fila inteira
        db.rollback()
        _record_failure(c, key, f"{type(e).__name__}: {e}")
        db.commit()
        return False
    _record_ok(c, key)
    db.commit()
    return True


async def run_piece(db: DB, c: ContentItem, force: bool = False) -> dict:
    """Uma passada: faz o que falta, na ordem texto -> capa -> páginas -> CTA. force=True ignora a espera entre tentativas
    e zera as tentativas esgotadas (o owner pediu para tentar de novo)."""
    import asyncio

    from . import layouts as lay

    if force:
        reset(c)
        db.commit()

    def todo(key: str) -> bool:
        p = next((x for x in check(db, c)["parts"] if x["key"] == key), None)
        return p is not None and _runnable(c, p, _state(c))

    if todo("copy"):
        await _attempt(db, c, "copy", lambda: complete_copy(db, c))
    if c.visual_format and todo("capa"):
        await _attempt(db, c, "capa", lambda: lay.generate_cover(db, c))
        db.refresh(c)
    if _is_template_carousel(c) and c.image_id:
        for i, s in enumerate(lay.piece_slides(c)):
            key = _slide_key(i, s["role"])
            if not todo(key):
                continue

            async def build(i=i):
                slides = lay.piece_slides(c)
                s = slides[i]
                if lay.slide_wants_photo(db, c, s) and not s.get("photo_id"):
                    await lay.generate_slide_photo(db, c, s)
                cover = await asyncio.to_thread(lay.cover_layers, db, c, lay.piece_template(db, c))
                await asyncio.to_thread(lay.render_slide, db, c, s, cover[0])
                c.slides = json.dumps(slides, ensure_ascii=False)
                db.commit()

            await _attempt(db, c, key, build)
    status = check(db, c)
    c.image_error = "; ".join(f"{p['label']}: {p['error'] or p['waiting']}" for p in status["parts"]
                              if not p["ok"] and (p["error"] or (p["waiting"] and p["waiting"] != "espera a capa")))[:500]
    cnt.touch(c)
    db.commit()
    return status
