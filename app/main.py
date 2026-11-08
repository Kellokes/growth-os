import asyncio
import base64
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DB

from .agent import cmd_json, execute_run, run_agent, tool_message
from .audit import audit
from .config import get_settings
from .db import (
    Agent, AgentNote, AiCall, AuditLog, Automation, CommandRun, ContentItem, Conversation, GrowthProject, Integration, Message, Metric,
    Mission, Action, LayoutAsset, Plan, ProjectAgent, Skill, Task, TaskLink, User, get_db, init_db, new_session, utcnow,
)
from .executor import classify
from . import actions as acts
from . import calllog, costs
from . import approvals as appr
from . import content as cnt
from . import growth
from . import layouts as lay
from . import media as med
from . import assets as ast
from . import missions
from . import endpoints, modelcfg
from . import pipeline
from . import routines
from . import projects as proj
from . import reset as rst
from .providers import all_providers
from .providers.base import ProviderError
from .security import (
    LOGIN_CHALLENGE_SECONDS, LOGIN_COOKIE, SESSION_COOKIE, Auth, check_password, client_ip,
    complete_login_challenge, create_login_challenge, create_session, current_auth,
    consume_totp, destroy_session, lookup_login_challenge, lookup_session,
)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")



def _protect_process_memory() -> None:
    """Impede que comandos executados pela IA (mesmo usuário) leiam /proc/<pid>/environ e a memória do app.

    PR_SET_DUMPABLE=0 faz o kernel tornar esses arquivos acessíveis só ao root. Os processos filhos
    reiniciam o flag ao fazer exec, então não herdam essa proteção nem ficam limitados por ela.
    """
    try:
        import ctypes

        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # 4 = PR_SET_DUMPABLE
    except Exception:  # noqa: BLE001 - plataforma sem prctl (macOS/Windows em desenvolvimento)
        pass


def _write_github_token() -> None:
    """Deixa o token num arquivo do tmpfs para o credential helper do git e o gos-pr.

    Ele nunca entra no ambiente dos comandos, e o executor bloqueia leitura direta do arquivo.
    O escopo do token (só os repos dos projetos, sem admin) é o que limita o estrago.
    """
    s = get_settings()
    path = Path(s.github_token_file)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if s.github_token:
            path.write_text(s.github_token.strip(), encoding="utf-8")
            path.chmod(0o600)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _protect_process_memory()
    _write_github_token()
    init_db()
    calllog.sweep(startup=True)
    growth.runner.bind(asyncio.get_running_loop())
    db = new_session()
    try:
        growth.seed_defaults(db)
        growth.fail_orphan_commands(db)
        growth.requeue_approved(db)
    finally:
        db.close()
    scheduler = asyncio.create_task(growth.scheduler_loop()) if get_settings().scheduler_enabled else None
    sender = asyncio.create_task(_auto_send_loop()) if get_settings().scheduler_enabled else None
    yield
    for t in (scheduler, sender):
        if t:
            t.cancel()


app = FastAPI(title="Growth OS", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------- páginas ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: DB = Depends(get_db)):
    if lookup_session(db, request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    error = "O tempo para o código acabou. Entre de novo." if request.query_params.get("expirou") else None
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: DB = Depends(get_db),
):
    """Etapa 1: usuário e senha. Certo abre o desafio 2FA; nada de sessão ainda."""
    ip = client_ip(request)
    user, reason = check_password(db, username, password)
    if user is None:
        audit(db, username[:64], "login_failed", reason, ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Usuário ou senha inválidos.", "username": username[:64]},
            status_code=401,
        )
    token = create_login_challenge(db, user)
    audit(db, user.username, "login_password_ok", "", ip)
    resp = RedirectResponse("/login/2fa", status_code=303)
    resp.set_cookie(
        LOGIN_COOKIE, token, httponly=True, secure=get_settings().cookie_secure, samesite="strict",
        max_age=LOGIN_CHALLENGE_SECONDS, path="/login",
    )
    return resp


def _two_factor_page(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, "login_2fa.html", {"error": error}, status_code=status_code)


def _back_to_login(expired: bool = True):
    resp = RedirectResponse("/login?expirou=1" if expired else "/login", status_code=303)
    resp.delete_cookie(LOGIN_COOKIE, path="/login")
    return resp


@app.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, db: DB = Depends(get_db)):
    if lookup_login_challenge(db, request.cookies.get(LOGIN_COOKIE)) is None:
        return _back_to_login(expired=LOGIN_COOKIE in request.cookies)
    return _two_factor_page(request)


@app.post("/login/2fa")
def login_2fa(request: Request, totp: str = Form(...), db: DB = Depends(get_db)):
    """Etapa 2: código do app autenticador. Só aqui nasce a sessão."""
    ip = client_ip(request)
    token = request.cookies.get(LOGIN_COOKIE)
    ch = lookup_login_challenge(db, token)
    if ch is None:
        return _back_to_login()
    owner = db.get(User, ch.user_id)
    username = owner.username if owner else str(ch.user_id)
    user, reason = complete_login_challenge(db, token, totp)
    if user is None:
        audit(db, username, "login_failed", reason, ip)
        if lookup_login_challenge(db, token) is None:
            # Tentativas esgotadas ou conta bloqueada: volta para a etapa 1.
            return _back_to_login()
        return _two_factor_page(request, "Código inválido. Confira o app autenticador.", 401)
    session_token, _ = create_session(db, user)
    audit(db, user.username, "login_ok", "", ip)
    resp = RedirectResponse("/", status_code=303)
    s = get_settings()
    resp.delete_cookie(LOGIN_COOKIE, path="/login")
    resp.set_cookie(
        SESSION_COOKIE, session_token, httponly=True, secure=s.cookie_secure, samesite="strict",
        max_age=s.session_hours * 3600, path="/",
    )
    return resp


@app.post("/logout")
def logout(request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    destroy_session(db, request.cookies.get(SESSION_COOKIE))
    audit(db, auth.user.username, "logout", "", client_ip(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _page(request: Request, db: DB, template: str, active: str):
    sess = lookup_session(db, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, template, {"csrf": sess.csrf_token, "active": active})


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "chat.html", "chat")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "dashboard.html", "dashboard")


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "approvals.html", "approvals")


def _moved(request: Request, db: DB, to: str):
    """Rotas antigas viram redirecionamento (links salvos e avisos do Telegram continuam funcionando)."""
    if lookup_session(db, request.cookies.get(SESSION_COOKIE)) is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(to, status_code=303)


@app.get("/plan")
def plan_moved(request: Request, db: DB = Depends(get_db)):
    return _moved(request, db, "/approvals")


@app.get("/agents")
def agents_moved(request: Request, db: DB = Depends(get_db)):
    return _moved(request, db, "/automations#agentes")


@app.get("/board", response_class=HTMLResponse)
def board_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "board.html", "board")


@app.get("/automations", response_class=HTMLResponse)
def automations_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "automations.html", "automations")


@app.get("/content", response_class=HTMLResponse)
def content_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "content.html", "board")


@app.get("/layouts", response_class=HTMLResponse)
def layouts_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "layouts.html", "layouts")


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "projects.html", "projects")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "settings.html", "models")


@app.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "costs.html", "costs")


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "config.html", "config")


# ---------- API ----------

@app.get("/api/providers")
def providers(auth: Auth = Depends(current_auth)):
    out = []
    for p in all_providers().values():
        ok, why = p.available()
        out.append({"id": p.id, "label": p.label, "available": ok, "reason": why})
    return out


def _choice_json(c) -> dict:
    return {"provider": c.provider or "", "model": c.model or "", "reasoning": c.reasoning or ""}


