"""Ações em integrações: o caminho que faz os agentes agirem de verdade, sem nunca verem a chave.

Fluxo: o agente roda `gos-int run <integração> <operação> '<json>'`; o CLI chama o endpoint interno do app com o
ticket da tarefa; aqui a operação é validada contra o projeto da tarefa e executada pelo conector.
  read / internal -> executa na hora e devolve o resultado
  external        -> vira um cartão em Aprovações; só executa depois da aprovação do owner
"""

import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session as DB

from . import approvals, growth, routines
from .audit import audit
from .connectors import REGISTRY, ConnectorError, Ctx
from .db import Action, GrowthProject, Integration, Task, utcnow


@dataclass(frozen=True)
class Ticket:
    task_id: int
    project_id: int
    agent_slug: str
    conversation_id: int


# Ticket por tarefa em execução: fica só na memória do app e morre quando a tarefa termina.
_TICKETS: dict[str, Ticket] = {}
_BY_CONVERSATION: dict[int, str] = {}


def issue_ticket(task: Task) -> str:
    token = secrets.token_urlsafe(24)
    _TICKETS[token] = Ticket(task.id, task.project_id, task.agent_slug, task.conversation_id or 0)
    _BY_CONVERSATION[task.conversation_id or 0] = token
    return token


def revoke_ticket(token: str | None) -> None:
    t = _TICKETS.pop(token or "", None)
    if t is not None:
        _BY_CONVERSATION.pop(t.conversation_id, None)


def ticket_for_conversation(cid: int | None) -> str | None:
    return _BY_CONVERSATION.get(cid or 0)


def lookup(token: str | None) -> Ticket | None:
    return _TICKETS.get(token or "")


def _ready(db: DB, project: GrowthProject) -> dict[str, dict]:
    return {i["kind"]: i for i in routines.integrations_for(db, project) if i["status"] == "ready" and i["enabled"]}


def catalog(db: DB, project: GrowthProject, agent_slug: str) -> list[dict]:
    """Integrações prontas deste projeto e as operações de cada uma (o que o agente enxerga)."""
    out = []
    for kind, info in _ready(db, project).items():
        conn = REGISTRY.get(kind)
        if conn is None:
            continue
        out.append({
            "integracao": kind, "nome": info["name"],
            "operacoes": [{"operacao": n, "efeito": op.effect, "sobre": op.about, "parametros": op.signature()}
                          for n, op in conn.ops.items()],
        })
    return out


def _fail(db: DB, project_id: int, task_id: int | None, kind: str, operation: str, effect: str, params: dict, error: str) -> Action:
    a = Action(project_id=project_id, task_id=task_id or None, integration=kind, operation=operation, effect=effect,
               params=json.dumps(params, ensure_ascii=False), status="failed", error=error[:500],
               idempotency_key=uuid.uuid4().hex, finished_at=utcnow(), attempts=1, risk="baixo")
    db.add(a)
    db.commit()
    approvals.emit(db, project_id, "action.failed", "action", a.id, {"integration": kind, "operation": operation})
    return a


