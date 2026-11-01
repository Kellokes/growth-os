import asyncio
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DB

from .agent import cmd_json, execute_run, run_agent, tool_message
from .audit import audit
from .config import get_settings
from .db import (
    Agent, AgentNote, AuditLog, Automation, CommandRun, ContentItem, Conversation, GrowthProject, Integration, Message, Metric,
    Mission, Action, Plan, ProjectAgent, Skill, Task, TaskLink, User, get_db, init_db, new_session, utcnow,
)
from .executor import classify
from . import approvals as appr
from . import content as cnt
from . import growth
from . import modelcfg
from . import routines
from . import projects as proj
from .providers import all_providers
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
    growth.runner.bind(asyncio.get_running_loop())
    db = new_session()
    try:
        growth.seed_defaults(db)
        growth.requeue_approved(db)
    finally:
        db.close()
    scheduler = asyncio.create_task(growth.scheduler_loop()) if get_settings().scheduler_enabled else None
    yield
    if scheduler:
        scheduler.cancel()


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


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "projects.html", "projects")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DB = Depends(get_db)):
    return _page(request, db, "settings.html", "models")


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
    return {**data, "models": modelcfg.for_kind(data["models"], kind)}


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
    err = modelcfg.check(provider, model, reasoning, modelcfg.kind_of(activity))
    if err:
        raise HTTPException(400, err)
    modelcfg.save(db, activity, provider, model, reasoning)
    audit(db, auth.user.username, "model_setting", f"{activity}: {provider or 'padrão'} {model} {reasoning}".strip(),
          client_ip(request))
    return {"ok": True}


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


def _stream(cid: int, provider, actor: str, ip: str) -> StreamingResponse:
    async def gen():
        db = new_session()  # sessão própria: a da dependência fecha antes do fim do stream
        try:
            async for event in run_agent(db, cid, provider, actor, ip):
                yield json.dumps(event, ensure_ascii=False) + "\n"
            growth.sync_task_for_conversation(db, cid)
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
        "mission_id": t.mission_id,
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
def decide_approvals(body: ApprovalDecideIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    """Aprova ou rejeita cartões de qualquer tipo. Ação externa de risco alto exige o código 2FA (consumido uma vez)."""
    ip = client_ip(request)
    seen: set[tuple[str, int]] = set()
    done, errors = [], []
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
                detail = f"#{c.id} {c.channel}"
            elif it.kind == "mission":
                m = db.get(Mission, it.ref_id)
                if m is None:
                    raise appr.ApprovalError("não encontrado", 404)
                appr.decide_mission(db, m, body.approve, body.note)
                detail = f"#{m.id} {m.title}"
            else:
                a = db.get(Action, it.ref_id)
                if a is None:
                    raise appr.ApprovalError("não encontrado", 404)
                if body.approve and a.risk == "alto" and a.status == "awaiting_approval" and totp_ok is None:
                    totp_ok = consume_totp(db, auth.user, body.totp)  # um código vale para o lote inteiro
                appr.decide_action(db, a, body.approve, bool(totp_ok), body.note)
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
    return {"decided": done, "errors": errors}


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
    frequency: str | None = Field(default=None, pattern=r"^(daily|weekly|monthly)$")
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
    for k in ("title", "steps", "frequency", "weekday", "monthday", "hour", "auto_approve", "enabled"):
        if data.get(k) is not None:
            setattr(a, k, data[k].strip() if isinstance(data[k], str) else data[k])
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
    if routines.has_open_run(db, a):
        raise HTTPException(409, "a última execução desta automação ainda está aberta no kanban")
    t = routines.create_run(db, a, approved=True, actor=auth.user.username)
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
    scheduled_at: str | None = Field(default=None, max_length=20)  # "AAAA-MM-DDTHH:MM" local; "" limpa


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
    return cnt.item_json(c, p.slug if p else "")


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
    items = [cnt.item_json(c, projects[c.project_id].slug if c.project_id in projects else "", now)
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
    _apply_content(c, body)
    db.add(c)
    db.commit()
    audit(db, auth.user.username, "content_create", f"#{c.id} {p.slug}/{c.channel}: {c.title or c.body[:60]}"[:500],
          client_ip(request))
    return _content_json(db, c)


def _apply_content(c: ContentItem, body: ContentIn) -> None:
    data = body.model_dump(exclude_unset=True)
    if "channel" in data and body.channel is not None:
        c.channel = cnt.norm_channel(body.channel)
    for field in ("title", "body", "recipient", "purpose", "media"):
        if data.get(field) is not None:
            setattr(c, field, str(data[field]).strip())
    if "scheduled_at" in data:
        raw = (body.scheduled_at or "").strip()
        when = cnt.parse_when(raw) if raw else None
        if raw and when is None:
            raise HTTPException(422, "data inválida (use AAAA-MM-DD HH:MM)")
        c.scheduled_at = when
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
    _apply_content(c, body)
    if c.status == "failed":
        c.status, c.note = "approved", ""
    db.commit()
    audit(db, auth.user.username, "content_edit", f"#{c.id}: {sorted(body.model_dump(exclude_unset=True))}", client_ip(request))
    return _content_json(db, c)


@app.post("/api/content/{cid}/decide")
def decide_content(cid: int, body: ContentDecideIn, request: Request,
                   auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    try:
        appr.decide_content(db, c, body.approve, body.note)
    except appr.ApprovalError as e:
        raise HTTPException(e.code, str(e)) from e
    audit(db, auth.user.username, "content_" + ("approve" if body.approve else "reject"), f"#{c.id} {c.channel}",
          client_ip(request))
    return _content_json(db, c)


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


@app.delete("/api/content/{cid}")
def delete_content(cid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = _content_or_404(db, cid)
    if c.status == "sent":
        raise HTTPException(409, "conteúdo enviado fica no histórico")
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
                       "item": cnt.item_json(c, slug(c.project_id), now)})
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
