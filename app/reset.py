"""Resetar dados: apaga o histórico de atividades e as threads, mantém a configuração.

Apaga (CLEAR): threads e mensagens, comandos rodados, planos do dia, tarefas do Kanban e o que nasce delas
(pedidos entre agentes, recados, conteúdos, ações em integrações), missões, decisões de aprovação e eventos.

Mantém (KEEP): projetos, agentes (catálogo e time de cada projeto), habilidades, integrações, rotinas,
modelos, KPIs (definições e histórico de valores), usuário, sessões e a auditoria. Nada nessas tabelas é
alterado, nem `automations.last_run_on` (a rotina que já rodou hoje não roda de novo por causa do reset).

Tudo acontece numa transação só: ou apaga tudo da lista, ou não apaga nada.
"""
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session as DB

from .db import (
    Action, AgentNote, Approval, AuditLog, CommandRun, ContentItem, Conversation, Event, Message, Mission, Plan,
    Task, TaskLink,
)

CONFIRM_WORD = "RESETAR"

# Ordem de exclusão: filhos antes dos pais (o Postgres checa as chaves estrangeiras).
CLEAR = [
    (TaskLink, "Pedidos entre agentes"),
    (AgentNote, "Recados entre agentes"),
    (ContentItem, "Conteúdos (posts, e-mails)"),
    (Action, "Ações em integrações"),
    (Approval, "Histórico de aprovações"),
    (Event, "Eventos da linha do tempo"),
    (Task, "Tarefas do Kanban e do plano"),
    (Mission, "Missões"),
    (Plan, "Planos do dia"),
    (CommandRun, "Comandos executados"),
    (Message, "Mensagens das threads"),
    (Conversation, "Threads (inclusive as da lixeira)"),
]

# Só para mostrar na prévia. Qualquer tabela que não está em CLEAR é mantida.
KEEP_LABELS = {
    "growth_projects": "Projetos",
    "agents": "Agentes (catálogo de áreas)",
    "project_agents": "Agentes de cada projeto",
    "skills": "Habilidades",
    "integrations": "Integrações",
    "automations": "Rotinas",
    "model_settings": "Modelos",
    "kpis": "KPIs (definições)",
    "kpi_points": "KPIs (valores medidos)",
    "users": "Usuário",
    "sessions": "Sessões de login",
    "login_challenges": "Desafios de login",
    "audit_log": "Auditoria",
}

CLEAR_TABLES = {model.__tablename__ for model, _ in CLEAR}


class ResetError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Busy:
    tasks: int
    commands: int
    actions: int

    def __bool__(self) -> bool:
        return bool(self.tasks or self.commands or self.actions)


def _count(db: DB, model) -> int:
    return db.query(func.count()).select_from(model).scalar() or 0


def busy(db: DB) -> Busy:
    """Trabalho em andamento escreveria de volta em linhas apagadas: o reset espera ele terminar."""
    return Busy(
        tasks=db.query(func.count(Task.id)).filter(Task.status == "running").scalar() or 0,
        commands=db.query(func.count(CommandRun.id)).filter(CommandRun.status == "running").scalar() or 0,
        actions=db.query(func.count(Action.id)).filter(Action.status == "running").scalar() or 0,
    )


def preview(db: DB) -> dict:
    """Só conta. Nunca apaga nada."""
    from .db import Base

    tables = [t for t in Base.metadata.sorted_tables if t.name not in CLEAR_TABLES]
    order = list(KEEP_LABELS)
    tables.sort(key=lambda t: order.index(t.name) if t.name in order else len(order))
    keep = [
        {"table": t.name, "label": KEEP_LABELS.get(t.name, t.name),
         "count": db.execute(func.count().select().select_from(t)).scalar() or 0}
        for t in tables
    ]
    b = busy(db)
    return {
        "word": CONFIRM_WORD,
        "clear": [{"table": m.__tablename__, "label": label, "count": _count(db, m)} for m, label in CLEAR],
        "keep": keep,
        "busy": {"tasks": b.tasks, "commands": b.commands, "actions": b.actions, "items": running_items(db)} if b else None,
    }


def running_items(db: DB, limit: int = 10) -> list[dict]:
    """O que está rodando, para o owner saber o que espera (comando, tarefa ou ação)."""
    out = []
    for r in db.query(CommandRun).filter(CommandRun.status == "running").order_by(CommandRun.id).limit(limit):
        out.append({"kind": "comando", "id": r.id, "label": r.command[:160], "since": r.created_at.isoformat(),
                    "conversation_id": r.conversation_id})
    for t in db.query(Task).filter(Task.status == "running").order_by(Task.id).limit(limit):
        out.append({"kind": "tarefa", "id": t.id, "label": t.title[:160], "since": (t.decided_at or t.created_at).isoformat(),
                    "conversation_id": t.conversation_id})
    for a in db.query(Action).filter(Action.status == "running").order_by(Action.id).limit(limit):
        out.append({"kind": "ação", "id": a.id, "label": (a.title or f"{a.integration}.{a.operation}")[:160],
                    "since": a.created_at.isoformat(), "conversation_id": None})
    return out


def reset_data(db: DB, confirm: str, actor: str, ip: str = "") -> dict[str, int]:
    """Apaga as tabelas de CLEAR numa transação e registra na auditoria. Devolve quantas linhas saíram de cada uma.

    Recusa (ResetError) sem a palavra de confirmação exata ou com trabalho em andamento.
    """
    if (confirm or "").strip() != CONFIRM_WORD:
        raise ResetError(400, f"digite {CONFIRM_WORD} para confirmar")
    b = busy(db)
    if b:
        raise ResetError(409, "há tarefas, comandos ou ações rodando agora; espere terminar e tente de novo")
    deleted: dict[str, int] = {}
    try:
        for model, _label in CLEAR:
            deleted[model.__tablename__] = db.query(model).delete(synchronize_session=False)
        detail = ", ".join(f"{t}={n}" for t, n in deleted.items())
        db.add(AuditLog(actor=actor, action="data_reset", detail=detail[:4000], ip=ip))
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.expire_all()
    return deleted