async def run_operation(db: DB, ticket: Ticket, kind: str, operation: str, raw_params: dict,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Executa (ou pede aprovação para) uma operação em nome da tarefa do ticket. Sempre no projeto da tarefa."""
    project = db.get(GrowthProject, ticket.project_id)
    conn = REGISTRY.get(kind)
    if project is None or conn is None:
        return {"status": "error", "message": f"integração desconhecida: {kind}"}
    ready = _ready(db, project)
    if kind not in ready:
        return {"status": "error", "message": f"{kind} não está pronta neste projeto (ligue e configure em Automações > Integrações)"}
    op = conn.ops.get(operation)
    if op is None:
        return {"status": "error", "message": f"operação desconhecida: {kind}.{operation}. Disponíveis: {', '.join(conn.ops)}"}
    try:
        params = op.clean(raw_params)
    except ConnectorError as e:
        return {"status": "error", "message": str(e)}
    row = db.query(Integration).filter(Integration.project_id == project.id, Integration.kind == kind).one()
    ctx = Ctx(config=routines._config(row), secret=_secret(project, kind), transport=transport)
    title = f"{ready[kind]['name']}: {operation}"

    if op.effect == "external":
        # Nunca roda sozinha: cartão em Aprovações com os parâmetros exatos.
        a = Action(project_id=project.id, task_id=ticket.task_id or None, integration=kind, operation=operation, effect="external",
                   risk=op.risk, title=title, params=json.dumps(params, ensure_ascii=False), status="awaiting_approval",
                   idempotency_key=uuid.uuid4().hex)
        db.add(a)
        db.commit()
        approvals.emit(db, project.id, "action.requested", "action", a.id, {"integration": kind, "operation": operation})
        audit(db, f"agente:{ticket.agent_slug}", "action_requested", f"#{a.id} {kind}.{operation}", "interno")
        return {"status": "awaiting_approval", "action_id": a.id,
                "message": f"Pedido #{a.id} enviado para Aprovações. Não repita: continue o resto do trabalho e cite o pedido no resumo."}

    a = Action(project_id=project.id, task_id=ticket.task_id or None, integration=kind, operation=operation, effect=op.effect,
               risk="baixo", title=title, params=json.dumps(params, ensure_ascii=False), status="running",
               idempotency_key=uuid.uuid4().hex, attempts=1)
    db.add(a)
    db.commit()
    try:
        result = await conn.run(ctx, operation, params)
    except ConnectorError as e:
        a.status, a.error, a.finished_at = "failed", str(e)[:500], utcnow()
        db.commit()
        approvals.emit(db, project.id, "action.failed", "action", a.id, {"integration": kind, "operation": operation})
        return {"status": "error", "message": str(e)}
    a.status, a.finished_at = "done", utcnow()
    a.result = json.dumps(result, ensure_ascii=False)[:20000]
    db.commit()
    approvals.emit(db, project.id, "action.done", "action", a.id, {"integration": kind, "operation": operation})
    audit(db, f"agente:{ticket.agent_slug}", "action_done", f"#{a.id} {kind}.{operation}", "interno")
    return {"status": "done", "result": result}


def _secret(project: GrowthProject, kind: str) -> str:
    import os

    env = routines.secret_env_name(project, kind)
    return os.environ.get(env, "") if env else ""


# ---------- coleta de KPIs sem IA ----------

# integração -> (operação, {kpi: campo do resultado})
KPI_SOURCES = {
    "umami": ("ler_visitas", {"visitantes": "visitantes"}),
    "plausible": ("ler_visitas", {"visitantes": "visitantes"}),
    "uptime_kuma": ("ler_status", {"uptime": "uptime_24h_medio"}),
}


async def collect_kpis(db: DB, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Lê as integrações prontas de cada projeto e grava os KPIs do dia, sem gastar token de IA."""
    done = []
    for project in db.query(GrowthProject).filter(GrowthProject.enabled.is_(True)).all():
        for kind in _ready(db, project):
            src = KPI_SOURCES.get(kind)
            if src is None:
                continue
            operation, mapping = src
            ticket = Ticket(0, project.id, "coleta", 0)
            args = {"dias": 1} if kind in ("umami", "plausible") else {}
            res = await run_operation(db, ticket, kind, operation, args, transport)
            if res["status"] != "done":
                continue
            for kpi, field in mapping.items():
                value = res["result"].get(field)
                if isinstance(value, (int, float)):
                    try:
                        growth.record_point(db, project.id, kpi, float(value), growth.local_today(), source=f"integração:{kind}")
                        done.append(f"{project.slug}/{kpi}")
                    except KeyError:
                        pass  # o projeto não tem esse KPI
    return done


def prune(db: DB, days: int = 30) -> int:
    """Leituras antigas saem do histórico para a tabela não crescer sem fim (ações externas ficam)."""
    cut = utcnow() - timedelta(days=days)
    n = db.query(Action).filter(Action.effect == "read", Action.created_at < cut).delete()
    db.commit()
    return n
