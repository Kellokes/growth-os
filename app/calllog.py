"""Log das chamadas em segundo plano: quem foi chamado (provider e modelo), para quê, quanto demorou e o que deu.

Cada chamada vira uma linha em `ai_calls`, que o rodapé da interface lista (GET /api/calls). Só entram RESUMOS:
tamanho, contagens e um trecho curto já sem segredos nem dados pessoais; nunca o prompt inteiro, chaves ou tokens.
Registrar jamais pode quebrar a chamada de verdade: qualquer falha aqui é engolida.
"""
import contextvars
import re
import time
from contextlib import contextmanager
from datetime import timedelta

from .db import AiCall, new_session, utcnow

ACTIVITY_LABELS = {"piece_render": "Imagem da peça (template)", "video_render": "Renderização de vídeo"}
SNIPPET = 100
KEEP_DAYS = 30
STALE_MINUTES = 30  # "em andamento" há mais que isso = o servidor reiniciou no meio da chamada

_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("calllog_ctx", default=None)

_REDACT = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [oculto]"),
    (re.compile(r"\b(sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|AIza|or)[-_][A-Za-z0-9_\-]{12,}"), "[chave oculta]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|senha|password|authorization)\b\s*[:=]\s*\S+"), r"\1=[oculto]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[cpf]"),
    (re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)"), "[telefone]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}"), "[oculto]"),  # blobs longos (tokens, base64)
]


_current_call: contextvars.ContextVar["track | None"] = contextvars.ContextVar("calllog_call", default=None)


def note_usage(usage: object) -> None:
    """Soma o uso (`usage` da resposta do OpenRouter: tokens e custo em dólar) à chamada em andamento. Nunca levanta."""
    call = _current_call.get()
    if call is None or not isinstance(usage, dict):
        return
    try:
        pt, ct, cost = usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost")
        if isinstance(pt, (int, float)):
            call.input_tokens = (call.input_tokens or 0) + int(pt)
        if isinstance(ct, (int, float)):
            call.output_tokens = (call.output_tokens or 0) + int(ct)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            call.cost_usd = (call.cost_usd or 0.0) + float(cost)
    except Exception:  # noqa: BLE001
        pass


def clean(text: object, limit: int = SNIPPET) -> str:
    """Trecho curto em uma linha, sem segredos nem dados pessoais óbvios."""
    s = " ".join(str(text or "").split())
    for rx, repl in _REDACT:
        s = rx.sub(repl, s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def summarize_history(history) -> str:
    """'3 mensagens · última: "trecho"' a partir de uma lista de ChatTurn (nunca a conversa inteira)."""
    n = len(history)
    last = clean(history[-1].content) if n else ""
    return f"{n} mensagem(ns) · última: “{last}”" if last else f"{n} mensagem(ns)"


def summarize_text(text: str) -> str:
    return f"{len(text or '')} caracteres" + (f" · “{clean(text)}”" if (text or "").strip() else " (vazio)")


def summarize_bytes(kind: str, size: int) -> str:
    return f"{kind} · {size / 1024:.0f} KB"


@contextmanager
def context(activity: str, project_id: int | None = None, actor: str = ""):
    """Define atividade/projeto/autor para as chamadas feitas lá dentro (que não sabem de onde vieram)."""
    token = _ctx.set({"activity": activity, "project_id": project_id, "actor": actor})
    try:
        yield
    finally:
        _ctx.reset(token)


def current(key: str, default=None):
    return (_ctx.get() or {}).get(key, default)


def _write(fn) -> None:
    try:
        db = new_session()
        try:
            fn(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - o log nunca derruba a chamada
        pass


class track:
    """`with calllog.track(provider, model, activity, request=...) as call:` ... `call.done(response=...)`.

    Grava a linha ao começar (aparece "em andamento" no rodapé) e fecha ao sair: ok, ou erro com a mensagem da exceção.
    """

    def __init__(self, provider: str, model: str = "", activity: str | None = None, request: str = "",
                 project_id: int | None = None, actor: str | None = None):
        self.provider, self.model = provider, model or ""
        self.activity = activity or current("activity", "")
        self.project_id = project_id if project_id is not None else current("project_id")
        self.actor = actor if actor is not None else current("actor", "")
        self.request, self.response = clean(request, 400), ""
        self.id: int | None = None
        self._t0 = 0.0
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cost_usd: float | None = None
        self._token = None

    def done(self, response: str = "") -> None:
        self.response = clean(response, 400)

    def __enter__(self):
        self._t0 = time.monotonic()
        self._token = _current_call.set(self)

        def add(db):
            row = AiCall(activity=self.activity[:40], provider=self.provider[:32], model=self.model[:120],
                         project_id=self.project_id, actor=(self.actor or "")[:64], status="running",
                         request_summary=self.request)
            db.add(row)
            db.commit()
            self.id = row.id

        _write(add)
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = int((time.monotonic() - self._t0) * 1000)
        try:
            _current_call.reset(self._token)
        except (ValueError, TypeError):  # fechado em outro contexto (ex.: gerador assíncrono)
            _current_call.set(None)
        if exc_type is None:
            status, error = "ok", ""
        else:
            status = "error"
            error = "cancelada" if not issubclass(exc_type, Exception) else clean(exc or exc_type.__name__, 600)

        def close(db):
            row = db.get(AiCall, self.id) if self.id else None
            if row is None:
                return
            row.status, row.duration_ms, row.error = status, ms, error
            row.response_summary = self.response if status == "ok" else ""
            row.input_tokens, row.output_tokens, row.cost_usd = self.input_tokens, self.output_tokens, self.cost_usd
            db.commit()

        _write(close)
        return False


def record_failure(provider: str, model: str, activity: str, error: str, request: str = "",
                   project_id: int | None = None) -> None:
    """Chamada que nem chegou a sair (modelo mal configurado, chave ausente): fica no log com o motivo."""
    pid = project_id if project_id is not None else current("project_id")

    def add(db):
        db.add(AiCall(activity=activity[:40], provider=(provider or "—")[:32], model=(model or "")[:120], project_id=pid,
                      actor=(current("actor", "") or "")[:64], status="error", duration_ms=0,
                      request_summary=clean(request, 400), error=clean(error, 600)))
        db.commit()

    _write(add)


def sweep(startup: bool = False) -> None:
    """Fecha o que ficou 'em andamento' sem retorno e apaga o que passou de 30 dias.

    Na partida do servidor nada mais está rodando, então tudo que estava 'em andamento' foi interrompido.
    """
    def run(db):
        stale = utcnow() if startup else utcnow() - timedelta(minutes=STALE_MINUTES)
        db.query(AiCall).filter(AiCall.status == "running", AiCall.at < stale).update(
            {"status": "error", "error": "sem retorno (o servidor reiniciou durante a chamada?)"})
        if startup:
            db.query(AiCall).filter(AiCall.at < utcnow() - timedelta(days=KEEP_DAYS)).delete()
        db.commit()

    _write(run)


def call_json(r: AiCall) -> dict:
    return {
        "id": r.id, "at": r.at.isoformat(), "activity": r.activity, "provider": r.provider, "model": r.model,
        "project_id": r.project_id, "actor": r.actor, "status": r.status, "duration_ms": r.duration_ms,
        "request": r.request_summary, "response": r.response_summary, "error": r.error,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost_usd": r.cost_usd,
    }