@app.get("/api/models")
def model_settings(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Tudo que a tela de modelos e os seletores precisam: atividades, catálogo, regras salvas e o efeito final."""
    rows = modelcfg.saved(db)
    provs = []
    for p in all_providers().values():
        ok, why = p.available()
        cat = modelcfg.CATALOG.get(p.id, {"kinds": ["text"], "reasoning": [], "models": []})
        provs.append({"id": p.id, "label": p.label, "available": ok, "reason": why, **cat})
    return {
        "providers": provs,
        "activities": modelcfg.ACTIVITIES,
        "settings": {k: _choice_json(r) for k, r in rows.items()},
        "effective": {a["key"]: {**_choice_json(c := modelcfg.resolve(db, a["key"])), "source": c.source}
                      for a in modelcfg.ACTIVITIES},
        "openrouter_default": get_settings().openrouter_model,
    }


@app.get("/api/models/openrouter")
async def openrouter_models(kind: str = "text", auth: Auth = Depends(current_auth)):
    """Modelos do OpenRouter que servem para a atividade, com preços (dólar) direto da API deles."""
    data = await modelcfg.openrouter_models()
    models = [{**m, "route": (r.route if (r := endpoints.lookup(m["id"])) else ""), "known_route": endpoints.is_known(m["id"])}
              for m in modelcfg.for_kind(data["models"], kind)]
    return {**data, "models": models}


class ModelChoiceIn(BaseModel):
    provider: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="", max_length=16)


@app.put("/api/models/{activity}")
def save_model_setting(activity: str, body: ModelChoiceIn, request: Request,
                       auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    if activity not in modelcfg.ACTIVITY_KEYS:
        raise HTTPException(404, "atividade desconhecida")
    provider, model, reasoning = body.provider.strip(), body.model.strip(), body.reasoning.strip()
    if not provider:
        model = reasoning = ""  # sem provider = segue o padrão
    kind = modelcfg.kind_of(activity)
    err = modelcfg.check(provider, model, reasoning, kind)
    if err:
        raise HTTPException(400, err)
    modelcfg.save(db, activity, provider, model, reasoning)
    audit(db, auth.user.username, "model_setting", f"{activity}: {provider or 'padrão'} {model} {reasoning}".strip(),
          client_ip(request))
    return {"ok": True, "warning": modelcfg.warning(provider, model, kind)}


def _conv_json(c: Conversation) -> dict:
    return {"id": c.id, "title": c.title, "category": c.category or "", "deleted": c.deleted_at is not None,
            "provider": c.provider or "", "model": c.model or "", "reasoning": c.reasoning or ""}


@app.get("/api/conversations")
def list_conversations(
    category: str | None = None, trash: bool = False, q: str = "",
    auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    query = db.query(Conversation)
    query = query.filter(Conversation.deleted_at.is_not(None) if trash else Conversation.deleted_at.is_(None))
    if category is not None:
        query = query.filter(Conversation.category == category.strip())
    if q.strip():
        query = query.filter(Conversation.title.ilike(f"%{q.strip()[:80]}%"))
    rows = query.order_by(Conversation.id.desc()).limit(200).all()
    return [_conv_json(c) for c in rows]


@app.get("/api/conversations/categories")
def conversation_categories(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    rows = (
        db.query(Conversation.category, func.count(Conversation.id))
        .filter(Conversation.deleted_at.is_(None))
        .group_by(Conversation.category).all()
    )
    trash = db.query(func.count(Conversation.id)).filter(Conversation.deleted_at.is_not(None)).scalar() or 0
    cats = sorted(({"name": n or "", "count": k} for n, k in rows), key=lambda c: (c["name"] == "", c["name"].lower()))
    return {"categories": cats, "trash": trash}


@app.post("/api/conversations")
def create_conversation(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = Conversation()
    db.add(c)
    db.commit()
    return _conv_json(c)


class ConvPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=40)


def _conv_or_404(db, cid: int) -> Conversation:
    c = db.get(Conversation, cid)
    if c is None:
        raise HTTPException(404, "not_found")
    return c


@app.patch("/api/conversations/{cid}")
def update_conversation(cid: int, body: ConvPatch, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _conv_or_404(db, cid)
    if body.title is not None and body.title.strip():
        c.title = body.title.strip()[:200]
    if body.category is not None:
        c.category = " ".join(body.category.split())[:40]
    db.commit()
    return _conv_json(c)


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Só esconde do sistema (vai para a lixeira). Mensagens, comandos e auditoria ficam no banco."""
    c = _conv_or_404(db, cid)
    if c.deleted_at is None:
        c.deleted_at = utcnow()
        db.commit()
        audit(db, auth.user.username, "conversation_hidden", f"#{c.id} {c.title[:120]}", client_ip(request))
    return _conv_json(c)


@app.post("/api/conversations/{cid}/restore")
def restore_conversation(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _conv_or_404(db, cid)
    if c.deleted_at is not None:
        c.deleted_at = None
        db.commit()
        audit(db, auth.user.username, "conversation_restored", f"#{c.id} {c.title[:120]}", client_ip(request))
    return _conv_json(c)


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = db.get(Conversation, cid)
    if c is None:
        raise HTTPException(404, "not_found")
    msgs = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id).all()
    cmds = db.query(CommandRun).filter(CommandRun.conversation_id == cid).order_by(CommandRun.id).all()
    return {
        **_conv_json(c),
        "messages": [
            {"id": m.id, "role": m.role, "provider": m.provider, "model": m.model or "", "content": m.content}
            for m in msgs if m.role != "tool"
        ],
        "commands": [cmd_json(r) for r in cmds],
    }


class ContinueIn(BaseModel):
    provider: str
    model: str = Field(default="", max_length=120)  # vazio = padrão da conta/CLI
    reasoning: str = Field(default="", max_length=16)  # vazio = padrão do modelo


class ChatIn(ContinueIn):
    content: str = Field(min_length=1, max_length=20000)


def _provider_or_400(pid: str, model: str = "", reasoning: str = ""):
    provider = all_providers().get(pid)
    if provider is None:
        raise HTTPException(400, "provider_desconhecido")
    ok, why = provider.available()
    if not ok:
        raise HTTPException(400, why)
    model, reasoning = model.strip(), reasoning.strip()
    if pid in modelcfg.CATALOG:
        err = modelcfg.check(pid, model, reasoning)
        if err:
            raise HTTPException(400, err)
    elif model and not modelcfg.MODEL_RE.match(model):
        raise HTTPException(400, "id de modelo inválido")
    return provider.configured(model, reasoning if pid in modelcfg.CATALOG else "")


def _missions_from_reply(db: DB, cid: int) -> list[Mission]:
    """Thread do chat (não de tarefa de agente) em que a IA propôs uma missão com um bloco ```missao```."""
    if db.query(Task).filter(Task.conversation_id == cid).count():
        return []
    last = db.query(Message).filter(Message.conversation_id == cid, Message.role == "assistant").order_by(Message.id.desc()).first()
    return missions.create_from_chat(db, cid, last.content) if last is not None else []


def _stream(cid: int, provider, actor: str, ip: str) -> StreamingResponse:
    async def gen():
        db = new_session()  # sessão própria: a da dependência fecha antes do fim do stream
        try:
            async for event in run_agent(db, cid, provider, actor, ip):
                yield json.dumps(event, ensure_ascii=False) + "\n"
            growth.sync_task_for_conversation(db, cid)
            for m in _missions_from_reply(db, cid):
                yield json.dumps({"type": "notice", "message": f"🎯 Missão proposta: {m.title}. Aprove em Aprovações "
                                  "para os agentes começarem."}, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 - o stream já começou; reporta como evento
            yield json.dumps({"type": "error", "message": f"erro interno: {type(e).__name__}"}) + "\n"
        finally:
            db.close()

    return StreamingResponse(
        gen(), media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@app.post("/api/conversations/{cid}/messages")
async def send_message(
    cid: int, body: ChatIn, request: Request,
    auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    c = db.get(Conversation, cid)
    if c is None:
        raise HTTPException(404, "not_found")
    if c.deleted_at is not None:
        raise HTTPException(409, "conversa na lixeira: restaure antes de continuar")
    provider = _provider_or_400(body.provider, body.model, body.reasoning)
    c.provider, c.model, c.reasoning = provider.id, provider.model, provider.reasoning  # a thread lembra a escolha

    db.add(Message(conversation_id=cid, role="user", content=body.content))
    title_changed = c.title in ("Nova thread", "Nova conversa")
    if title_changed:
        c.title = body.content.strip().splitlines()[0][:80]
    db.commit()
    ip = client_ip(request)
    audit(db, auth.user.username, "chat", f"{provider.id}: {body.content[:200]}", ip)
    return _stream(cid, provider, auth.user.username, ip)


@app.post("/api/conversations/{cid}/continue")
async def continue_conversation(
    cid: int, body: ContinueIn, request: Request,
    auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    """Retoma o laço de onde parou, sem nova mensagem do owner.

    Serve para: depois do 2FA (aprovou/rejeitou), depois do limite de passos e para tentar de novo quando a
    IA falhou (timeout, 502...). Só faz sentido se a última mensagem não é uma resposta final da IA.
    """
    c = db.get(Conversation, cid)
    if c is None:
        raise HTTPException(404, "not_found")
    if c.deleted_at is not None:
        raise HTTPException(409, "conversa na lixeira: restaure antes de continuar")
    provider = _provider_or_400(body.provider, body.model, body.reasoning)
    pending = db.query(CommandRun).filter(CommandRun.conversation_id == cid, CommandRun.status == "pending").first()
    if pending is not None:
        raise HTTPException(409, "há um comando esperando o seu 2FA: aprove ou rejeite antes de continuar")
    last = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id.desc()).first()
    if last is None or last.role not in ("tool", "user"):
        raise HTTPException(409, "nada para continuar")
    return _stream(cid, provider, auth.user.username, client_ip(request))


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=8000)
    conversation_id: int | None = None


class ApproveIn(BaseModel):
    totp: str = Field(min_length=6, max_length=8)


@app.post("/api/commands")
async def submit_command(
    body: CommandIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    """Execução manual de um comando (botão ▶): segue a mesma política do agente."""
    cls = classify(body.command)
    status = {"safe": "running", "auto": "running", "approval": "pending", "blocked": "blocked"}[cls.risk]
    run = CommandRun(
        conversation_id=body.conversation_id, command=body.command, risk=cls.risk, status=status,
        output=cls.reason if cls.risk == "blocked" else "",
    )
    db.add(run)
    db.commit()
    audit(db, auth.user.username, f"command_{cls.risk}", body.command, client_ip(request))
    if cls.risk in ("safe", "auto"):
        await execute_run(db, run)
    return {**cmd_json(run), "reason": cls.reason}


def _record_result(db: DB, run: CommandRun) -> None:
    """Guarda o resultado na conversa para a IA ver quando o laço for retomado."""
    if run.conversation_id:
        db.add(Message(conversation_id=run.conversation_id, role="tool", content=tool_message(run, after_pause=True)))
        db.commit()


@app.post("/api/commands/{rid}/approve")
async def approve_command(
    rid: int, body: ApproveIn, request: Request,
    auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    run = db.get(CommandRun, rid)
    if run is None:
        raise HTTPException(404, "not_found")
    if run.status != "pending":
        raise HTTPException(409, "comando não está pendente")
    if not consume_totp(db, auth.user, body.totp):
        audit(db, auth.user.username, "approve_failed", run.command, client_ip(request))
        raise HTTPException(403, "código 2FA inválido")
    # Revalida: as regras podem ter mudado desde que o comando foi enviado.
    if classify(run.command).risk == "blocked":
        run.status = "blocked"
        db.commit()
        raise HTTPException(403, "comando bloqueado")
    run.status = "running"
    db.commit()
    audit(db, auth.user.username, "command_approved", run.command, client_ip(request))
    await execute_run(db, run)
    _record_result(db, run)
    return cmd_json(run)


@app.post("/api/commands/{rid}/reject")
def reject_command(rid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    run = db.get(CommandRun, rid)
    if run is None or run.status != "pending":
        raise HTTPException(409, "comando não está pendente")
    run.status = "rejected"
    db.commit()
    audit(db, auth.user.username, "command_rejected", run.command, client_ip(request))
    _record_result(db, run)
    return cmd_json(run)


@app.get("/api/audit")
def audit_log(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(200).all()
    return [
        {"at": r.at.isoformat(), "actor": r.actor, "action": r.action, "detail": r.detail, "ip": r.ip}
        for r in rows
    ]


@app.get("/api/calls")
def ai_calls(limit: int = 100, status: str = "", activity: str = "", after_id: int = 0, auth: Auth = Depends(current_auth),
             db: DB = Depends(get_db)):
    """Log das chamadas em segundo plano (IA, imagem, renderização), da mais nova para a mais antiga."""
    calllog.sweep()
    base = db.query(AiCall).filter(AiCall.id > max(after_id, 0))  # after_id: o "limpar a tela" do rodapé (nada é apagado)
    q = base
    if status in ("running", "ok", "error"):
        q = q.filter(AiCall.status == status)
    if activity:
        q = q.filter(AiCall.activity == activity)
    rows = q.order_by(AiCall.id.desc()).limit(max(1, min(limit, 300))).all()
    names = {p.id: p.name for p in db.query(GrowthProject).filter(GrowthProject.id.in_({r.project_id for r in rows if r.project_id})).all()}
    running = base.filter(AiCall.status == "running").count()
    failed = base.filter(AiCall.status == "error", AiCall.at >= utcnow() - timedelta(hours=24)).count()
    failed_total = base.filter(AiCall.status == "error").count()
    latest_id = db.query(func.max(AiCall.id)).scalar() or 0
    labels = {a["key"]: a["label"] for a in modelcfg.ACTIVITIES} | calllog.ACTIVITY_LABELS
    return {"running": running, "failed_24h": failed, "failed_total": failed_total, "latest_id": latest_id,
            "log_prompts": calllog.full_enabled(), "calls": [
        calllog.call_json(r) | {"project": names.get(r.project_id, ""), "activity_label": labels.get(r.activity, r.activity)}
        for r in rows]}


@app.get("/api/calls/{call_id}")
def ai_call_detail(call_id: int, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Uma chamada com o prompt enviado e o retorno completos (só existem nas gravadas com GOS_LOG_PROMPTS=true)."""
    r = db.get(AiCall, call_id)
    if r is None:
        raise HTTPException(404, "chamada não encontrada")
    return calllog.full_json(r)


@app.get("/api/costs")
async def api_costs(days: int = 30, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Custo das chamadas ao OpenRouter por atividade, modelo e dia (dólar), a partir do log de chamadas."""
    models = (await modelcfg.openrouter_models())["models"]
    return costs.summarize(db, models, max(1, min(days, 90)))


@app.delete("/api/calls")
def clear_ai_calls(request: Request, status: str = "error", auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Limpa do log as chamadas com erro (as em andamento e as que deram certo ficam)."""
    if status != "error":
        raise HTTPException(422, "só dá para limpar as chamadas com erro")
    n = db.query(AiCall).filter(AiCall.status == "error").delete()
    db.commit()
    audit(db, auth.user.username, "calls_clear", f"{n} chamada(s) com erro apagadas do log", client_ip(request))
    return {"deleted": n}


# ---------- configurações: resetar dados ----------

class ResetIn(BaseModel):
    confirm: str = Field(max_length=32)
    totp: str = Field(min_length=6, max_length=8)


@app.get("/api/reset/preview")
def reset_preview(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Só conta o que seria apagado e o que fica. Abrir a página nunca apaga nada."""
    return rst.preview(db)


@app.post("/api/reset")
def reset_data(body: ResetIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    ip = client_ip(request)
    if (body.confirm or "").strip() != rst.CONFIRM_WORD:
        raise HTTPException(400, f"digite {rst.CONFIRM_WORD} para confirmar")
    if not consume_totp(db, auth.user, body.totp):
        audit(db, auth.user.username, "data_reset_failed", "código 2FA inválido", ip)
        raise HTTPException(403, "código 2FA inválido")
    try:
        deleted = rst.reset_data(db, body.confirm, auth.user.username, ip)
    except rst.ResetError as e:
        raise HTTPException(e.status, e.detail) from None
    growth.runner.clear()
    return {"ok": True, "deleted": deleted}


# ---------- projetos (etapa 2) ----------

@app.get("/api/projects")
async def projects_overview(auth: Auth = Depends(current_auth)):
    sites, caddy_error = proj.read_caddy_sites()
    try:
        containers, gateway_error = await proj.list_containers(), ""
    except proj.GatewayError as e:
        containers, gateway_error = [], str(e)
    return {
        "projects": proj.group_projects(containers, sites),
        "errors": [e for e in (gateway_error, caddy_error) if e],
    }


@app.get("/api/projects/domains")
async def projects_domains(auth: Auth = Depends(current_auth)):
    sites, _ = proj.read_caddy_sites()
    hosts = sorted({h for s in sites for h in s["hosts"]})
    return await proj.check_hosts(hosts)


@app.get("/api/containers/{name}/logs")
async def container_logs(name: str, request: Request, tail: int = 200, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    try:
        text = await proj.container_logs(name, tail)
    except proj.GatewayError as e:
        raise HTTPException(502, str(e)) from e
    audit(db, auth.user.username, "container_logs", name, client_ip(request))
    return {"name": name, "logs": text}


@app.post("/api/containers/{name}/restart")
async def container_restart(
    name: str, body: ApproveIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    ip = client_ip(request)
    if not consume_totp(db, auth.user, body.totp):
        audit(db, auth.user.username, "restart_failed", f"{name}: 2FA inválido", ip)
        raise HTTPException(403, "código 2FA inválido")
    try:
        await proj.restart_container(name)
    except proj.GatewayError as e:
        audit(db, auth.user.username, "restart_failed", f"{name}: {e}", ip)
        raise HTTPException(502, str(e)) from e
    audit(db, auth.user.username, "container_restart", name, ip)
    return {"ok": True}


# ---------- etapa 3: projetos, agentes, plano do dia e KPIs (tudo por projeto) ----------

def _project_or_404(db: DB, slug: str | None) -> GrowthProject:
    p = growth.get_project(db, slug)
    if p is None:
        raise HTTPException(404, "projeto não encontrado")
    return p


def _project_json(p: GrowthProject) -> dict:
    return {"slug": p.slug, "name": p.name, "icon": p.icon, "description": p.description, "repo": p.repo,
            "domains": p.domains, "compose_project": p.compose_project, "enabled": p.enabled}


def _task_json(t: Task, projects: dict[int, GrowthProject] | None = None, links: dict[int, TaskLink] | None = None) -> dict:
    proj_ = (projects or {}).get(t.project_id)
    link = (links or {}).get(t.id)
    return {
        "requested_by": link.requested_by if link else "", "parent_id": link.parent_id if link else None,
        "id": t.id, "plan_id": t.plan_id, "project": proj_.slug if proj_ else "", "agent": t.agent_slug,
        "title": t.title, "rationale": t.rationale, "steps": t.steps, "risk": t.risk, "kpi": t.kpi_key,
        "status": t.status, "result": t.result, "conversation_id": t.conversation_id,
        "created_at": t.created_at.isoformat(),
        "decided_at": t.decided_at.isoformat() if t.decided_at else None,
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        "automation_id": t.automation_id, "type": "task", "wait_reason": t.wait_reason or "",
        "mission_id": t.mission_id, "depends_on": t.depends_on,
        # de onde veio: plano do dia, automação, pedido de outro agente ou pedido direto do owner
        "origin": "automation" if t.automation_id else "handoff" if link else "plan" if t.plan_id else "owner",
    }


def _links(db: DB, tasks: list[Task]) -> dict[int, TaskLink]:
    ids = [t.id for t in tasks]
    return {lk.task_id: lk for lk in db.query(TaskLink).filter(TaskLink.task_id.in_(ids)).all()} if ids else {}


def _projects_by_id(db: DB) -> dict[int, GrowthProject]:
    return {p.id: p for p in db.query(GrowthProject).all()}


async def _import_from_vps(db: DB) -> list[str]:
    containers = await proj.list_containers()
    sites, _ = proj.read_caddy_sites()
    return growth.import_projects(db, containers, sites)


@app.get("/api/gprojects")
async def list_gprojects(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    if db.query(GrowthProject).count() == 0:
        try:
            await _import_from_vps(db)  # primeira vez: traz os projetos que já rodam no VPS
        except proj.GatewayError:
            pass
    rows = db.query(GrowthProject).order_by(GrowthProject.id).all()
    pending: dict[int, int] = {}
    for card in appr.pending(db):  # tudo que espera decisão: tarefas, missões, conteúdo e ações externas
        pending[card["project_id"]] = pending.get(card["project_id"], 0) + 1
    return [{**_project_json(p), "pending": pending.get(p.id, 0)} for p in rows]


@app.post("/api/gprojects/import")
async def import_gprojects(request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    try:
        created = await _import_from_vps(db)
    except proj.GatewayError as e:
        raise HTTPException(502, str(e)) from e
    audit(db, auth.user.username, "projects_import", ", ".join(created), client_ip(request))
    return {"created": created}


class GProjectIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    icon: str | None = Field(default=None, max_length=8)
    description: str | None = Field(default=None, max_length=4000)
    repo: str | None = Field(default=None, pattern=r"^$|^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    domains: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None


@app.post("/api/gprojects")
def create_gproject(body: GProjectIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    if not body.name:
        raise HTTPException(422, "nome obrigatório")
    slug = growth.slugify(body.name)
    if db.query(GrowthProject).filter(GrowthProject.slug == slug).count():
        raise HTTPException(409, "já existe um projeto com esse nome")
    fields = body.model_dump(exclude_none=True, exclude={"name"})
    p = growth.create_project(db, body.name, **fields)
    audit(db, auth.user.username, "project_create", p.slug, client_ip(request))
    return _project_json(p)


@app.patch("/api/gprojects/{slug}")
def update_gproject(slug: str, body: GProjectIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v.strip() if isinstance(v, str) else v)
    db.commit()
    audit(db, auth.user.username, "project_update", f"{slug}: {body.model_dump(exclude_none=True)}"[:500], client_ip(request))
    return _project_json(p)


@app.get("/api/agents")
def agent_catalog(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Catálogo das áreas (nome, ícone, missão padrão). O time de cada projeto fica em /api/gprojects/{p}/agents."""
    return [{"slug": a.slug, "name": a.name, "area": a.area, "icon": a.icon, "mission": a.mission}
            for a in db.query(Agent).order_by(Agent.id).all()]


@app.get("/api/gprojects/{slug}/agents")
def project_agents(slug: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    stats: dict[str, dict] = {}
    for a_slug, status in db.query(Task.agent_slug, Task.status).filter(Task.project_id == p.id).all():
        s = stats.setdefault(a_slug, {})
        s[status] = s.get(status, 0) + 1
    return [
        {"slug": a.slug, "name": a.name, "area": a.area, "icon": a.icon, "mission": growth.mission(a, pa),
         "default_mission": a.mission, "custom": bool(pa.mission.strip()), "enabled": pa.enabled,
         "provider": pa.provider or "", "model": pa.model or "", "reasoning": pa.reasoning or "",
         "stats": stats.get(a.slug, {})}
        for a, pa in growth.team(db, p)
    ]


class AgentIn(BaseModel):
    enabled: bool | None = None
    mission: str | None = Field(default=None, max_length=4000)  # "" volta para a missão padrão da área
    provider: str | None = Field(default=None, max_length=32)  # "" = segue a configuração de modelos
    model: str | None = Field(default=None, max_length=120)
    reasoning: str | None = Field(default=None, max_length=16)


@app.patch("/api/gprojects/{slug}/agents/{agent}")
def update_project_agent(slug: str, agent: str, body: AgentIn, request: Request,
                         auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    pa = db.query(ProjectAgent).filter(ProjectAgent.project_id == p.id, ProjectAgent.agent_slug == agent).one_or_none()
    if pa is None:
        raise HTTPException(404, "agente não existe neste projeto")
    if body.provider and body.provider not in all_providers():
        raise HTTPException(400, "provider_desconhecido")
    if body.enabled is not None:
        pa.enabled = body.enabled
    if body.mission is not None:
        default = db.query(Agent).filter(Agent.slug == agent).one().mission
        pa.mission = "" if body.mission.strip() == default.strip() else body.mission.strip()
    if body.provider is not None:
        pa.provider = body.provider or None
    if body.model is not None:
        pa.model = body.model.strip()
    if body.reasoning is not None:
        pa.reasoning = body.reasoning.strip()
    if not pa.provider:
        pa.model, pa.reasoning = "", ""
    elif pa.provider in modelcfg.CATALOG:
        err = modelcfg.check(pa.provider, pa.model, pa.reasoning)
        if err:
            db.rollback()
            raise HTTPException(400, err)
    db.commit()
    audit(db, auth.user.username, "agent_update", f"{slug}/{agent}: {body.model_dump(exclude_none=True)}"[:500],
          client_ip(request))
    return {"ok": True}


@app.get("/api/plans/today")
def plan_today(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    today = growth.local_today()
    plan = db.query(Plan).filter(Plan.day == today).one_or_none()
    projects = _projects_by_id(db)
    tasks = db.query(Task).filter(Task.plan_id == plan.id).order_by(Task.project_id, Task.id).all() if plan else []
    # Fora do plano de hoje mas pedindo atenção: pedidos entre agentes esperando aprovação, fila, rodando, 2FA.
    open_tasks = (
        db.query(Task).filter(
            or_(Task.plan_id.is_(None), Task.plan_id != (plan.id if plan else -1)),
            or_(Task.status.in_(("waiting", "running", "approved")),
                (Task.status == "proposed") & Task.plan_id.is_(None)),
        ).order_by(Task.id.desc()).limit(40).all()
    )
    links = _links(db, tasks + open_tasks)
    s = get_settings()
    return {
        "day": today.isoformat(),
        "plan": {"id": plan.id, "status": plan.status, "error": plan.error} if plan else None,
        "tasks": [_task_json(t, projects, links) for t in tasks],
        "carryover": [_task_json(t, projects, links) for t in open_tasks],
        "schedule": {"hour": s.plan_hour, "timezone": s.timezone, "enabled": s.scheduler_enabled,
                     "telegram": bool(s.telegram_bot_token and s.telegram_chat_id)},
    }


@app.post("/api/plans/generate")
async def plan_generate(request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    plan = db.query(Plan).filter(Plan.day == growth.local_today()).one_or_none()
    if plan is not None and plan.status == "ready":
        # Refazer o plano descarta só as tarefas ainda não decididas.
        db.query(Task).filter(Task.plan_id == plan.id, Task.status == "proposed").delete()
        plan.status = "failed"
        db.commit()
    audit(db, auth.user.username, "plan_generate_manual", "", client_ip(request))
    asyncio.create_task(growth.generate_plan(actor=auth.user.username))
    return {"ok": True}


class DecideIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=300)
    approve: bool


@app.post("/api/tasks/decide")
def decide_tasks(body: DecideIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Aprovar é o portão: só tarefa aprovada pelo owner roda. Comandos perigosos ainda pedem 2FA."""
    projects = _projects_by_id(db)
    changed = []
    for t in db.query(Task).filter(Task.id.in_(body.ids), Task.status == "proposed").all():
        appr.decide_task(db, t, body.approve)
        changed.append(t)
    for t in changed:
        audit(db, auth.user.username, "task_approved" if body.approve else "task_rejected",
              f"#{t.id} {projects[t.project_id].slug}/{t.agent_slug}: {t.title}", client_ip(request))
        if body.approve:
            growth.runner.submit(t.id)
    return {"changed": [_task_json(t, projects, _links(db, changed)) for t in changed]}


# ---------- aprovações: a fila única do owner ----------

class ApprovalItem(BaseModel):
    kind: str = Field(pattern=r"^(task|mission|content|action)$")
    ref_id: int


class ApprovalDecideIn(BaseModel):
    items: list[ApprovalItem] = Field(min_length=1, max_length=300)
    approve: bool
    note: str = Field(default="", max_length=1000)
    totp: str = Field(default="", max_length=12)


@app.get("/api/approvals")
def list_approvals(project: str = "all", kind: str = "all", auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Tudo que espera uma decisão: tarefas do plano e pedidos, missões, conteúdo e ações externas."""
    if kind != "all" and kind not in appr.KINDS:
        raise HTTPException(422, "tipo inválido")
    pid = _scope(db, project)
    projects = _projects_by_id(db)
    cards = appr.pending(db, pid, None if kind == "all" else kind)
    return {"items": [appr.card_json(c, projects) for c in cards], "counts": appr.counts(db, pid)}


@app.get("/api/approvals/history")
def approvals_history(project: str = "all", limit: int = 50, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    projects = _projects_by_id(db)
    return [{"id": a.id, "kind": a.kind, "ref_id": a.ref_id, "title": a.title, "requested_by": a.requested_by,
             "risk": a.risk, "status": a.status, "via": a.decided_via, "note": a.note,
             "project": projects[a.project_id].slug if a.project_id in projects else "",
             "at": a.decided_at.isoformat()} for a in appr.history(db, _scope(db, project), limit)]


@app.post("/api/approvals/decide")
async def decide_approvals(body: ApprovalDecideIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Aprova ou rejeita cartões de qualquer tipo. Ação externa de risco alto exige o código 2FA (consumido uma vez)."""
    ip = client_ip(request)
    seen: set[tuple[str, int]] = set()
    done, errors, to_run, to_send = [], [], [], []
    totp_ok: bool | None = None
    for it in body.items:
        key = (it.kind, it.ref_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            if it.kind == "task":
                t = db.get(Task, it.ref_id)
                if t is None:
                    raise appr.ApprovalError("não encontrado", 404)
                appr.decide_task(db, t, body.approve)
                if body.approve:
                    growth.runner.submit(t.id)
                detail = f"#{t.id} {t.agent_slug}: {t.title}"
            elif it.kind == "content":
                c = _content_or_404(db, it.ref_id)
                appr.decide_content(db, c, body.approve, body.note)
                if body.approve:
                    to_send.append(c)
                detail = f"#{c.id} {c.channel}"
            elif it.kind == "mission":
                m = db.get(Mission, it.ref_id)
                if m is None:
                    raise appr.ApprovalError("não encontrado", 404)
                appr.decide_mission(db, m, body.approve, body.note)
                if body.approve:
                    for tid in missions.activate(db, m):
                        growth.runner.submit(tid)
                else:
                    missions.cancel(db, m)
                detail = f"#{m.id} {m.title}"
            else:
                a = db.get(Action, it.ref_id)
                if a is None:
                    raise appr.ApprovalError("não encontrado", 404)
                if body.approve and a.risk == "alto" and a.status == "awaiting_approval" and totp_ok is None:
                    totp_ok = consume_totp(db, auth.user, body.totp)  # um código vale para o lote inteiro
                appr.decide_action(db, a, body.approve, bool(totp_ok), body.note)
                if body.approve:
                    to_run.append(a)
                else:
                    growth.action_settled(db, a)  # rejeitada: a tarefa que esperava segue sem ela
                detail = f"#{a.id} {a.integration}.{a.operation}"
        except appr.ApprovalError as e:
            errors.append({"kind": it.kind, "ref_id": it.ref_id, "error": str(e), "code": e.code})
            if e.code == 403:
                audit(db, auth.user.username, "approve_failed", f"{it.kind}#{it.ref_id}", ip)
            continue
        done.append({"kind": it.kind, "ref_id": it.ref_id})
        audit(db, auth.user.username, f"approval_{it.kind}_" + ("approved" if body.approve else "rejected"), detail[:500], ip)
    if not done and errors:
        raise HTTPException(errors[0]["code"], errors[0]["error"])
    for a in to_run:  # aprovada pelo owner: agora sim o conector age fora do OS
        await acts.execute_approved(db, a)
        audit(db, auth.user.username, "action_executed", f"#{a.id} {a.integration}.{a.operation} -> {a.status}", ip)
        growth.action_settled(db, a)  # feita ou falhou: a tarefa que esperava continua com o desfecho
    for c in to_send:  # peça de rede social aprovada: vai sozinha ao Postiz (agenda ou publica)
        await _auto_send(db, c, auth.user.username, ip)
    return {"decided": done, "errors": errors,
            "actions": [{"id": a.id, "status": a.status, "error": a.error} for a in to_run]}


# ---------- interno: gos-int (só de dentro do container, com o ticket da tarefa) ----------

INTERNAL_HOSTS = {"127.0.0.1", "::1"}
_FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "cf-connecting-ip")


class InternalIn(BaseModel):
    ticket: str = Field(max_length=200)
    integracao: str = Field(default="", max_length=32)
    operacao: str = Field(default="", max_length=48)
    parametros: dict = Field(default_factory=dict)


def _internal_ticket(request: Request, ticket: str) -> acts.Ticket:
    """Só quem roda dentro do container (loopback, sem passar pelo Caddy) e tem o ticket de uma tarefa viva."""
    host = request.client.host if request.client else ""
    if host not in INTERNAL_HOSTS or any(h in request.headers for h in _FORWARD_HEADERS):
        raise HTTPException(404, "not_found")
    t = acts.lookup(ticket)
    if t is None:
        raise HTTPException(403, "ticket inválido ou tarefa já terminou")
    return t


@app.post("/internal/integrations")
def internal_integrations(body: InternalIn, request: Request, db: DB = Depends(get_db)):
    t = _internal_ticket(request, body.ticket)
    project = db.get(GrowthProject, t.project_id)
    return {"integracoes": acts.catalog(db, project, t.agent_slug) if project else []}


@app.post("/internal/actions")
async def internal_actions(body: InternalIn, request: Request, db: DB = Depends(get_db)):
    t = _internal_ticket(request, body.ticket)
    return await acts.run_operation(db, t, body.integracao.strip().lower(), body.operacao.strip(), body.parametros)


class TaskIn(BaseModel):
    project: str
    agent: str
    title: str = Field(min_length=3, max_length=200)
    steps: str = Field(default="", max_length=4000)


@app.post("/api/tasks")
def create_task(body: TaskIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Tarefa avulsa criada pelo owner para um agente de um projeto (já nasce aprovada e entra na fila)."""
    p = _project_or_404(db, body.project)
    if db.query(ProjectAgent).filter(ProjectAgent.project_id == p.id, ProjectAgent.agent_slug == body.agent).count() == 0:
        raise HTTPException(404, "agente não existe neste projeto")
    t = Task(project_id=p.id, agent_slug=body.agent, title=body.title.strip(), steps=body.steps.strip(),
             status="approved", rationale="Pedido direto do owner.", decided_at=utcnow())
    db.add(t)
    db.commit()
    audit(db, auth.user.username, "task_created", f"#{t.id} {p.slug}/{t.agent_slug}: {t.title}", client_ip(request))
    growth.runner.submit(t.id)
    return _task_json(t, {p.id: p})


@app.get("/api/tasks")
def list_tasks(project: str | None = None, limit: int = 50, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    q = db.query(Task)
    if project:
        q = q.filter(Task.project_id == _project_or_404(db, project).id)
    rows = q.order_by(Task.id.desc()).limit(max(1, min(limit, 200))).all()
    projects = _projects_by_id(db)
    links = _links(db, rows)
    return [_task_json(t, projects, links) for t in rows]


@app.get("/api/gprojects/{slug}/notes")
def project_notes(slug: str, limit: int = 40, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Conversa do time: pedidos, recados e respostas entre os agentes do projeto."""
    p = _project_or_404(db, slug)
    rows = (
        db.query(AgentNote).filter(AgentNote.project_id == p.id).order_by(AgentNote.id.desc())
        .limit(max(1, min(limit, 200))).all()
    )
    return [{"id": n.id, "from": n.from_agent, "to": n.to_agent, "kind": n.kind, "text": n.text,
             "task_id": n.task_id, "at": n.created_at.isoformat()} for n in rows]


@app.get("/api/gprojects/{slug}/metrics")
def metrics(slug: str, days: int = 90, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    return growth.metric_series(db, _project_or_404(db, slug).id, max(7, min(days, 730)))


class PointIn(BaseModel):
    value: float
    day: date | None = None
    note: str = Field(default="", max_length=500)


@app.post("/api/gprojects/{slug}/metrics/{key}/points")
def add_point(slug: str, key: str, body: PointIn, request: Request,
              auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    try:
        pt = growth.record_point(db, p.id, key, body.value, body.day, source="manual", note=body.note)
    except KeyError as e:
        raise HTTPException(404, "KPI não existe neste projeto") from e
    audit(db, auth.user.username, "kpi_point", f"{slug}/{key}={body.value} ({pt.day})", client_ip(request))
    return {"key": key, "day": pt.day.isoformat(), "value": pt.value}


class MetricIn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    name: str = Field(min_length=2, max_length=80)
    area: str = Field(default="growth", max_length=32)
    unit: str = Field(default="number", pattern=r"^(number|currency|percent)$")
    direction: str = Field(default="up", pattern=r"^(up|down)$")
    target: float | None = None


@app.post("/api/gprojects/{slug}/metrics")
def upsert_metric(slug: str, body: MetricIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    m = db.query(Metric).filter(Metric.project_id == p.id, Metric.key == body.key).one_or_none()
    if m is None:
        m = Metric(project_id=p.id, key=body.key)
        db.add(m)
    m.name, m.area, m.unit, m.direction, m.target = body.name, body.area, body.unit, body.direction, body.target
    db.commit()
    audit(db, auth.user.username, "kpi_upsert", f"{slug}/{body.key}", client_ip(request))
    return {"ok": True}


# ---------- kanban das atividades dos agentes ----------

@app.get("/api/board")
def board(project: str = "all", days: int = 14, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Tudo que está aberto + o que fechou nos últimos `days` dias. As colunas são as etapas da tarefa."""
    days = max(1, min(days, 90))
    q = db.query(Task)
    if project != "all":
        q = q.filter(Task.project_id == _project_or_404(db, project).id)
    since = utcnow() - timedelta(days=days)
    open_rows = q.filter(Task.status.in_(routines.OPEN_STATUSES)).order_by(Task.id.desc()).limit(300).all()
    closed = (
        q.filter(Task.status.in_(("done", "failed", "rejected")),
                 func.coalesce(Task.finished_at, Task.decided_at, Task.created_at) >= since)
        .order_by(Task.id.desc()).limit(300).all()
    )
    rows = open_rows + closed
    projects = _projects_by_id(db)
    links = _links(db, rows)
    autos = {a.id: a.title for a in db.query(Automation).all()}
    out = []
    for t in rows:
        j = _task_json(t, projects, links)
        j["automation"] = autos.get(t.automation_id, "") if t.automation_id else ""
        out.append(j)
    out += _board_extras(db, project, since, projects)
    return {"tasks": out, "days": days}


_CONTENT_STAGE = {"pending": "proposed", "approved": "approved", "sent": "done", "rejected": "rejected", "failed": "failed"}
_ACTION_STAGE = {"awaiting_approval": "proposed", "requested": "approved", "approved": "approved", "running": "running",
                 "done": "done", "failed": "failed", "rejected": "rejected"}


def _board_extras(db: DB, project: str, since: datetime, projects: dict[int, GrowthProject]) -> list[dict]:
    """Conteúdo e ações em integrações entram no mesmo quadro, com o tipo no cartão (filtro "Conteúdo" etc.)."""
    pid = None if project == "all" else _project_or_404(db, project).id
    out = []
    cq = db.query(ContentItem)
    aq = db.query(Action)
    if pid is not None:
        cq, aq = cq.filter(ContentItem.project_id == pid), aq.filter(Action.project_id == pid)
    stamp = func.coalesce(ContentItem.sent_at, ContentItem.decided_at, ContentItem.created_at)
    for c in cq.filter(or_(ContentItem.status.in_(cnt.EDITABLE), stamp >= since)).order_by(ContentItem.id.desc()).limit(300).all():
        p = projects.get(c.project_id)
        out.append({
            "type": "content", "id": c.id, "project": p.slug if p else "", "agent": c.agent_slug,
            "title": c.title or (c.body or "")[:90], "channel": c.channel, "status": _CONTENT_STAGE[c.status],
            "risk": "baixo", "origin": "content", "requested_by": "", "automation": "", "rationale": c.purpose,
            "steps": c.body, "result": c.note, "conversation_id": None, "wait_reason": "",
            "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
            "created_at": c.created_at.isoformat(), "decided_at": c.decided_at.isoformat() if c.decided_at else None,
            "finished_at": c.sent_at.isoformat() if c.sent_at else None,
        })
    astamp = func.coalesce(Action.finished_at, Action.created_at)
    open_actions = ("awaiting_approval", "requested", "approved", "running")
    for a in aq.filter(or_(Action.status.in_(open_actions), astamp >= since)).order_by(Action.id.desc()).limit(300).all():
        p = projects.get(a.project_id)
        out.append({
            "type": "action", "id": a.id, "project": p.slug if p else "", "agent": "",
            "title": a.title or f"{a.integration}: {a.operation}", "channel": a.integration, "status": _ACTION_STAGE[a.status],
            "risk": a.risk, "origin": "action", "requested_by": "", "automation": "", "rationale": f"{a.integration} · {a.operation}",
            "steps": a.params, "result": a.error or a.result, "conversation_id": None, "wait_reason": "",
            "created_at": a.created_at.isoformat(), "decided_at": None,
            "finished_at": a.finished_at.isoformat() if a.finished_at else None,
        })
    return out


@app.post("/api/tasks/{tid}/retry")
def retry_task(tid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Tarefa que falhou volta para a fila (é o owner pedindo de novo, então já vale como aprovação)."""
    t = db.get(Task, tid)
    if t is None:
        raise HTTPException(404, "tarefa não encontrada")
    if t.status != "failed":
        raise HTTPException(409, "só dá para repetir tarefa que falhou")
    t.status, t.result, t.finished_at, t.decided_at = "approved", "", None, utcnow()
    db.commit()
    audit(db, auth.user.username, "task_retry", f"#{t.id} {t.agent_slug}: {t.title}", client_ip(request))
    growth.runner.submit(t.id)
    return _task_json(t, _projects_by_id(db), _links(db, [t]))


# ---------- habilidades, integrações e automações (por projeto) ----------

def _agent_slugs(db: DB, project: GrowthProject) -> set[str]:
    return {pa.agent_slug for pa in db.query(ProjectAgent).filter(ProjectAgent.project_id == project.id).all()}


@app.get("/api/routines/catalog")
def routines_catalog(auth: Auth = Depends(current_auth)):
    return {
        "skills": [{"key": k, "name": n, "description": d, "agents": routines.split_agents(a), "instructions": i}
                   for k, n, d, a, i in routines.SKILL_TEMPLATES],
        "automations": [{"title": t, "agent": a, "skill_template": sk, "frequency": f, "weekday": w, "monthday": m, "hour": h}
                        for t, a, sk, f, w, m, h in routines.AUTOMATION_TEMPLATES],
        "weekdays": routines.WEEKDAYS,
        "events": [{"key": k, "label": v} for k, v in routines.EVENTS.items()],
    }


class SkillIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    instructions: str | None = Field(default=None, max_length=6000)
    agents: list[str] | None = Field(default=None, max_length=20)
    enabled: bool | None = None


def _skill_or_404(db: DB, project: GrowthProject, sid: int) -> Skill:
    s = db.get(Skill, sid)
    if s is None or s.project_id != project.id:
        raise HTTPException(404, "habilidade não encontrada neste projeto")
    return s


def _apply_skill(db: DB, project: GrowthProject, s: Skill, body: SkillIn) -> None:
    if body.agents is not None:
        unknown = set(body.agents) - _agent_slugs(db, project)
        if unknown:
            raise HTTPException(400, f"agente não existe neste projeto: {', '.join(sorted(unknown))}")
        s.agents = ",".join(dict.fromkeys(body.agents))
    for k in ("name", "description", "instructions", "enabled"):
        v = getattr(body, k)
        if v is not None:
            setattr(s, k, v.strip() if isinstance(v, str) else v)


def _skill_from_template(db: DB, project: GrowthProject, key: str) -> Skill:
    existing = db.query(Skill).filter(Skill.project_id == project.id, Skill.template == key).first()
    if existing is not None:
        return existing
    tpl = next((t for t in routines.SKILL_TEMPLATES if t[0] == key), None)
    if tpl is None:
        raise HTTPException(404, "modelo de habilidade não existe")
    _, name, desc, agents, instructions = tpl
    s = Skill(project_id=project.id, name=name, description=desc, instructions=instructions, agents=agents, template=key)
    db.add(s)
    db.commit()
    return s


@app.get("/api/gprojects/{slug}/skills")
def list_skills(slug: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    return [routines.skill_json(s) for s in db.query(Skill).filter(Skill.project_id == p.id).order_by(Skill.id).all()]


@app.post("/api/gprojects/{slug}/skills")
def create_skill(slug: str, body: SkillIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    if not body.name or not (body.instructions or "").strip():
        raise HTTPException(422, "nome e instruções são obrigatórios")
    s = Skill(project_id=p.id, name="")
    _apply_skill(db, p, s, body)
    db.add(s)
    db.commit()
    audit(db, auth.user.username, "skill_create", f"{slug}: {s.name}", client_ip(request))
    return routines.skill_json(s)


@app.post("/api/gprojects/{slug}/skills/template/{key}")
def add_skill_template(slug: str, key: str, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    s = _skill_from_template(db, p, key)
    audit(db, auth.user.username, "skill_create", f"{slug}: {s.name} (biblioteca)", client_ip(request))
    return routines.skill_json(s)


@app.patch("/api/gprojects/{slug}/skills/{sid}")
def update_skill(slug: str, sid: int, body: SkillIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    s = _skill_or_404(db, p, sid)
    _apply_skill(db, p, s, body)
    db.commit()
    audit(db, auth.user.username, "skill_update", f"{slug}: {s.name}", client_ip(request))
    return routines.skill_json(s)


@app.delete("/api/gprojects/{slug}/skills/{sid}")
def delete_skill(slug: str, sid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    s = _skill_or_404(db, p, sid)
    db.query(Automation).filter(Automation.skill_id == s.id).update({Automation.skill_id: None})
    db.delete(s)
    db.commit()
    audit(db, auth.user.username, "skill_delete", f"{slug}: {s.name}", client_ip(request))
    return {"ok": True}


@app.post("/api/gprojects/{slug}/collect")
async def collect_now(slug: str, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Coleta de KPIs sem IA, agora, só deste projeto. Mostra o que foi gravado e o erro de cada integração."""
    p = _project_or_404(db, slug)
    res = await acts.collect_project(db, p)
    gravados = sum(len(r["kpis"]) for r in res)
    audit(db, auth.user.username, "kpi_collect_manual", f"{slug}: {gravados} KPI(s) gravado(s)", client_ip(request))
    return {"integracoes": res}


@app.get("/api/gprojects/{slug}/acoes-sem-ia")
def deterministic_actions(slug: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Ações que rodam sem IA neste projeto: a coleta das integrações e os relatórios feitos em código."""
    p = _project_or_404(db, slug)
    coletas = [{"integracao": i["kind"], "nome": i["name"], "pronta": i["status"] == "ready" and i["enabled"],
                "kpis": [k.strip() for k in (i["kpis"] or "").split(",") if k.strip()]}
               for i in routines.integrations_for(db, p) if i["kind"] in acts.KPI_SOURCES or i["kind"] == "project_api"]
    relatorios = []
    for a in db.query(Automation).filter(Automation.project_id == p.id).order_by(Automation.id).all():
        skill = db.get(Skill, a.skill_id) if a.skill_id else None
        if skill is None or skill.template != "relatorio_kpis":
            continue
        last = db.query(Task).filter(Task.automation_id == a.id).order_by(Task.id.desc()).first()
        relatorios.append({"id": a.id, "titulo": a.title, "ativa": a.enabled,
                           "ultima": last.status if last else None,
                           "resultado": (last.result or "")[:6000] if last and last.status == "done" else ""})
    return {"coletas": coletas, "relatorios": relatorios}


@app.get("/api/gprojects/{slug}/integrations")
def list_integrations(slug: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    return routines.integrations_for(db, _project_or_404(db, slug))


class IntegrationIn(BaseModel):
    config: dict[str, str] = Field(default_factory=dict, max_length=10)
    enabled: bool | None = None


@app.put("/api/gprojects/{slug}/integrations/{kind}")
def save_integration(slug: str, kind: str, body: IntegrationIn, request: Request,
                     auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    try:
        routines.save_integration(db, p, kind, body.config, body.enabled)
    except KeyError as e:
        raise HTTPException(404, "integração não existe no catálogo") from e
    audit(db, auth.user.username, "integration_save", f"{slug}/{kind}: {sorted(body.config)}"[:500], client_ip(request))
    return next(i for i in routines.integrations_for(db, p) if i["kind"] == kind)


@app.delete("/api/gprojects/{slug}/integrations/{kind}")
def delete_integration(slug: str, kind: str, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    db.query(Integration).filter(Integration.project_id == p.id, Integration.kind == kind).delete()
    db.commit()
    audit(db, auth.user.username, "integration_delete", f"{slug}/{kind}", client_ip(request))
    return {"ok": True}


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


class AutomationIn(BaseModel):
    agent: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, min_length=3, max_length=200)
    steps: str | None = Field(default=None, max_length=4000)
    skill_id: int | None = None
    skill_template: str | None = Field(default=None, max_length=48)
    frequency: str | None = Field(default=None, pattern=r"^(daily|weekly|monthly|event)$")
    event_type: str | None = Field(default=None, max_length=32)
    event_filter: str | None = Field(default=None, max_length=100)
    event_min: int | None = Field(default=None, ge=1, le=100)
    weekday: int | None = Field(default=None, ge=0, le=6)
    monthday: int | None = Field(default=None, ge=1, le=28)
    hour: int | None = Field(default=None, ge=0, le=23)
    auto_approve: bool | None = None
    enabled: bool | None = None


def _automation_or_404(db: DB, project: GrowthProject, aid: int) -> Automation:
    a = db.get(Automation, aid)
    if a is None or a.project_id != project.id:
        raise HTTPException(404, "automação não encontrada neste projeto")
    return a


def _apply_automation(db: DB, project: GrowthProject, a: Automation, body: AutomationIn) -> None:
    data = body.model_dump(exclude_unset=True)
    if "agent" in data and data["agent"] is not None:
        if data["agent"] not in _agent_slugs(db, project):
            raise HTTPException(404, "agente não existe neste projeto")
        a.agent_slug = data["agent"]
    if data.get("skill_template"):
        a.skill_id = _skill_from_template(db, project, data["skill_template"]).id
    elif "skill_id" in data:
        if data["skill_id"] is not None:
            _skill_or_404(db, project, data["skill_id"])
        a.skill_id = data["skill_id"]
    if data.get("event_type") is not None and data["event_type"] not in routines.EVENTS:
        raise HTTPException(422, "evento desconhecido")
    for k in ("title", "steps", "frequency", "weekday", "monthday", "hour", "auto_approve", "enabled", "event_type",
              "event_filter", "event_min"):
        if data.get(k) is not None:
            setattr(a, k, data[k].strip() if isinstance(data[k], str) else data[k])
    if a.frequency == "event" and a.event_type not in routines.EVENTS:
        raise HTTPException(422, "escolha o evento que dispara a rotina")
    # Criada (ou remarcada) depois da hora de hoje: começa na próxima vez, não dispara na hora.
    now = _now_local()
    if routines.scheduled_today(a, now.date()) and now.hour > a.hour:
        a.last_run_on = now.date()


@app.get("/api/gprojects/{slug}/automations")
def list_automations(slug: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    now = _now_local()
    rows = db.query(Automation).filter(Automation.project_id == p.id).order_by(Automation.id).all()
    return [routines.automation_json(db, a, now) for a in rows]


@app.post("/api/gprojects/{slug}/automations")
def create_automation(slug: str, body: AutomationIn, request: Request,
                      auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    if not body.agent or not body.title:
        raise HTTPException(422, "agente e título são obrigatórios")
    a = Automation(project_id=p.id, agent_slug=body.agent, title=body.title)
    _apply_automation(db, p, a, body)
    db.add(a)
    db.commit()
    audit(db, auth.user.username, "automation_create",
          f"{slug}/{a.agent_slug}: {a.title} ({routines.describe_schedule(a)}, auto={a.auto_approve})"[:500], client_ip(request))
    return routines.automation_json(db, a, _now_local())


@app.patch("/api/gprojects/{slug}/automations/{aid}")
def update_automation(slug: str, aid: int, body: AutomationIn, request: Request,
                      auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    a = _automation_or_404(db, p, aid)
    _apply_automation(db, p, a, body)
    db.commit()
    audit(db, auth.user.username, "automation_update", f"#{a.id} {slug}: {body.model_dump(exclude_unset=True)}"[:500],
          client_ip(request))
    return routines.automation_json(db, a, _now_local())


@app.delete("/api/gprojects/{slug}/automations/{aid}")
def delete_automation(slug: str, aid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, slug)
    a = _automation_or_404(db, p, aid)
    db.delete(a)
    db.commit()
    audit(db, auth.user.username, "automation_delete", f"#{aid} {slug}: {a.title}", client_ip(request))
    return {"ok": True}


@app.post("/api/gprojects/{slug}/automations/{aid}/run")
def run_automation_now(slug: str, aid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Rodar agora é o owner pedindo: a tarefa já nasce aprovada e entra na fila."""
    p = _project_or_404(db, slug)
    a = _automation_or_404(db, p, aid)
    if not routines.is_code_report(db, a) and routines.has_open_run(db, a):
        raise HTTPException(409, "a última execução desta automação ainda está aberta no kanban")
    t = routines.create_run(db, a, approved=True, actor=auth.user.username)
    if t.status == "approved":  # o relatório de KPIs já nasce concluído (feito em código)
        growth.runner.submit(t.id)
    return _task_json(t, {p.id: p})


# ---------- central de conteúdo: posts, e-mails e saídas dos agentes (por projeto) ----------

class ContentIn(BaseModel):
    project: str | None = Field(default=None, max_length=48)  # só na criação
    channel: str | None = Field(default=None, max_length=24)
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=20000)
    recipient: str | None = Field(default=None, max_length=300)
    purpose: str | None = Field(default=None, max_length=60)
    media: str | None = Field(default=None, max_length=4000)
    img_title: str | None = Field(default=None, max_length=200)  # título curto escrito NA imagem
    img_text: str | None = Field(default=None, max_length=400)  # frase de apoio escrita NA imagem
    visual_format: str | None = Field(default=None, max_length=12)  # story | carrossel | reel | "" (sem imagem)
    scheduled_at: str | None = Field(default=None, max_length=20)  # "AAAA-MM-DDTHH:MM" local; "" limpa
    image_mode: str | None = Field(default=None, max_length=10)  # template | reference | prompt
    template_id: int | None = None  # modo template: qual template (0/vazio = o aprovado do projeto)
    ref_id: int | None = None  # modo reference: qual referência (0/vazio = todas)
    image_prompt: str | None = Field(default=None, max_length=4000)  # modo prompt
    template_ids: dict[str, int] | None = None  # carrossel: {"capa": id, "pagina": id, "cta": id}; 0 = o padrão
    slides: list[dict] | None = Field(default=None, max_length=12)  # carrossel: páginas do meio + CTA
    media_source: str | None = Field(default=None, max_length=12)  # gerar_imagem | gerar_video | upload | link | nenhuma
    media_url: str | None = Field(default=None, max_length=1000)  # link coletado na internet
    asset_ids: list[int] | None = Field(default=None, max_length=12)  # ativos do projeto na arte; [] = automático


class ContentDecideIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=1000)


class ContentSentIn(BaseModel):
    link: str = Field(default="", max_length=500, pattern=r"^$|^https?://\S+$")


def _content_or_404(db: DB, cid: int) -> ContentItem:
    c = db.get(ContentItem, cid)
    if c is None:
        raise HTTPException(404, "conteúdo não encontrado")
    return c


def _content_json(db: DB, c: ContentItem) -> dict:
    p = db.get(GrowthProject, c.project_id)
    return cnt.item_json(c, p.slug if p else "", db=db)


def _scope(db: DB, project: str) -> int | None:
    return None if project == "all" else _project_or_404(db, project).id


@app.get("/api/content/channels")
def content_channels(auth: Auth = Depends(current_auth)):
    return cnt.channels_json()


@app.get("/api/content")
def list_content(project: str = "all", days: int = 30, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Tudo que ainda vai sair (aguardando, aprovado, falhou) + o que saiu ou foi rejeitado nos últimos `days` dias."""
    days = max(1, min(days, 365))
    pid = _scope(db, project)
    q = db.query(ContentItem)
    if pid is not None:
        q = q.filter(ContentItem.project_id == pid)
    since = utcnow() - timedelta(days=days)
    open_rows = q.filter(ContentItem.status.in_(cnt.EDITABLE)).order_by(ContentItem.id.desc()).limit(500).all()
    closed = (
        q.filter(ContentItem.status.in_(("sent", "rejected")),
                 func.coalesce(ContentItem.sent_at, ContentItem.decided_at, ContentItem.created_at) >= since)
        .order_by(ContentItem.id.desc()).limit(500).all()
    )
    projects = _projects_by_id(db)
    now = cnt.local_now()
    items = [cnt.item_json(c, projects[c.project_id].slug if c.project_id in projects else "", now, db)
             for c in open_rows + closed]
    counts: dict[str, int] = {}
    for it in items:
        counts[it["stage"]] = counts.get(it["stage"], 0) + 1
    return {"items": items, "counts": counts, "days": days}


@app.post("/api/content")
def create_content(body: ContentIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Peça escrita pelo próprio owner: já nasce aprovada (quem escreveu é quem aprova)."""
    p = _project_or_404(db, body.project)
    if not (body.body or "").strip():
        raise HTTPException(422, "o texto é obrigatório")
    c = ContentItem(project_id=p.id, status="approved", decided_at=utcnow())
    _apply_content(c, body, db)
    db.add(c)
    db.commit()
    audit(db, auth.user.username, "content_create", f"#{c.id} {p.slug}/{c.channel}: {c.title or c.body[:60]}"[:500],
          client_ip(request))
    return _content_json(db, c)


def _apply_content(c: ContentItem, body: ContentIn, db: DB | None = None) -> None:
    data = body.model_dump(exclude_unset=True)
    if data.get("image_mode") is not None:
        mode = body.image_mode.strip().lower()
        if mode not in cnt.IMAGE_MODES:
            raise HTTPException(422, "modo de imagem inválido (template, reference ou prompt)")
        c.image_mode = mode
    if data.get("image_prompt") is not None:
        c.image_prompt = body.image_prompt.strip()
    if "template_ids" in data:
        clean: dict[str, int] = {}
        for role, val in (body.template_ids or {}).items():
            if role not in lay.ROLES or not val:
                continue
            a = db.get(LayoutAsset, val) if db is not None else None
            if db is not None and (a is None or a.kind != "template" or a.project_id != c.project_id):
                raise HTTPException(422, "template não encontrado neste projeto")
            clean[role] = int(val)
        if json.dumps(clean, sort_keys=True) != json.dumps(lay.piece_template_ids(c), sort_keys=True):
            c.template_ids = json.dumps(clean)
            c.template_id = clean.get("capa") if c.visual_format == "carrossel" else c.template_id
    if data.get("slides") is not None and db is not None:
        new = lay.clean_slides(body.slides)
        old = lay.piece_slides(c)
        strip = lambda xs: [(x["role"], x["title"], x["text"]) for x in xs]  # noqa: E731
        if strip(new) != strip(old):
            lay.set_slides(db, c, new)
    if data.get("asset_ids") is not None:
        ok = {a.id for a in ast.project_assets(db, c.project_id)} if db is not None else set()
        ids = [i for i in dict.fromkeys(body.asset_ids) if i in ok][:ast.MAX_SENT]
        if len(ids) != len(set(body.asset_ids)) and db is not None:
            raise HTTPException(422, "ativo não encontrado neste projeto (ou mais de 5)")
        c.asset_ids = json.dumps(ids) if ids else ""
    if data.get("media_source") is not None:
        src = body.media_source.strip().lower()
        if src not in med.SOURCES:
            raise HTTPException(422, "fonte da mídia inválida")
        c.media_source = src
    if data.get("media_url") is not None:
        url = body.media_url.strip()
        if url:
            try:
                med.check_url(url)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        if url != c.media_url:
            c.media_url = url
            if c.media_source == "link" and c.media_id and db is not None:  # link novo: a mídia baixada do antigo sai
                old_m = db.get(LayoutAsset, c.media_id)
                if old_m is not None:
                    db.delete(old_m)
                c.media_id = None
    for field, kind in (("template_id", "template"), ("ref_id", "reference")):
        if field in data:
            val = data[field] or None
            if val is not None and db is not None:
                a = db.get(LayoutAsset, val)
                if a is None or a.kind != kind or a.project_id != c.project_id:
                    raise HTTPException(422, f"{'template' if kind == 'template' else 'referência'} não encontrado neste projeto")
            setattr(c, field, val)
    if "channel" in data and body.channel is not None:
        c.channel = cnt.norm_channel(body.channel)
    for field in ("title", "body", "recipient", "purpose", "media", "img_title", "img_text"):
        if data.get(field) is not None:
            setattr(c, field, str(data[field]).strip())
    if data.get("visual_format") is not None:
        vf = body.visual_format.strip().lower()
        if vf and vf not in cnt.VISUAL_FORMATS:
            raise HTTPException(422, "formato visual inválido (story, carrossel ou reel)")
        if vf != c.visual_format and c.image_id:
            raise HTTPException(422, "a peça já tem imagem; apague-a antes de mudar o formato")
        c.visual_format = vf
    if "scheduled_at" in data:
        raw = (body.scheduled_at or "").strip()
        when = cnt.parse_when(raw) if raw else None
        if raw and when is None:
            raise HTTPException(422, "data inválida (use AAAA-MM-DD HH:MM)")
        c.scheduled_at = when
    if c.scheduled_at and c.status == "approved" and cnt.needs_image(c):
        raise HTTPException(409, "esta peça é visual e ainda não tem imagem: gere ou suba a imagem antes de agendar")
    if not c.body.strip():
        raise HTTPException(422, "o texto não pode ficar vazio")
    cnt.touch(c)


@app.patch("/api/content/{cid}")
def update_content(cid: int, body: ContentIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "conteúdo enviado ou rejeitado não se edita; volte para aprovação antes")
    if body.project is not None:
        raise HTTPException(422, "não dá para mudar o projeto de um conteúdo")
    _apply_content(c, body, db)
    if c.status == "failed":
        c.status, c.note = "approved", ""
    db.commit()
    pipeline.reset(c)  # o owner mexeu na peça: as partes que falharam ganham novas tentativas
    db.commit()
    if {"title", "body", "img_title", "img_text", "slides"} & set(body.model_dump(exclude_unset=True)):
        lay.rerender_piece(db, c)  # o texto da imagem mudou: refaz a imagem final, com o mesmo fundo
    audit(db, auth.user.username, "content_edit", f"#{c.id}: {sorted(body.model_dump(exclude_unset=True))}", client_ip(request))
    return _content_json(db, c)


@app.post("/api/content/{cid}/decide")
async def decide_content(cid: int, body: ContentDecideIn, request: Request,
                         auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    try:
        appr.decide_content(db, c, body.approve, body.note)
    except appr.ApprovalError as e:
        raise HTTPException(e.code, str(e)) from e
    ip = client_ip(request)
    audit(db, auth.user.username, "content_" + ("approve" if body.approve else "reject"), f"#{c.id} {c.channel}", ip)
    auto = await _auto_send(db, c, auth.user.username, ip) if body.approve else None
    return {**_content_json(db, c), "auto": auto}


def postiz_network(identifier: str) -> str:
    """Canal do OS para um provedor do Postiz: "instagram-standalone" e "instagram" são Instagram, "linkedin-page" é LinkedIn..."""
    ident = str(identifier or "").strip().lower()
    if ident in cnt.CHANNELS:
        return ident
    head = ident.split("-")[0]
    return head if head in cnt.CHANNELS else ident


def choose_postiz_channel(db: DB, project: GrowthProject, channel: str, mine: list[dict]) -> tuple[str, str]:
    """Qual canal do Postiz leva a peça do projeto: (id, "") ou ("", motivo). O Postiz é um só; cada projeto tem o seu canal.

    1) o campo "canal" da integração do projeto (nome ou id); 2) o canal da rede cujo nome cita o projeto;
    3) o único canal da rede. Se ainda sobrar dúvida, o owner escolhe no Publicar.
    """
    net = cnt.CHANNELS[channel][0]
    row = db.query(Integration).filter(Integration.project_id == project.id, Integration.kind == "postiz").one_or_none()
    want = str(routines._config(row).get("canal", "")).strip().lower()
    if want:
        hit = ([x for x in mine if str(x.get("id")).lower() == want or str(x.get("nome", "")).strip().lower() == want]
               or [x for x in mine if want in str(x.get("nome", "")).lower()])
        if len(hit) == 1:
            return str(hit[0]["id"]), ""
        return "", (f'o canal "{want}" configurado em Integrações > Postiz '
                    f'{"não foi achado entre os canais de " + net if not hit else "bate com mais de um canal de " + net}.')
    named = [x for x in mine if project.name.strip().lower() in str(x.get("nome", "")).lower()]
    pool = named or mine
    if len(pool) == 1:
        return str(pool[0]["id"]), ""
    return "", (f"há {len(pool)} canais de {net} no Postiz; preencha o campo Canal em Integrações > Postiz deste projeto "
                "ou escolha um no Publicar.")


AUTO_NOTE = "Envio automático ao Postiz não saiu: "
PADRAO = "padrao_"  # chave na config do Postiz: canal escolhido por rede (padrao_instagram...)


def _slug(text: str) -> str:
    """Minúsculas, sem acento e só letras/números: "A Ilha" e "a-ilha" viram "ailha"."""
    base = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", base)


def _project_keys(project: GrowthProject) -> set[str]:
    """Nomes pelos quais um canal do Postiz costuma citar o projeto: nome, slug e o miolo dos domínios."""
    keys = {_slug(project.name), _slug(project.slug)}
    for d in re.split(r"[,\s]+", project.domains or ""):
        parts = [x for x in d.lower().split(".") if x and x not in ("www", "com", "br", "io", "net", "org", "app")]
        keys.update(_slug(x) for x in parts)
    return {k for k in keys if len(k) >= 3}


def pick_postiz_channel(db: DB, project: GrowthProject, channel: str, mine: list[dict]) -> dict | None:
    """Qual canal do Postiz recebe a peça de `channel` neste projeto, sem perguntar (None = não dá para saber).

    Um só canal da rede: ele. Vários: o que o owner já escolheu antes neste projeto (guardado na integração) e,
    sem isso, o único cujo nome cita o projeto (ex.: "A Ilha Jogo" no projeto a-ilha).
    """
    if str(routines._config(db.query(Integration).filter(Integration.project_id == project.id,
                                                          Integration.kind == "postiz").one_or_none()).get("canal", "")).strip():
        found, _ = choose_postiz_channel(db, project, channel, mine)  # campo "Canal" da integração manda mais que tudo
        return next((x for x in mine if str(x["id"]) == found), None)
    if len(mine) == 1:
        return mine[0]
    saved = routines._config(db.query(Integration).filter(Integration.project_id == project.id,
                                                           Integration.kind == "postiz").one_or_none()).get(PADRAO + channel)
    if saved and (hit := next((x for x in mine if str(x.get("id")) == saved), None)):
        return hit
    keys = _project_keys(project)
    named = [x for x in mine if any(k in _slug(x.get("nome", "")) for k in keys)]
    return named[0] if len(named) == 1 else None


def remember_postiz_channel(db: DB, project: GrowthProject, channel: str, canal_id: str) -> None:
    row = db.query(Integration).filter(Integration.project_id == project.id, Integration.kind == "postiz").one_or_none()
    if row is None:
        return
    cfg = routines._config(row)
    cfg[PADRAO + channel] = str(canal_id)[:120]
    row.config = json.dumps(cfg, ensure_ascii=False)
    db.commit()


async def _auto_send(db: DB, c: ContentItem, actor: str, ip: str) -> dict:
    """Peça de rede social aprovada vai sozinha ao Postiz: com data futura, agenda; sem data (ou já passou), publica agora.

    Se não der (Postiz desligado, nenhum canal da rede ou vários sem como saber qual), a peça fica aprovada com o motivo
    na nota, e o botão Publicar continua valendo. Devolve o desfecho para a tela: {"state", "message", "pick"}.
    """
    if c.status != "approved" or cnt.CHANNELS.get(c.channel, ("", ""))[1] != "social" or cnt.needs_image(c):
        return {"state": "skipped", "message": "", "pick": []}
    project = db.get(GrowthProject, c.project_id)

    def why(msg: str, pick: list | None = None) -> dict:
        c.note = (AUTO_NOTE + msg)[:1000]
        cnt.touch(c)
        db.commit()
        return {"state": "pick" if pick else "held", "message": msg, "pick": pick or []}

    if "postiz" not in acts._ready(db, project):
        return why("a integração Postiz não está ligada e pronta neste projeto (Automações > Integrações).")
    try:
        res = await acts.run_operation(db, acts.Ticket(0, project.id, "owner", 0), "postiz", "listar_canais", {})
        if res.get("status") != "done":
            return why(res.get("message") or "não consegui listar os canais do Postiz.")
        canais = [x for x in (res.get("result") or {}).get("canais", []) if not x.get("desativado")]
        mine = [x for x in canais if postiz_network(x.get("rede", "")) == c.channel]
        if not mine:
            return why(f"nenhum canal de {cnt.CHANNELS[c.channel][0]} conectado no Postiz.")
        chosen = pick_postiz_channel(db, project, c.channel, mine)
        if chosen is None:
            cfg_msg = choose_postiz_channel(db, project, c.channel, mine)[1] if routines._config(
                db.query(Integration).filter(Integration.project_id == project.id, Integration.kind == "postiz").one_or_none()).get("canal") else ""
            if cfg_msg:  # o Canal configurado não bate: corrigir a configuração, não escolher no ato
                return why(cfg_msg)
            return why(f"há {len(mine)} canais de {cnt.CHANNELS[c.channel][0]} no Postiz e nenhum leva o nome de {project.name}; escolha um.",
                       [{"id": str(x["id"]), "nome": x.get("nome") or str(x["id"]), "rede": x.get("rede", "")} for x in mine])
        later = bool(c.scheduled_at and c.scheduled_at > cnt.local_now())
        out = await _publish_piece(db, c, PublishIn(canal_id=str(chosen["id"])), _Actor(actor), ip)
        action = out.get("action") or {}
        if c.status == "sent":
            return {"state": "sent", "message": f"{'Agendada' if later else 'Publicada'} no Postiz ({chosen.get('nome') or chosen['id']}).", "pick": []}
        return {"state": "failed", "message": action.get("error") or c.note, "pick": []}
    except HTTPException as e:
        return why(str(e.detail))


async def _auto_send_loop() -> None:
    while True:
        try:
            await auto_send_pending()
        except Exception:  # noqa: BLE001 - o laço nunca pode morrer
            growth.log.exception("envio automático ao Postiz")
        await asyncio.sleep(60)


class _Actor:
    """Quem dispara um envio fora de uma requisição (aprovação em lote, varredura do agendador)."""

    def __init__(self, username: str, user=None):
        self.user = user or type("U", (), {"username": username})()


async def auto_send_pending() -> None:
    """Varredura do agendador: peça social aprovada que nunca foi ao Postiz vai agora.

    Pega aprovações por qualquer caminho (lote, peça criada pelo owner, data ou imagem acertadas depois).
    Só olha peças com data futura ou aprovadas nas últimas 24 h, e pula as que já tentaram (há uma ação)
    ou já têm o motivo de não ter ido na nota.
    """
    db = new_session()
    try:
        social = [k for k, v in cnt.CHANNELS.items() if v[1] == "social"]
        since = utcnow() - timedelta(hours=24)
        rows = (db.query(ContentItem)
                .filter(ContentItem.status == "approved", ContentItem.channel.in_(social),
                        or_(ContentItem.scheduled_at > cnt.local_now(), ContentItem.decided_at >= since))
                .order_by(ContentItem.id).limit(20).all())
        for c in rows:
            if (c.note or "").startswith(AUTO_NOTE) or cnt.needs_image(c):
                continue
            if db.query(Action).filter(Action.idempotency_key.like(f"conteudo-{c.id}-%")).count():
                continue
            try:
                await _auto_send(db, c, "agendador", "")
            except Exception:  # noqa: BLE001 - uma peça com problema não trava as outras
                growth.log.exception("envio automático da peça #%s", c.id)
    finally:
        db.close()


class PublishIn(BaseModel):
    canal_id: str = Field(default="", max_length=120)
    lembrar: bool = False  # vale como o canal padrão desta rede neste projeto
    totp: str = Field(default="", max_length=12)
    agora: bool = False  # rede social: publica já, mesmo que a peça tenha data futura


@app.get("/api/gprojects/{slug}/postiz/canais")
async def postiz_channels(slug: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Canais conectados no Postiz do projeto (para escolher onde publicar uma peça)."""
    p = _project_or_404(db, slug)
    res = await acts.run_operation(db, acts.Ticket(0, p.id, "owner", 0), "postiz", "listar_canais", {})
    if res["status"] != "done":
        raise HTTPException(409, res["message"])
    return res["result"]["canais"]


@app.post("/api/content/{cid}/publish")
async def publish_content(cid: int, body: PublishIn, request: Request,
                          auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """O owner manda sair uma peça JÁ aprovada pela integração do canal (e-mail por SMTP, redes pelo Postiz).

    Apertar o botão é a aprovação da ação; e-mail para terceiros exige também o 2FA.
    """
    return await _publish_piece(db, _content_or_404(db, cid), body, auth, client_ip(request))


async def _publish_piece(db: DB, c: ContentItem, body: PublishIn, auth: Auth, ip: str) -> dict:
    if c.status not in ("approved", "failed"):
        raise HTTPException(409, "só conteúdo aprovado pode ser publicado")
    if cnt.needs_image(c):
        raise HTTPException(409, "esta peça é visual e ainda não tem imagem: gere ou suba a imagem antes de publicar")
    project = db.get(GrowthProject, c.project_id)
    when = None
    if c.channel == "email":
        kind, operation = "email", "enviar_email"
        if not (c.recipient or "").strip():
            raise HTTPException(422, "falta o destinatário do e-mail")
        raw = {"para": c.recipient, "assunto": c.title or "(sem assunto)", "texto": c.body}
    elif cnt.CHANNELS.get(c.channel, ("", ""))[1] == "social":
        kind, operation = "postiz", "publicar_post"
        canal = body.canal_id.strip()
        if not canal:  # sem canal escolhido: o do projeto (campo Canal da integração, nome citando o projeto ou o único)
            res = await acts.run_operation(db, acts.Ticket(0, project.id, "owner", 0), "postiz", "listar_canais", {})
            mine = [x for x in (res.get("result") or {}).get("canais", []) if not x.get("desativado")
                    and postiz_network(x.get("rede", "")) == c.channel]
            canal, msg = choose_postiz_channel(db, project, c.channel, mine) if mine else ("", "nenhum canal desta rede no Postiz")
            if not canal:
                raise HTTPException(422, f"escolha o canal do Postiz: {msg}")
        raw = {"canal_id": canal, "texto": c.body}
        if media_ids := cnt.media_asset_ids(c):
            raw["midia_ids"] = ",".join(map(str, media_ids))
        if c.visual_format == "story":
            raw["tipo"] = "story"
        if not body.agora and c.scheduled_at and c.scheduled_at > cnt.local_now():
            operation = "agendar_post"
            when = c.scheduled_at.replace(tzinfo=ZoneInfo(get_settings().timezone)).astimezone(ZoneInfo("UTC"))
            raw["quando"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        raise HTTPException(422, "este canal não tem integração de envio; marque como enviada depois de publicar")
    conn = acts.REGISTRY[kind]
    op = conn.ops[operation]
    if kind not in acts._ready(db, project):
        raise HTTPException(409, f"{kind} não está pronta neste projeto (ligue e configure em Automações > Integrações)")
    try:
        params = op.clean(raw)
    except acts.ConnectorError as e:
        raise HTTPException(422, str(e)) from e
    if acts.over_cap(db, project, kind, op):
        raise HTTPException(429, f"limite de {op.daily_cap} envios por dia em {kind} neste projeto")
    if op.risk == "alto" and not consume_totp(db, auth.user, body.totp):
        audit(db, auth.user.username, "approve_failed", f"publicar conteúdo #{c.id}", ip)
        raise HTTPException(403, "código 2FA inválido")
    a = Action(project_id=c.project_id, integration=kind, operation=operation, effect="external", risk=op.risk,
                    title=f"Publicar: {c.title or c.body[:60]}"[:200], params=json.dumps(params, ensure_ascii=False),
                    status="approved", idempotency_key=f"conteudo-{c.id}-{utcnow().timestamp()}")
    db.add(a)
    db.commit()
    appr._record(db, c.project_id, "action", a.id, a.title, f"conteudo:{c.id}", op.risk, True,
                 "2fa" if op.risk == "alto" else "clique")
    audit(db, auth.user.username, "content_publish", f"#{c.id} {c.channel} via {kind}.{operation}", ip)
    await acts.execute_approved(db, a)
    if body.lembrar and kind == "postiz" and a.status == "done":
        remember_postiz_channel(db, project, c.channel, body.canal_id.strip())
    if a.status == "done":
        c.status, c.sent_at, c.note = "sent", utcnow(), ""
        if a.external_ref.startswith("http"):
            c.link = a.external_ref[:500]
    else:
        c.status, c.note = "failed", f"Falhou ao publicar: {a.error}"[:1000]
    cnt.touch(c)
    db.commit()
    if c.status == "sent":
        appr.emit(db, c.project_id, "content.sent", "content", c.id, {"channel": c.channel, "title": c.title[:120], "link": c.link})
    return {**_content_json(db, c), "action": {"id": a.id, "status": a.status, "error": a.error}}


@app.post("/api/content/{cid}/sent")
def mark_content_sent(cid: int, body: ContentSentIn, request: Request,
                      auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """O owner publicou/enviou (até as integrações de envio existirem, é ele quem aperta o botão lá fora)."""
    c = _content_or_404(db, cid)
    if c.status != "approved":
        raise HTTPException(409, "só conteúdo aprovado pode ser marcado como enviado")
    c.status, c.sent_at, c.link = "sent", utcnow(), body.link.strip()
    cnt.touch(c)
    db.commit()
    appr.emit(db, c.project_id, "content.sent", "content", c.id, {"channel": c.channel, "title": c.title[:120], "link": c.link})
    audit(db, auth.user.username, "content_sent", f"#{c.id} {c.channel} {c.link}"[:500], client_ip(request))
    return _content_json(db, c)


@app.post("/api/content/{cid}/reopen")
def reopen_content(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Rejeitado, falhou ou aprovado volta para "aguardando aprovação" (para revisar de novo)."""
    c = _content_or_404(db, cid)
    if c.status == "sent":
        raise HTTPException(409, "conteúdo já enviado não volta")
    c.status, c.decided_at, c.note = "pending", None, ""
    cnt.touch(c)
    db.commit()
    audit(db, auth.user.username, "content_reopen", f"#{c.id}", client_ip(request))
    return _content_json(db, c)


class PieceImageIn(BaseModel):
    feedback: str = Field(default="", max_length=500)
    prompt: str = Field(default="", max_length=6000)  # prompt editado pelo owner: vale no lugar do montado (só a capa)


class PieceUploadIn(BaseModel):
    data: str = Field(max_length=6_000_000)


@app.post("/api/content/{cid}/image")
async def content_generate_image(cid: int, body: PieceImageIn, request: Request, auth: Auth = Depends(current_auth),
                                 db: DB = Depends(get_db)):
    """O owner pede a imagem da peça (nova ou outra versão), no layout aprovado do projeto. Custa uma imagem."""
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "só dá para mudar a imagem de uma peça em aprovação ou aprovada")
    pipeline.reset(c)
    try:
        await lay.generate_piece_image(db, c, body.feedback, body.prompt)
    except ProviderError as e:
        c.image_error = str(e)[:500]
        db.commit()
        raise HTTPException(422, str(e)) from e
    audit(db, auth.user.username, "content_image", f"#{c.id} {c.visual_format}", client_ip(request))
    return _content_json(db, c)


class SlideIn(BaseModel):
    prompt: str | None = Field(default=None, max_length=6000)  # prompt da imagem desta página ("" = volta ao montado)
    media: str | None = Field(default=None, max_length=600)  # o que a imagem mostra
    assets: list[int] | None = Field(default=None, max_length=5)  # referências escolhidas (ids de ativos do projeto)
    auto_assets: bool | None = None  # True = referências automáticas; False = só as de "assets"
    feedback: str = Field(default="", max_length=500)


def _slide_or_404(db: DB, cid: int, i: int) -> tuple[ContentItem, list[dict]]:
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "só dá para mudar a imagem de uma peça em aprovação ou aprovada")
    slides = lay.piece_slides(c)
    if c.visual_format != "carrossel" or not 0 <= i < len(slides):
        raise HTTPException(404, "página não encontrada")
    return c, slides


def _apply_slide(db: DB, c: ContentItem, slides: list[dict], i: int, body: SlideIn) -> None:
    s = slides[i]
    if body.media is not None:
        if body.media.strip():
            s["media"] = body.media.strip()
        else:
            s.pop("media", None)
    if body.assets is not None:
        ok = {a.id for a in ast.project_assets(db, c.project_id)}
        ids = [x for x in dict.fromkeys(body.assets) if x in ok]
        if len(ids) != len(set(body.assets)):
            raise HTTPException(422, "ativo não encontrado neste projeto")
        s["assets"] = ids
        if not ids:
            s.pop("assets", None)
    if body.auto_assets is not None:
        if body.auto_assets:
            s.pop("manual_assets", None)
        else:
            s["manual_assets"] = True
    if body.prompt is not None:
        text = body.prompt.strip()
        shown = lay.slide_plan(db, c, s)["prompt"].strip()  # igual ao montado = o owner não mudou nada
        if text and text != shown:
            s["prompt"] = text
        else:
            s.pop("prompt", None)
    c.slides = json.dumps(slides, ensure_ascii=False)
    cnt.touch(c)
    db.commit()


@app.get("/api/content/{cid}/slides")
def content_slides(cid: int, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Cada página do carrossel com estado, prompt, referências e o que falta (tudo que o formulário mostra)."""
    c = _content_or_404(db, cid)
    return {"slides": cnt.slides_detail(db, c) if c.visual_format == "carrossel" else []}


@app.put("/api/content/{cid}/slides/{i}")
def content_slide_settings(cid: int, i: int, body: SlideIn, request: Request, auth: Auth = Depends(current_auth),
                           db: DB = Depends(get_db)):
    """Grava o prompt, a descrição e as referências de UMA página, sem gerar nada."""
    c, slides = _slide_or_404(db, cid, i)
    _apply_slide(db, c, slides, i, body)
    audit(db, auth.user.username, "content_slide_edit", f"#{c.id} página {i}", client_ip(request))
    return {"slides": cnt.slides_detail(db, c)}


@app.post("/api/content/{cid}/slides/{i}/image")
async def content_slide_image(cid: int, i: int, body: SlideIn, request: Request, auth: Auth = Depends(current_auth),
                              db: DB = Depends(get_db)):
    """Gera de novo SÓ a imagem desta página (1 imagem paga), com o prompt e as referências dela, e a monta no template."""
    c, slides = _slide_or_404(db, cid, i)
    if not c.image_id:
        raise HTTPException(409, "gere a capa primeiro: as páginas seguem o mesmo cenário")
    _apply_slide(db, c, slides, i, body)
    key = pipeline._slide_key(i, slides[i]["role"])
    try:
        await lay.build_slide(db, c, i, body.feedback, force=True)
    except ProviderError as e:
        db.rollback()
        st = pipeline._state(c)
        st["errors"][key] = str(e)[:400]
        pipeline._save(c, st)
        db.commit()
        raise HTTPException(422, str(e)) from e
    pipeline._record_ok(c, key)
    db.commit()
    audit(db, auth.user.username, "content_slide_image", f"#{c.id} página {i}", client_ip(request))
    return {"item": _content_json(db, c), "slides": cnt.slides_detail(db, c)}


@app.post("/api/content/{cid}/complete")
async def content_complete(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """O owner manda completar a peça agora: refaz só as partes que faltam (texto, capa, páginas, CTA), zerando as tentativas."""
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "só dá para completar uma peça em aprovação ou aprovada")
    await pipeline.run_piece(db, c, force=True)
    audit(db, auth.user.username, "content_complete", f"#{c.id}", client_ip(request))
    return _content_json(db, c)


@app.post("/api/content/{cid}/image/upload")
async def content_upload_image(cid: int, body: PieceUploadIn, request: Request, auth: Auth = Depends(current_auth),
                               db: DB = Depends(get_db)):
    """Imagem do owner para a peça. Se a peça usa um layout (modo template com template definido), a imagem ENTRA no
    layout, como a mídia da peça; só sem layout ela substitui a imagem final como está."""
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "só dá para mudar a imagem de uma peça em aprovação ou aprovada")
    if not c.visual_format:
        raise HTTPException(422, "esta peça não é visual: escolha story, carrossel ou reel antes")
    try:
        data, mime = lay.decode_image(body.data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    template = None
    if (c.image_mode or "template") == "template":
        try:
            template = lay.piece_template(db, c)
        except ProviderError as e:
            raise HTTPException(422, str(e)) from e
    if template is not None:
        c.media_source = "upload"
        med.set_media(db, c, data, mime, "upload")
        audit(db, auth.user.username, "content_image_upload", f"#{c.id} no layout #{template.id}", client_ip(request))
        await _compose_after_media(db, c)
        return _content_json(db, c)
    lay.set_piece_image(db, c, data, mime, model="upload")
    audit(db, auth.user.username, "content_image_upload", f"#{c.id} substitui (sem layout)", client_ip(request))
    return _content_json(db, c)


class MediaUploadIn(BaseModel):
    data: str = Field(max_length=35_000_000)  # vídeo de até 25 MB em base64


class MediaLinkIn(BaseModel):
    url: str = Field(max_length=1000)


def _editable_visual(db: DB, cid: int) -> ContentItem:
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "só dá para mudar a mídia de uma peça em aprovação ou aprovada")
    if not c.visual_format:
        raise HTTPException(422, "esta peça não é visual: escolha story, carrossel ou reel antes")
    return c


async def _compose_after_media(db: DB, c: ContentItem) -> None:
    """Mídia nova: monta a imagem no layout na hora (se o template tem janela, sem IA). Falha fica em image_error."""
    if (c.image_mode or "template") != "template":
        return
    try:
        await lay.generate_piece_image(db, c)
    except ProviderError as e:
        db.rollback()
        c.image_error = str(e)[:500]
        db.commit()


@app.post("/api/content/{cid}/media/upload")
async def content_upload_media(cid: int, body: MediaUploadIn, request: Request, auth: Auth = Depends(current_auth),
                               db: DB = Depends(get_db)):
    """Foto ou vídeo do owner que entra NO layout (diferente de "subir imagem final", que substitui tudo)."""
    c = _editable_visual(db, cid)
    raw = body.data.split(",", 1)[1] if body.data.startswith("data:") and "," in body.data else body.data
    try:
        data = base64.b64decode(raw, validate=True)
        mime = med.check_media(data)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e) if isinstance(e, ValueError) and str(e) else "arquivo inválido") from e
    c.media_source = "upload"
    med.set_media(db, c, data, mime, "upload")
    audit(db, auth.user.username, "content_media_upload", f"#{c.id} {mime} {len(data) // 1024} KB", client_ip(request))
    await _compose_after_media(db, c)
    return _content_json(db, c)


@app.post("/api/content/{cid}/media/link")
async def content_media_link(cid: int, body: MediaLinkIn, request: Request, auth: Auth = Depends(current_auth),
                             db: DB = Depends(get_db)):
    """Baixa uma foto ou um vídeo de um link público (https, arquivo direto) e usa na peça."""
    c = _editable_visual(db, cid)
    try:
        data, mime = await med.download(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    c.media_source, c.media_url = "link", body.url.strip()
    med.set_media(db, c, data, mime, "link", c.media_url)
    audit(db, auth.user.username, "content_media_link", f"#{c.id} {c.media_url}"[:500], client_ip(request))
    await _compose_after_media(db, c)
    return _content_json(db, c)


@app.delete("/api/content/{cid}/media")
def content_delete_media(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    if c.media_id and (m := db.get(LayoutAsset, c.media_id)) is not None:
        db.delete(m)
    c.media_id, c.media_job = None, ""
    db.commit()
    audit(db, auth.user.username, "content_media_delete", f"#{c.id}", client_ip(request))
    return _content_json(db, c)


@app.post("/api/content/{cid}/video")
async def content_generate_video(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Anima a peça (mesmo template, texto e fundo) num MP4. Não usa IA: só o serviço motion."""
    c = _content_or_404(db, cid)
    if c.status not in cnt.EDITABLE:
        raise HTTPException(409, "só dá para gerar o vídeo de uma peça em aprovação ou aprovada")
    try:
        await lay.generate_piece_video(db, c)
    except ProviderError as e:
        raise HTTPException(422, str(e)) from e
    audit(db, auth.user.username, "content_video", f"#{c.id} {c.visual_format}", client_ip(request))
    return _content_json(db, c)


@app.delete("/api/content/{cid}/video")
def content_delete_video(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    lay.drop_video(db, c)
    db.commit()
    audit(db, auth.user.username, "content_video_delete", f"#{c.id}", client_ip(request))
    return _content_json(db, c)


@app.delete("/api/content/{cid}")
def delete_content(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    if c.status == "sent":
        raise HTTPException(409, "conteúdo enviado fica no histórico")
    lay.drop_slides(db, c)
    for aid in (c.image_id, c.bg_id, c.video_id, c.media_id):
        if aid and (img := db.get(LayoutAsset, aid)) is not None:
            db.delete(img)
    db.delete(c)
    db.commit()
    audit(db, auth.user.username, "content_delete", f"#{cid} {c.channel}: {c.title or c.body[:60]}"[:500], client_ip(request))
    return {"ok": True}


@app.get("/api/content/agenda")
def content_agenda(project: str = "all", days: int = 14, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """O que está programado nos próximos `days` dias: conteúdo com data e execuções das rotinas."""
    days = max(1, min(days, 62))
    pid = _scope(db, project)
    start, end = cnt.agenda_window(days)
    projects = _projects_by_id(db)
    slug = lambda i: projects[i].slug if i in projects else ""  # noqa: E731
    now = cnt.local_now()
    events = []
    q = db.query(ContentItem).filter(
        ContentItem.scheduled_at.isnot(None), ContentItem.scheduled_at >= start, ContentItem.scheduled_at < end,
        ContentItem.status.in_(("pending", "approved", "sent", "failed")),
    )
    if pid is not None:
        q = q.filter(ContentItem.project_id == pid)
    for c in q.all():
        events.append({"kind": "content", "at": c.scheduled_at.isoformat(timespec="minutes"),
                       "item": cnt.item_json(c, slug(c.project_id), now, db)})
    aq = db.query(Automation).filter(Automation.enabled.is_(True))
    if pid is not None:
        aq = aq.filter(Automation.project_id == pid)
    for a in aq.all():
        p = projects.get(a.project_id)
        if p is None or not p.enabled:
            continue
        for i in range(days):
            d = (start + timedelta(days=i)).date()
            if not routines.scheduled_today(a, d) or (d == now.date() and (a.last_run_on == d or now.hour > a.hour)):
                continue
            events.append({"kind": "routine", "at": datetime(d.year, d.month, d.day, a.hour).isoformat(timespec="minutes"),
                           "routine": {"id": a.id, "project": p.slug, "agent": a.agent_slug, "title": a.title,
                                       "schedule": routines.describe_schedule(a), "auto_approve": a.auto_approve}})
    events.sort(key=lambda e: e["at"])
    unscheduled = db.query(ContentItem).filter(ContentItem.status == "approved", ContentItem.scheduled_at.is_(None))
    if pid is not None:
        unscheduled = unscheduled.filter(ContentItem.project_id == pid)
    return {"events": events[:500], "days": days, "start": start.date().isoformat(),
            "unscheduled": unscheduled.count()}


@app.get("/api/content/outputs")
def agent_outputs(project: str = "all", days: int = 14, agent: str = "",
                  auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """O que os agentes entregaram: tarefas concluídas ou que falharam, com o resultado e as peças que geraram."""
    days = max(1, min(days, 90))
    pid = _scope(db, project)
    since = utcnow() - timedelta(days=days)
    q = db.query(Task).filter(Task.status.in_(("done", "failed")),
                              func.coalesce(Task.finished_at, Task.created_at) >= since)
    if pid is not None:
        q = q.filter(Task.project_id == pid)
    if agent:
        q = q.filter(Task.agent_slug == agent)
    rows = q.order_by(func.coalesce(Task.finished_at, Task.created_at).desc(), Task.id.desc()).limit(200).all()
    ids = [t.id for t in rows]
    pieces: dict[int, list[dict]] = {}
    if ids:
        for c in db.query(ContentItem).filter(ContentItem.task_id.in_(ids)).all():
            pieces.setdefault(c.task_id, []).append({"id": c.id, "channel": c.channel, "title": c.title,
                                                     "stage": cnt.stage(c)})
    projects = _projects_by_id(db)
    links = _links(db, rows)
    out = []
    for t in rows:
        j = _task_json(t, projects, links)
        j["content"] = pieces.get(t.id, [])
        out.append(j)
    return {"outputs": out, "days": days}


# ---------- ativos do projeto (logo, artefatos, cartas, vídeos do site) que entram nas artes ----------

class AssetIn(BaseModel):
    project: str = Field(max_length=48)
    data: str = Field(default="", max_length=6_000_000)  # imagem (base64); ou
    link: str = Field(default="", max_length=1000)  # link de vídeo (https); ou
    text: str = Field(default="", max_length=4000)  # texto (briefing, regra da marca, frase fixa)
    name: str = Field(default="", max_length=80)
    category: str = Field(default="outro", max_length=20)
    note: str = Field(default="", max_length=500)  # o que é
    use_when: str = Field(default="", max_length=500)  # quando usar
    tags: str = Field(default="", max_length=600)  # palavras que ligam o ativo ao assunto ("tabuleiro, board, mapa")
    always: bool = False


class AssetEditIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)
    use_when: str | None = Field(default=None, max_length=500)
    tags: str | None = Field(default=None, max_length=600)
    always: bool | None = None
    link: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=4000)


@app.get("/api/assets")
def assets_list(project: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, project)
    return {"project": p.slug, "categories": [{"key": k, "name": v} for k, v in ast.CATEGORIES.items()],
            "assets": [ast.asset_json(a) for a in ast.project_assets(db, p.id)], "limit": ast.MAX_ASSETS}


@app.post("/api/assets")
def assets_add(body: AssetIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, body.project)
    if len(ast.project_assets(db, p.id)) >= ast.MAX_ASSETS:
        raise HTTPException(422, f"no máximo {ast.MAX_ASSETS} ativos por projeto: apague algum")
    if sum(map(bool, (body.data, body.link.strip(), body.text.strip()))) != 1:
        raise HTTPException(422, "mande uma imagem, um link de vídeo ou um texto")
    if body.text.strip():
        if not body.name.strip():
            raise HTTPException(422, "dê um nome ao texto (é o nome que você cita com @)")
        url, data, mime = "", body.text.strip().encode(), ast.TEXT_MIME
    elif body.link.strip():
        try:
            url = med.check_url(body.link)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        data, mime = b"", ast.LINK_MIME
    else:
        url = ""
        try:
            data, mime = lay.decode_image(body.data)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    a = LayoutAsset(project_id=p.id, kind="reference", mime=mime, data=data, url=url, name=body.name.strip(),
                    category=ast.norm_category(body.category, bool(url), mime == ast.TEXT_MIME), note=body.note.strip(),
                    use_when=body.use_when.strip(), tags=ast.split_tags(body.tags), always=0 if url or mime == ast.TEXT_MIME else int(body.always))
    db.add(a)
    db.commit()
    audit(db, auth.user.username, "asset_add", f"{p.slug} #{a.id} {a.category} {a.name}"[:500], client_ip(request))
    return ast.asset_json(a)


@app.patch("/api/assets/{aid}")
def assets_edit(aid: int, body: AssetEditIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    a = _asset_or_404(db, aid)
    if a.kind != "reference":
        raise HTTPException(409, "isto não é um ativo do projeto")
    link = ast.is_link(a) or ast.is_text(a)
    if body.text is not None and ast.is_text(a):
        if not body.text.strip():
            raise HTTPException(422, "o texto não pode ficar vazio")
        a.data = body.text.strip().encode()
    for field in ("name", "note", "use_when"):
        val = getattr(body, field)
        if val is not None:
            setattr(a, field, val.strip())
    if body.tags is not None:
        a.tags = ast.split_tags(body.tags)
    if body.category is not None:
        a.category = ast.norm_category(body.category, ast.is_link(a), ast.is_text(a))
    if body.always is not None:
        a.always = int(body.always and not link)
    if body.link is not None and ast.is_link(a):
        try:
            a.url = med.check_url(body.link)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    db.commit()
    audit(db, auth.user.username, "asset_edit", f"#{a.id} {a.name}"[:500], client_ip(request))
    return ast.asset_json(a)


# ---------- estúdio de layouts por projeto (referências + layouts gerados) ----------

class BriefIn(BaseModel):
    project: str = Field(max_length=48)
    brief: str = Field(default="", max_length=3000)


class ReferenceIn(BaseModel):
    project: str = Field(max_length=48)
    data: str = Field(max_length=6_000_000)  # base64 (aceita data:URL)
    note: str = Field(default="", max_length=300)


class GenerateIn(BaseModel):
    project: str = Field(max_length=48)
    format: str = Field(max_length=12)
    role: str = Field(default="", max_length=12)  # carrossel: capa | pagina | cta
    name: str = Field(default="", max_length=80)
    count: int = Field(default=2, ge=1, le=lay.MAX_PER_REQUEST)
    feedback: str = Field(default="", max_length=1000)


class LayoutDecideIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=300)


def _asset_or_404(db: DB, aid: int) -> LayoutAsset:
    a = db.get(LayoutAsset, aid)
    if a is None:
        raise HTTPException(404, "imagem não encontrada")
    return a


def _html_model(db: DB) -> dict:
    try:
        return {"model": lay.html_choice(db).model, "problem": ""}
    except ProviderError as e:
        return {"model": "", "problem": str(e)}


@app.get("/api/layouts")
def layouts_state(project: str, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, project)
    rows = (db.query(LayoutAsset).filter(LayoutAsset.project_id == p.id, LayoutAsset.status != "archived")
            .order_by(LayoutAsset.id.desc()).all())
    try:
        c = lay.image_choice(db)
        model, problem = c.model, ""
    except ProviderError as e:
        model, problem = "", str(e)
    return {
        "project": p.slug, "brief": p.style_brief or "",
        "formats": [{"key": k, "name": v[0], "ratio": v[1], "use": v[2],
                     "roles": [{"key": r, "name": lay.role_label(k, r)} for r in lay.roles_for(k)]}
                    for k, v in lay.FORMATS.items()],
        "positions": list(lay.TEXT_POSITIONS),
        "references": [{**lay.asset_json(a), **ast.asset_json(a)} for a in rows if a.kind == "reference" and ast.is_image(a)],
        "layouts": [lay.asset_json(a) for a in rows if a.kind == "layout"],
        "templates": [lay.asset_json(a) for a in rows if a.kind == "template"],
        "html_model": _html_model(db),
        "image_model": model, "problem": problem,
        "limits": {"references": lay.MAX_REFERENCES, "per_request": lay.MAX_PER_REQUEST},
    }


@app.get("/api/public/ref/{token}")
def public_reference(token: str):
    """Referência de imagem por link de uso único (10 min), para provedores que não aceitam data URL. Sem login: o token é o acesso."""
    item = lay.shared_image(token)
    if item is None:
        raise HTTPException(404, "link expirado")
    return Response(item[0], media_type=item[1], headers={"Cache-Control": "no-store"})


@app.get("/api/layouts/assets/{aid}")
def layout_asset_file(aid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    a = _asset_or_404(db, aid)
    headers = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"}
    rng = request.headers.get("range", "")
    if a.kind == "video" and rng:  # o <video> pede por pedaços (Range); sem isso o Safari não toca
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        size = len(a.data)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # "-N": os N últimos bytes
                start, end = max(size - int(m.group(2)), 0), size - 1
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return Response(a.data[start:end + 1], status_code=206, media_type=a.mime, headers=headers)
    return Response(a.data, media_type=a.mime, headers=headers)


@app.put("/api/layouts/brief")
def layouts_brief(body: BriefIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    p = _project_or_404(db, body.project)
    p.style_brief = body.brief.strip()
    db.commit()
    audit(db, auth.user.username, "layout_brief", p.slug, client_ip(request))
    return {"brief": p.style_brief}


@app.post("/api/layouts/references")
def layouts_add_reference(body: ReferenceIn, request: Request, auth: Auth = Depends(current_auth),
                          db: DB = Depends(get_db)):
    p = _project_or_404(db, body.project)
    if db.query(LayoutAsset).filter(LayoutAsset.project_id == p.id, LayoutAsset.kind == "reference").count() >= lay.MAX_REFERENCES:
        raise HTTPException(422, f"no máximo {lay.MAX_REFERENCES} referências por projeto: apague alguma")
    try:
        data, mime = lay.decode_image(body.data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    a = LayoutAsset(project_id=p.id, kind="reference", mime=mime, data=data, note=body.note.strip(), category="estilo")
    db.add(a)
    db.commit()
    audit(db, auth.user.username, "layout_reference_add", f"{p.slug} #{a.id}", client_ip(request))
    return lay.asset_json(a)


@app.post("/api/layouts/generate")
async def layouts_generate(body: GenerateIn, request: Request, auth: Auth = Depends(current_auth),
                           db: DB = Depends(get_db)):
    """Gera `count` layouts novos para o formato, com as referências do projeto. Um clique = no máximo 4 imagens."""
    p = _project_or_404(db, body.project)
    if body.format not in lay.FORMATS:
        raise HTTPException(422, "formato inválido")
    try:
        choice = lay.image_choice(db, log=True, project_id=p.id)
    except ProviderError as e:
        raise HTTPException(422, str(e)) from e
    refs_rows = lay.style_refs(db, p.id)[:lay.MAX_REFS_SENT]  # logo, paleta e o que for "sempre": não as cartas e objetos
    refs = [(a.data, a.mime) for a in refs_rows]
    role = lay.norm_role(body.format, body.role)
    prompt = lay.build_prompt(p.name, p.style_brief or "", body.format, body.feedback, [a.note for a in refs_rows], role)
    ratio = lay.FORMATS[body.format][1]
    made, error = [], ""
    for _ in range(body.count):
        try:
            with calllog.context("image", p.id, auth.user.username):
                data, mime = await lay.request_image(choice.model, prompt, refs, ratio)
        except ProviderError as e:
            error = str(e)
            break
        a = LayoutAsset(project_id=p.id, kind="layout", format=body.format, status="proposed", mime=mime, data=data,
                        note=body.feedback.strip(), prompt=prompt, model=choice.model, role=role, name=body.name.strip())
        db.add(a)
        db.commit()
        made.append(a)
    audit(db, auth.user.username, "layout_generate", f"{p.slug} {body.format} x{len(made)} {choice.model}", client_ip(request))
    if not made:
        raise HTTPException(502, error or "não saiu nenhuma imagem")
    return {"layouts": [lay.asset_json(a) for a in made], "error": error}


class TemplateIn(BaseModel):
    feedback: str = Field(default="", max_length=1000)
    simple: bool = False  # True = texto padrão, sem IA e sem custo
    position: str = Field(default="embaixo", max_length=10)  # embaixo | meio | topo (só no simples)


class LayoutUploadIn(BaseModel):
    project: str = Field(max_length=48)
    format: str = Field(max_length=12)
    role: str = Field(default="", max_length=12)
    name: str = Field(default="", max_length=80)
    data: str = Field(max_length=6_000_000)
    text: str = Field(default="embaixo", max_length=10)  # embaixo | meio | topo | ia (a IA define onde vai o texto)


class LayoutEditIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=12)


@app.post("/api/layouts/upload")
async def layouts_upload(body: LayoutUploadIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Arte do owner (sem texto) vira layout aprovado e já sai como template: texto padrão (sem IA) ou com a IA de HTML."""
    p = _project_or_404(db, body.project)
    if body.format not in lay.FORMATS:
        raise HTTPException(422, "formato inválido")
    try:
        data, mime = lay.decode_image(body.data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    role = lay.norm_role(body.format, body.role)
    a = LayoutAsset(project_id=p.id, kind="layout", format=body.format, status="approved", mime=mime, data=data,
                    model="upload", role=role, name=body.name.strip())
    db.add(a)
    db.commit()
    audit(db, auth.user.username, "layout_upload", f"{p.slug} {body.format}/{role or '-'} #{a.id}", client_ip(request))
    try:
        if body.text == "ia":
            t = await lay.generate_template(db, a)
        else:
            t = await asyncio.to_thread(lay.make_simple_template, db, a, body.text if body.text in lay.TEXT_POSITIONS else "embaixo")
            t.status = "approved"  # foi o owner que subiu e escolheu o texto padrão: já vale
            lay.ensure_default(db, t.project_id, t.format, t.role)
            db.commit()
    except ProviderError as e:
        return {"layout": lay.asset_json(a), "template": None, "error": str(e)}
    return {"layout": lay.asset_json(a), "template": lay.asset_json(t), "error": ""}


@app.patch("/api/layouts/{aid}")
def layouts_edit(aid: int, body: LayoutEditIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Renomeia ou muda o papel (capa, páginas, CTA) de um layout ou template."""
    a = _asset_or_404(db, aid)
    if a.kind not in ("layout", "template"):
        raise HTTPException(409, "só layouts e templates têm nome e papel")
    if body.name is not None:
        a.name = body.name.strip()
    if body.role is not None and a.format == "carrossel":
        new = lay.norm_role(a.format, body.role)
        if new != (a.role or "") and a.is_default:
            a.is_default = 0
        a.role = new
        if a.kind == "template":
            lay.ensure_default(db, a.project_id, a.format, a.role)
    db.commit()
    audit(db, auth.user.username, "layout_edit", f"#{a.id} {a.name!r} {a.role}", client_ip(request))
    return lay.asset_json(a)


@app.post("/api/layouts/{aid}/default")
def layouts_default(aid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """O template vira o padrão do formato (e papel): é o que as peças usam quando ninguém escolhe outro."""
    a = _asset_or_404(db, aid)
    if a.kind != "template" or a.status != "approved":
        raise HTTPException(409, "aprove o template antes de torná-lo padrão")
    lay.set_default(db, a)
    db.commit()
    audit(db, auth.user.username, "layout_default", f"#{a.id} {a.format}/{a.role or '-'}", client_ip(request))
    return lay.asset_json(a)


@app.post("/api/layouts/{aid}/template")
async def layouts_make_template(aid: int, body: TemplateIn, request: Request, auth: Auth = Depends(current_auth),
                                db: DB = Depends(get_db)):
    """Converte um layout APROVADO em template HTML (uma chamada de IA). O template precisa da sua aprovação para valer."""
    a = _asset_or_404(db, aid)
    if a.kind != "layout" or a.status != "approved":
        raise HTTPException(409, "aprove o layout antes de convertê-lo em HTML")
    try:
        if body.simple:
            t = await asyncio.to_thread(lay.make_simple_template, db, a,
                                        body.position if body.position in lay.TEXT_POSITIONS else "embaixo")
        else:
            t = await lay.generate_template(db, a, body.feedback)
    except ProviderError as e:
        raise HTTPException(422, str(e)) from e
    audit(db, auth.user.username, "layout_template", f"#{a.id} -> #{t.id} {a.format} {t.model}", client_ip(request))
    return lay.asset_json(t)


class BoxesIn(BaseModel):
    boxes: dict


@app.post("/api/layouts/{aid}/boxes")
async def layouts_boxes(aid: int, body: BoxesIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Editor de caixas: numa arte aprovada cria um template (já aprovado); num template, troca as caixas no lugar. Sem IA."""
    a = _asset_or_404(db, aid)
    if a.kind not in ("layout", "template"):
        raise HTTPException(409, "o editor é para artes e templates")
    if len(json.dumps(body.boxes)) > 4000:
        raise HTTPException(422, "caixas grandes demais")
    try:
        t = await asyncio.to_thread(lay.save_box_template, db, a, body.boxes)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except ProviderError as e:
        raise HTTPException(422, str(e)) from e
    audit(db, auth.user.username, "layout_boxes", f"#{a.id} -> #{t.id} {t.format}", client_ip(request))
    return lay.asset_json(t)


@app.post("/api/layouts/{aid}/decide")
def layouts_decide(aid: int, body: LayoutDecideIn, request: Request, auth: Auth = Depends(current_auth),
                   db: DB = Depends(get_db)):
    """Aprovar deixa o layout/template disponível (vários por formato); o primeiro template aprovado vira o padrão."""
    a = _asset_or_404(db, aid)
    if a.kind not in ("layout", "template") or a.status not in ("proposed", "rejected", "approved"):
        raise HTTPException(409, "este layout não está em votação")
    if body.approve:
        a.status = "approved"
        if a.kind == "template":
            lay.ensure_default(db, a.project_id, a.format, a.role or "")
    else:
        if a.status == "approved" and a.kind == "template" and a.is_default:
            raise HTTPException(409, "este é o template padrão: torne outro padrão antes de rejeitá-lo")
        a.status = "rejected"
        a.is_default = 0
        a.note = body.note.strip() or a.note
    db.commit()
    audit(db, auth.user.username, "layout_" + ("approve" if body.approve else "reject"), f"#{a.id} {a.format}", client_ip(request))
    return lay.asset_json(a)


@app.delete("/api/layouts/assets/{aid}")
def layouts_delete(aid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    a = _asset_or_404(db, aid)
    if a.kind in ("piece", "background", "video", "media"):
        raise HTTPException(409, "a imagem de uma peça se troca na Central de Conteúdo")
    if a.kind == "template" and a.is_default and len(lay.role_templates(db, a.project_id, a.format, a.role or "")) > 1:
        raise HTTPException(409, "este é o template padrão: torne outro padrão antes de apagar")
    fmt, role, pid = a.format, a.role or "", a.project_id
    if a.kind == "template":
        lay.delete_template(db, a)
    else:
        db.delete(a)
    db.commit()
    if a.kind == "template":
        lay.ensure_default(db, pid, fmt, role)
        db.commit()
    audit(db, auth.user.username, "layout_delete", f"#{aid}", client_ip(request))
    return {"ok": True}
