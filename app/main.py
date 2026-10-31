from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from .audit import audit
from .config import get_settings
from .db import AuditLog, CommandRun, Conversation, Message, get_db, init_db
from .executor import classify, run_command
from .providers import ChatTurn, ProviderError, all_providers
from .security import (
    SESSION_COOKIE, Auth, authenticate, client_ip, create_session, current_auth,
    consume_totp, destroy_session, lookup_session,
)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")



@asynccontextmanager
async def lifespan(_app: FastAPI):
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
        "messages": [{"id": m.id, "role": m.role, "provider": m.provider, "content": m.content} for m in msgs],
        "commands": [_cmd_json(r) for r in cmds],
    }


class ChatIn(BaseModel):
    provider: str
    content: str = Field(min_length=1, max_length=20000)


@app.post("/api/conversations/{cid}/messages")
async def send_message(
    cid: int, body: ChatIn, request: Request,
    auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    c = db.get(Conversation, cid)
    if c is None:
        raise HTTPException(404, "not_found")
    provider = all_providers().get(body.provider)
    if provider is None:
        raise HTTPException(400, "provider_desconhecido")
    ok, why = provider.available()
    if not ok:
        raise HTTPException(400, why)

    db.add(Message(conversation_id=cid, role="user", content=body.content))
    if c.title == "Nova conversa":
        c.title = body.content.strip().splitlines()[0][:80]
    db.commit()
    audit(db, auth.user.username, "chat", f"{provider.id}: {body.content[:200]}", client_ip(request))

    msgs = db.query(Message).filter(Message.conversation_id == cid, Message.role.in_(["user", "assistant"]))
    history = [ChatTurn(m.role, m.content) for m in msgs.order_by(Message.id).all()][-30:]
    try:
        reply = await provider.complete(history)
    except ProviderError as e:
        audit(db, auth.user.username, "chat_error", f"{provider.id}: {e}", client_ip(request))
        raise HTTPException(502, str(e))
    m = Message(conversation_id=cid, role="assistant", provider=provider.id, content=reply or "(sem resposta)")
    db.add(m)
    db.commit()
    return {"id": m.id, "role": m.role, "provider": m.provider, "content": m.content, "title": c.title}


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None


class ApproveIn(BaseModel):
    totp: str = Field(min_length=6, max_length=8)


def _cmd_json(r: CommandRun) -> dict:
    return {
        "id": r.id, "command": r.command, "risk": r.risk, "status": r.status,
        "exit_code": r.exit_code, "output": r.output,
    }


async def _execute(db: DB, run: CommandRun) -> None:
    s = get_settings()
    result = await run_in_threadpool(
        run_command, run.command, Path(s.workspace_dir), s.command_timeout_seconds, s.command_max_output_bytes
    )
    run.exit_code = result.exit_code
    run.output = result.output
    run.status = "done" if result.exit_code == 0 else "failed"
    db.commit()


@app.post("/api/commands")
async def submit_command(
    body: CommandIn, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db),
):
    cls = classify(body.command)
    status = {"safe": "running", "approval": "pending", "blocked": "blocked"}[cls.risk]
    run = CommandRun(
        conversation_id=body.conversation_id, command=body.command, risk=cls.risk, status=status,
        output=cls.reason if cls.risk == "blocked" else "",
    )
    db.add(run)
    db.commit()
    audit(db, auth.user.username, f"command_{cls.risk}", body.command, client_ip(request))
    if cls.risk == "safe":
        await _execute(db, run)
    return {**_cmd_json(run), "reason": cls.reason}


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
    await _execute(db, run)
    return _cmd_json(run)


@app.post("/api/commands/{rid}/reject")
def reject_command(rid: int, request: Request, auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    run = db.get(CommandRun, rid)
    if run is None or run.status != "pending":
        raise HTTPException(409, "comando não está pendente")
    run.status = "rejected"
    db.commit()
    audit(db, auth.user.username, "command_rejected", run.command, client_ip(request))
    return _cmd_json(run)


@app.get("/api/audit")
def audit_log(auth: Auth = Depends(current_auth), db: DB = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(200).all()
    return [
        {"at": r.at.isoformat(), "actor": r.actor, "action": r.action, "detail": r.detail, "ip": r.ip}
        for r in rows
    ]
