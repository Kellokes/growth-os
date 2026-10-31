import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DB

from .agent import cmd_json, execute_run, run_agent, tool_message
from .audit import audit
from .config import get_settings
from .db import AuditLog, CommandRun, Conversation, Message, get_db, init_db, new_session
from .executor import classify
from .providers import all_providers
from .security import (
    SESSION_COOKIE, Auth, authenticate, client_ip, create_session, current_auth,
    consume_totp, destroy_session, lookup_session,
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _protect_process_memory()
    init_db()
    yield


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
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp: str = Form(...),
    db: DB = Depends(get_db),
):
    ip = client_ip(request)
    user, reason = authenticate(db, username, password, totp)
    if user is None:
        audit(db, username[:64], "login_failed", reason, ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Usuário, senha ou código inválidos."}, status_code=401
        )
    token, _ = create_session(db, user)
    audit(db, user.username, "login_ok", "", ip)
    resp = RedirectResponse("/", status_code=303)
    s = get_settings()
    resp.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=s.cookie_secure, samesite="strict",
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: DB = Depends(get_db)):
    sess = lookup_session(db, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "chat.html", {"csrf": sess.csrf_token})


# ---------- API ----------

@app.get("/api/providers")
def providers(auth: Auth = Depends(current_auth)):
    out = []
    for p in all_providers().values():
        ok, why = p.available()
        out.append({"id": p.id, "label": p.label, "available": ok, "reason": why})
    return out


@app.get("/api/conversations")
def list_conversations(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    rows = db.query(Conversation).order_by(Conversation.id.desc()).limit(100).all()
    return [{"id": c.id, "title": c.title} for c in rows]


@app.post("/api/conversations")
def create_conversation(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = Conversation()
    db.add(c)
    db.commit()
    return {"id": c.id, "title": c.title}


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    c = db.get(Conversation, cid)
    if c is None:
        raise HTTPException(404, "not_found")
    msgs = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id).all()
    cmds = db.query(CommandRun).filter(CommandRun.conversation_id == cid).order_by(CommandRun.id).all()
    return {
        "id": c.id,
        "title": c.title,
        "messages": [
            {"id": m.id, "role": m.role, "provider": m.provider, "content": m.content}
            for m in msgs if m.role != "tool"
        ],
        "commands": [cmd_json(r) for r in cmds],
    }


class ChatIn(BaseModel):
    provider: str
    content: str = Field(min_length=1, max_length=20000)


class ContinueIn(BaseModel):
    provider: str


def _provider_or_400(pid: str):
    provider = all_providers().get(pid)
    if provider is None:
        raise HTTPException(400, "provider_desconhecido")
    ok, why = provider.available()
    if not ok:
        raise HTTPException(400, why)
    return provider


def _stream(cid: int, provider, actor: str, ip: str) -> StreamingResponse:
    async def gen():
        db = new_session()  # sessão própria: a da dependência fecha antes do fim do stream
        try:
            async for event in run_agent(db, cid, provider, actor, ip):
                yield json.dumps(event, ensure_ascii=False) + "\n"
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
    provider = _provider_or_400(body.provider)

    db.add(Message(conversation_id=cid, role="user", content=body.content))
    title_changed = c.title == "Nova conversa"
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
    """Retoma o laço depois que o owner aprovou ou rejeitou um comando."""
    if db.get(Conversation, cid) is None:
        raise HTTPException(404, "not_found")
    provider = _provider_or_400(body.provider)
    last = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id.desc()).first()
    if last is None or last.role != "tool":
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
