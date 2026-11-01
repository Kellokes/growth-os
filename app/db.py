from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect,
    text as sql_text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret: Mapped[str] = mapped_column(String(64))
    # Última janela TOTP aceita: impede reutilizar o mesmo código (replay).
    last_totp_step: Mapped[int] = mapped_column(Integer, default=0)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginChallenge(Base):
    """Login em duas etapas: senha certa gera um desafio curto que só o código 2FA completa."""
    __tablename__ = "login_challenges"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="Nova thread")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    category: Mapped[str] = mapped_column(String(40), default="", server_default="")
    # "Excluir" no app só esconde: a conversa (e a auditoria dela) continua no banco.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Última escolha de modelo da thread (vazio = segue a configuração de modelos).
    provider: Mapped[str] = mapped_column(String(32), default="", server_default="")
    model: Mapped[str] = mapped_column(String(120), default="", server_default="")
    reasoning: Mapped[str] = mapped_column(String(16), default="", server_default="")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="", server_default="")
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CommandRun(Base):
    __tablename__ = "command_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    command: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(16))  # safe | approval | blocked
    status: Mapped[str] = mapped_column(String(16))  # pending | done | failed | rejected | blocked
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


# ---------- etapa 3: agentes, KPIs e plano diário ----------

class Agent(Base):
    """Catálogo das áreas (papéis). Cada projeto tem o seu time em ProjectAgent."""
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    area: Mapped[str] = mapped_column(String(64))
    icon: Mapped[str] = mapped_column(String(8), default="◆")
    mission: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GrowthProject(Base):
    """Um negócio do owner (ex.: takeone, zombie-party). Agentes, KPIs e tarefas são sempre de um projeto."""
    __tablename__ = "growth_projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    icon: Mapped[str] = mapped_column(String(8), default="◆")
    description: Mapped[str] = mapped_column(Text, default="")
    repo: Mapped[str] = mapped_column(String(120), default="")  # DONO/REPO no GitHub
    domains: Mapped[str] = mapped_column(Text, default="")  # separados por vírgula
    compose_project: Mapped[str] = mapped_column(String(64), default="")  # liga aos containers
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProjectAgent(Base):
    """O agente de uma área dentro de um projeto (time próprio, missão própria)."""
    __tablename__ = "project_agents"
    __table_args__ = (UniqueConstraint("project_id", "agent_slug", name="uq_project_agent"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    agent_slug: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mission: Mapped[str] = mapped_column(Text, default="")  # vazio = missão padrão da área
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="", server_default="")
    reasoning: Mapped[str] = mapped_column(String(16), default="", server_default="")


class ModelSetting(Base):
    """Provider, modelo e raciocínio de uma atividade (threads, agentes, imagens...). Ver app/modelcfg.py."""
    __tablename__ = "model_settings"
    activity: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    reasoning: Mapped[str] = mapped_column(String(16), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Metric(Base):
    __tablename__ = "kpis"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_project_kpi"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(80))
    area: Mapped[str] = mapped_column(String(32), default="growth")  # slug da área/agente dono
    unit: Mapped[str] = mapped_column(String(16), default="number")  # number | currency | percent
    direction: Mapped[str] = mapped_column(String(8), default="up")  # up | down (qual lado é bom)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")


class MetricPoint(Base):
    __tablename__ = "kpi_points"
    __table_args__ = (UniqueConstraint("metric_id", "day", name="uq_kpi_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("kpis.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(48), default="manual")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Plan(Base):
    __tablename__ = "daily_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="generating")  # generating | ready | failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("daily_plans.id"), nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    agent_slug: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    rationale: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(String(8), default="baixo")  # baixo | medio | alto
    kpi_key: Mapped[str] = mapped_column(String(48), default="")
    # proposed | approved | rejected | running | waiting (2FA) | done | failed
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Tarefa criada por uma automação (atividade rotineira); vazio = plano, pedido ou avulsa.
    automation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mission_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Por que está em "waiting": 2fa | acao | agente | horario (vazio = sem motivo registrado).
    wait_reason: Mapped[str] = mapped_column(String(12), default="", server_default="")



class TaskLink(Base):
    """Tarefa pedida por outro agente (handoff). Tabela à parte para não alterar agent_tasks."""
    __tablename__ = "task_links"
    task_id: Mapped[int] = mapped_column(ForeignKey("agent_tasks.id"), primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True, index=True)
    requested_by: Mapped[str] = mapped_column(String(32))  # slug do agente que pediu
    depth: Mapped[int] = mapped_column(Integer, default=1)  # 1 = pedido direto de uma tarefa do plano


class AgentNote(Base):
    """Recado de um agente para outro (ou para o time) dentro de um projeto."""
    __tablename__ = "agent_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    from_agent: Mapped[str] = mapped_column(String(32))
    to_agent: Mapped[str] = mapped_column(String(32), default="")  # vazio = time todo
    kind: Mapped[str] = mapped_column(String(16), default="nota")  # nota | pedido | resposta
    text: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------- habilidades, integrações e automações (sempre de um projeto) ----------

class Skill(Base):
    """Playbook reutilizável: instruções que os agentes escolhidos recebem quando planejam e executam."""
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    agents: Mapped[str] = mapped_column(String(400), default="")  # slugs separados por vírgula; vazio = time todo
    template: Mapped[str] = mapped_column(String(48), default="")  # chave do modelo da biblioteca, se veio de lá
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Integration(Base):
    """Ferramenta externa ligada a um projeto. Só guarda configuração NÃO secreta; a chave fica no .env do VPS."""
    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("project_id", "kind", name="uq_project_integration"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # chave do catálogo (umami, meta_graph...)
    config: Mapped[str] = mapped_column(Text, default="{}")  # JSON com os campos públicos
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Automation(Base):
    """Atividade rotineira: vira uma tarefa do agente no dia e hora marcados."""
    __tablename__ = "automations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    agent_slug: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    steps: Mapped[str] = mapped_column(Text, default="")
    skill_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frequency: Mapped[str] = mapped_column(String(8), default="daily")  # daily | weekly | monthly
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0 = segunda (weekly)
    monthday: Mapped[int] = mapped_column(Integer, default=1)  # 1..28 (monthly)
    hour: Mapped[int] = mapped_column(Integer, default=9)  # hora local do owner
    # False (padrão): cada execução entra como proposta e espera o owner aprovar.
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- central de conteúdo (sempre de um projeto) ----------

class ContentItem(Base):
    """Peça que vai sair para fora: post de rede social, e-mail de contato/parceria, artigo...

    Nasce de um bloco ```conteudo``` de um agente (ou do owner) e SEMPRE espera a aprovação do owner.
    Nada aqui é publicado ou enviado sozinho: até as integrações de envio existirem, o owner publica e
    marca como enviado.
    """
    __tablename__ = "content_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    agent_slug: Mapped[str] = mapped_column(String(32), default="")  # vazio = criado pelo owner
    task_id: Mapped[int | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(24), default="instagram")  # ver app/content.py CHANNELS
    title: Mapped[str] = mapped_column(String(200), default="")  # assunto do e-mail / título do post
    body: Mapped[str] = mapped_column(Text, default="")
    recipient: Mapped[str] = mapped_column(String(300), default="")  # e-mail: para quem; post: público
    purpose: Mapped[str] = mapped_column(String(60), default="")  # parceria, contato, lançamento...
    media: Mapped[str] = mapped_column(Text, default="")  # descrição da imagem/vídeo que acompanha
    # pending (aguardando aprovação) | approved | sent | rejected | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)  # hora local do owner
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    link: Mapped[str] = mapped_column(String(500), default="")  # onde saiu (URL do post), opcional
    note: Mapped[str] = mapped_column(Text, default="")  # motivo da rejeição / da falha
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- missões, aprovações, ações em integrações e eventos (sempre de um projeto) ----------

class Mission(Base):
    """Objetivo que envolve vários agentes. O Coordenador quebra em tarefas (fase de orquestração)."""
    __tablename__ = "missions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), default="chat")  # chat | plan | owner | event
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    lead_agent: Mapped[str] = mapped_column(String(32), default="coordenador")
    # draft | awaiting_approval | active | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="awaiting_approval", index=True)
    budget_tasks: Mapped[int] = mapped_column(Integer, default=12)
    budget_steps: Mapped[int] = mapped_column(Integer, default=80)
    used_tasks: Mapped[int] = mapped_column(Integer, default=0)
    used_steps: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Action(Base):
    """Uma chamada a uma integração. Efeito `external` só roda depois da aprovação do owner."""
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True, index=True)
    integration: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(48))
    effect: Mapped[str] = mapped_column(String(10), default="external")  # read | internal | external
    risk: Mapped[str] = mapped_column(String(8), default="alto")  # baixo | medio | alto (alto exige 2FA)
    title: Mapped[str] = mapped_column(String(200), default="")
    params: Mapped[str] = mapped_column(Text, default="{}")  # JSON já validado pelo esquema da operação
    # requested | awaiting_approval | approved | running | done | failed | rejected
    status: Mapped[str] = mapped_column(String(20), default="awaiting_approval", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    result: Mapped[str] = mapped_column(Text, default="")
    external_ref: Mapped[str] = mapped_column(String(300), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Approval(Base):
    """Registro de cada decisão do owner (tarefa, missão, conteúdo ou ação). A fila pendente é calculada ao vivo
    a partir das tabelas de origem (app/approvals.py), então esta tabela nunca fica fora de sincronia com elas."""
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # task | mission | content | action
    ref_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    requested_by: Mapped[str] = mapped_column(String(48), default="")  # agente:social | rotina:12 | chat:34 | owner
    risk: Mapped[str] = mapped_column(String(8), default="baixo")
    status: Mapped[str] = mapped_column(String(10), default="approved")  # approved | rejected | expired
    decided_via: Mapped[str] = mapped_column(String(10), default="clique")  # clique | 2fa | herdada | rotina
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Event(Base):
    """Linha do tempo do projeto e barramento que liga uma parte do sistema à outra."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("growth_projects.id"), index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)  # task.done, action.failed, content.sent...
    subject_kind: Mapped[str] = mapped_column(String(16), default="")
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON pequeno, nunca com segredos
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


_SessionLocal = None


# Colunas novas em tabelas que já existem (create_all só cria tabelas). Só ADD COLUMN, nunca DROP.
_NEW_COLUMNS = {
    "conversations": {
        "category": "VARCHAR(40) NOT NULL DEFAULT ''",
        "deleted_at": "TIMESTAMP NULL",
        "provider": "VARCHAR(32) NOT NULL DEFAULT ''",
        "model": "VARCHAR(120) NOT NULL DEFAULT ''",
        "reasoning": "VARCHAR(16) NOT NULL DEFAULT ''",
    },
    "messages": {"model": "VARCHAR(120) NOT NULL DEFAULT ''"},
    "project_agents": {
        "model": "VARCHAR(120) NOT NULL DEFAULT ''",
        "reasoning": "VARCHAR(16) NOT NULL DEFAULT ''",
    },
    "agent_tasks": {
        "automation_id": "INTEGER NULL",
        "mission_id": "INTEGER NULL",
        "wait_reason": "VARCHAR(12) NOT NULL DEFAULT ''",  # 2fa | acao | agente | horario
    },
}


def _add_missing_columns(engine):
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in _NEW_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db(url: str | None = None):
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("sqlite"):
        _engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        # pool_pre_ping descarta conexões que o Postgres fechou (restart, timeout).
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_db():
    if _SessionLocal is None:
        init_db()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_session():
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()
