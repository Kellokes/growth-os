"""Etapa 3: agentes por área, KPIs e o plano da manhã — tudo POR PROJETO.

Cada projeto (negócio) do owner tem o seu time de agentes, os seus KPIs e as suas tarefas.

Fluxo diário:
  1. No horário configurado (ou pelo botão), cada agente ativo lê os KPIs e o que aconteceu ontem e
     propõe até 3 tarefas. Nada roda nessa fase: a resposta é só JSON.
  2. O owner aprova ou rejeita cada tarefa na tela "Plano do dia". Sem aprovação, nada acontece.
  3. Tarefas aprovadas rodam no laço normal da IA (mesma política de comandos: o perigoso pede 2FA),
     cada uma na sua conversa, uma por vez.
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session as DB

from .audit import audit
from .config import get_settings
from .db import (
    Action, Agent, AgentNote, CommandRun, Conversation, GrowthProject, Message, Metric, MetricPoint, Mission, Plan,
    ProjectAgent, Task, TaskLink, new_session, utcnow,
)
from .providers import ChatTurn, Provider, ProviderError, all_providers
from . import approvals as appr
from . import content
from . import missions
from . import modelcfg
from . import routines

log = logging.getLogger("growth")

MAX_TASKS_PER_AGENT = 3
MAX_HANDOFFS_PER_TASK = 3   # pedidos que uma tarefa pode abrir para outros agentes
MAX_HANDOFF_DEPTH = 3       # A pede a B, B pede a C, C pede a D: daí não passa (evita laço)
MAX_NOTES_PER_TASK = 5

COORDINATOR = "coordenador"
RESUME_MARK = "[RETOMADA]"

DEFAULT_AGENTS = [
    ("growth", "Growth", "Aquisição e conversão", "📈",
     "Fazer a base de usuários e a receita crescerem. Olha o funil inteiro (visita → cadastro → ativação → pagamento), "
     "acha o maior gargalo e propõe experimentos pequenos e mensuráveis para destravá-lo."),
    ("crm", "CRM", "Relacionamento e lifecycle", "🤝",
     "Cuidar dos leads e clientes ao longo do ciclo de vida: segmentação, réguas de e-mail/WhatsApp, reativação de "
     "inativos e upsell. Prioriza ações que aumentam conversão e reduzem churn."),
    ("marketing", "Marketing", "Mídia paga e campanhas", "📣",
     "Planejar e otimizar campanhas pagas e orgânicas, criativos e copy. Mede tudo por CAC e ROAS e corta o que não paga."),
    ("social", "Social", "Redes sociais e conteúdo", "💬",
     "Analisar o desempenho dos posts próprios e dos concorrentes, achar padrões que engajam e propor a pauta de "
     "conteúdo da semana, com ganchos e formatos que estão funcionando."),
    ("design", "Design", "Marca, UI e criativos", "🎨",
     "Cuidar da experiência e da marca: landing pages, telas do produto e criativos. Propõe melhorias de UI que "
     "aumentem conversão e consistência visual."),
    ("produto", "Produto", "Roadmap e uso do produto", "🧭",
     "Entender como os clientes usam o produto (logs, pedidos de funcionalidade, suporte) e transformar isso em "
     "melhorias priorizadas por impacto na ativação e na retenção."),
    ("tech", "Tech", "Engenharia e infraestrutura", "🛠",
     "Manter os projetos no ar, rápidos e seguros: monitora containers, erros e logs, corrige bugs via PR e "
     "propõe melhorias técnicas. Nunca mexe direto em produção nem no Caddy."),
    ("cs", "CS", "Sucesso do cliente", "🎧",
     "Garantir que os clientes tenham sucesso: acompanha tickets, NPS e sinais de risco, propõe respostas, "
     "artigos de ajuda e ações para reduzir churn."),
    ("financeiro", "Financeiro", "Receita, custos e caixa", "💰",
     "Acompanhar MRR, receita, custos de infraestrutura e de mídia, e o caixa. Aponta desperdícios e riscos e "
     "propõe ações para melhorar margem."),
    (COORDINATOR, "Coordenador", "Orquestração do time", "🧩",
     "Quebrar um objetivo em tarefas para os agentes do projeto, na ordem certa, acompanhar os resultados e "
     "garantir que o critério de pronto foi atingido. Não executa trabalho de área: delega ao agente certo."),
]

# key, nome, área, unidade, direção
DEFAULT_METRICS = [
    ("visitantes", "Visitantes", "growth", "number", "up"),
    ("cadastros", "Cadastros", "growth", "number", "up"),
    ("conversao_cadastro", "Conversão visita → cadastro", "growth", "percent", "up"),
    ("cac", "CAC", "marketing", "currency", "down"),
    ("roas", "ROAS", "marketing", "number", "up"),
    ("gasto_ads", "Investimento em mídia", "marketing", "currency", "down"),
    ("leads_ativos", "Leads ativos", "crm", "number", "up"),
    ("seguidores", "Seguidores", "social", "number", "up"),
    ("engajamento", "Engajamento", "social", "percent", "up"),
    ("usuarios_ativos", "Usuários ativos", "produto", "number", "up"),
    ("retencao_d30", "Retenção D30", "produto", "percent", "up"),
    ("uptime", "Uptime dos sites", "tech", "percent", "up"),
    ("tickets_abertos", "Tickets abertos", "cs", "number", "down"),
    ("nps", "NPS", "cs", "number", "up"),
    ("mrr", "MRR", "financeiro", "currency", "up"),
    ("receita", "Receita do mês", "financeiro", "currency", "up"),
    ("churn", "Churn", "financeiro", "percent", "down"),
]


def seed_defaults(db: DB) -> None:
    """Catálogo de áreas (papéis). Não altera o que já existe."""
    have = {a.slug for a in db.query(Agent.slug).all()}
    for slug, name, area, icon, mission in DEFAULT_AGENTS:
        if slug not in have:
            db.add(Agent(slug=slug, name=name, area=area, icon=icon, mission=mission))
    db.commit()
    for project in db.query(GrowthProject).all():
        setup_project(db, project)


def setup_project(db: DB, project: GrowthProject) -> None:
    """Garante o time completo e os KPIs padrão do projeto (só cria o que falta)."""
    team = {pa.agent_slug for pa in db.query(ProjectAgent).filter(ProjectAgent.project_id == project.id).all()}
    for slug, *_ in DEFAULT_AGENTS:
        if slug not in team:
            db.add(ProjectAgent(project_id=project.id, agent_slug=slug))
    keys = {m.key for m in db.query(Metric.key).filter(Metric.project_id == project.id).all()}
    for key, name, area, unit, direction in DEFAULT_METRICS:
        if key not in keys:
            db.add(Metric(project_id=project.id, key=key, name=name, area=area, unit=unit, direction=direction))
    db.commit()


def slugify(text: str) -> str:
    import unicodedata

    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return s[:48] or "projeto"


def create_project(db: DB, name: str, **fields) -> GrowthProject:
    slug = fields.pop("slug", None) or slugify(name)
    project = GrowthProject(slug=slug, name=name.strip()[:80], **fields)
    db.add(project)
    db.commit()
    setup_project(db, project)
    return project


def import_projects(db: DB, containers: list[dict], sites: list[dict]) -> list[str]:
    """Cria um projeto para cada projeto do compose no VPS que ainda não está cadastrado."""
    from .projects import group_projects

    known = {p.compose_project for p in db.query(GrowthProject).all()} | {p.slug for p in db.query(GrowthProject).all()}
    created = []
    for group in group_projects(containers, sites):
        name = group["name"]
        if name in ("avulsos", "_caddy", "growth-os") or name in known:
            continue
        domains = sorted({h for s in group["sites"] for h in s["hosts"]})
        create_project(db, name.replace("-", " ").title(), slug=slugify(name), compose_project=name,
                       domains=", ".join(domains))
        created.append(name)
    return created


def get_project(db: DB, slug: str | None) -> GrowthProject | None:
    q = db.query(GrowthProject)
    if slug:
        return q.filter(GrowthProject.slug == slug).one_or_none()
    return q.order_by(GrowthProject.id).first()


def team(db: DB, project: GrowthProject) -> list[tuple[Agent, ProjectAgent]]:
    agents = {a.slug: a for a in db.query(Agent).all()}
    rows = db.query(ProjectAgent).filter(ProjectAgent.project_id == project.id).order_by(ProjectAgent.id).all()
    return [(agents[pa.agent_slug], pa) for pa in rows if pa.agent_slug in agents]


def mission(agent: Agent, pa: ProjectAgent | None) -> str:
    return (pa.mission if pa and pa.mission.strip() else agent.mission).strip()


def local_today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


# ---------- KPIs ----------

def record_point(db: DB, project_id: int, key: str, value: float, day: date | None = None,
                 source: str = "manual", note: str = "") -> MetricPoint:
    metric = db.query(Metric).filter(Metric.project_id == project_id, Metric.key == key).one_or_none()
    if metric is None:
        raise KeyError(key)
    day = day or local_today()
    point = db.query(MetricPoint).filter(MetricPoint.metric_id == metric.id, MetricPoint.day == day).one_or_none()
    if point is None:
        point = MetricPoint(metric_id=metric.id, day=day, value=value, source=source, note=note)
        db.add(point)
    else:
        point.value, point.source, point.note = value, source, note
    db.commit()
    return point


def metric_series(db: DB, project_id: int, days: int = 90) -> list[dict]:
    since = local_today() - timedelta(days=days)
    metrics = db.query(Metric).filter(Metric.project_id == project_id).order_by(Metric.area, Metric.id).all()
    ids = [m.id for m in metrics]
    points = (
        db.query(MetricPoint).filter(MetricPoint.metric_id.in_(ids), MetricPoint.day >= since)
        .order_by(MetricPoint.day).all() if ids else []
    )
    by_metric: dict[int, list[MetricPoint]] = {}
    for p in points:
        by_metric.setdefault(p.metric_id, []).append(p)
    out = []
    for m in metrics:
        pts = by_metric.get(m.id, [])
        out.append({
            "key": m.key, "name": m.name, "area": m.area, "unit": m.unit, "direction": m.direction,
            "target": m.target, "description": m.description,
            "points": [{"day": p.day.isoformat(), "value": p.value, "source": p.source} for p in pts],
            "last": pts[-1].value if pts else None, "prev": pts[-2].value if len(pts) > 1 else None,
        })
    return out


def kpi_snapshot(db: DB, project_id: int, days: int = 30) -> str:
    """Texto curto com os KPIs do projeto para os agentes lerem."""
    lines = []
    for m in metric_series(db, project_id, days):
        if not m["points"]:
            lines.append(f"- {m['key']} ({m['name']}, área {m['area']}): SEM DADOS")
            continue
        vals = [p["value"] for p in m["points"]]
        first, last = vals[0], vals[-1]
        trend = "" if first == 0 else f", variação no período {((last - first) / abs(first)) * 100:+.1f}%"
        target = f", meta {m['target']}" if m["target"] is not None else ""
        lines.append(
            f"- {m['key']} ({m['name']}, área {m['area']}, {m['unit']}, bom é {'subir' if m['direction'] == 'up' else 'cair'}): "
            f"último {last:g} em {m['points'][-1]['day']}{trend}{target}"
        )
    return "\n".join(lines)


_KPI_BLOCK = re.compile(r"```kpi[ \t]*\n(.*?)```", re.S)


def ingest_kpi_blocks(db: DB, project_id: int, text: str, source: str) -> list[str]:
    """Agentes reportam números num bloco ```kpi {"chave": valor}```. Só chaves do próprio projeto."""
    saved = []
    for block in _KPI_BLOCK.findall(text or ""):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            try:
                record_point(db, project_id, str(key), float(value), source=source)
                saved.append(str(key))
            except (KeyError, TypeError, ValueError):
                continue
    return saved


# ---------- providers ----------

def pick_provider(pa: ProjectAgent | None = None, activity: str = "agents_run", db: DB | None = None) -> Provider:
    """Escolha do agente > configuração de modelos da atividade > padrão > GOS_AGENT_PROVIDER > qualquer um."""
    providers = all_providers()
    choices = []
    if pa is not None and pa.provider:
        choices.append(modelcfg.Choice(pa.provider, pa.model or "", pa.reasoning or ""))
    if db is not None:
        choices.append(modelcfg.resolve(db, activity))
    choices.append(modelcfg.Choice(get_settings().agent_provider))
    for c in choices:
        p = providers.get(c.provider or "")
        if p and p.available()[0]:
            return p.configured(c.model, c.reasoning)
    for p in providers.values():
        if p.available()[0]:
            return p
    raise ProviderError("nenhum provider disponível para os agentes")


# ---------- plano da manhã ----------

def _recent_report(db: DB, project_id: int, slug: str) -> str:
    since = utcnow() - timedelta(days=3)
    rows = (
        db.query(Task).filter(Task.project_id == project_id, Task.agent_slug == slug, Task.created_at >= since)
        .order_by(Task.id.desc()).limit(8).all()
    )
    if not rows:
        return "nenhuma tarefa recente"
    return "\n".join(f"- [{t.status}] {t.title}" + (f" → {t.result[:200]}" if t.result else "") for t in rows)


def project_context(project: GrowthProject) -> str:
    parts = [f"Projeto: **{project.name}**"]
    if project.description:
        parts.append(f"Sobre o projeto: {project.description}")
    if project.domains:
        parts.append(f"Domínios: {project.domains}")
    if project.repo:
        parts.append(f"Repositório: github.com/{project.repo}")
    if project.compose_project:
        parts.append(f"Containers no VPS: projeto compose \"{project.compose_project}\" (gos-docker ps {project.compose_project})")
    return "\n".join(parts)


def planning_prompt(db: DB, project: GrowthProject, agent: Agent, pa: ProjectAgent, today: date) -> str:
    return f"""MODO PLANEJAMENTO. Nesta mensagem NÃO escreva blocos ```bash e não rode nada.

Você é o agente **{agent.name}** ({agent.area}) do projeto **{project.name}**. Você só cuida deste projeto.
Missão: {mission(agent, pa)}

{project_context(project)}

Hoje é {today.isoformat()}.

KPIs do projeto {project.name} (últimos 30 dias):
{kpi_snapshot(db, project.id)}

Suas tarefas recentes neste projeto:
{_recent_report(db, project.id, agent.slug)}

Seu time neste projeto:
{team_roster(db, project, exclude=agent.slug)}

Recados e pedidos recentes para você (leve em conta ao planejar):
{notes_for(db, project.id, agent.slug)}

{_toolbox_section(db, project, agent.slug)}
Proponha de 1 a {MAX_TASKS_PER_AGENT} tarefas para HOJE, concretas e de alto impacto na sua área, só para este projeto.
Cada tarefa precisa ser executável por você com o que o Growth OS oferece (shell no /workspace, gos-docker,
git e gos-pr no repositório do projeto, internet), ou preparar algo para o owner decidir.
Se faltam dados nos KPIs da sua área, uma boa tarefa é descobrir como obtê-los.
Não repita tarefas já concluídas. Risco "alto" é para o que mexe em produção.

Responda SOMENTE com JSON válido, sem texto antes ou depois:
{{"tarefas": [{{"titulo": "...", "porque": "impacto esperado e KPI", "passos": "passo 1; passo 2; ...", "risco": "baixo|medio|alto", "kpi": "chave do KPI ou vazio"}}]}}"""


def _toolbox_section(db: DB, project: GrowthProject, slug: str) -> str:
    box = routines.toolbox(db, project, slug, full=False)
    return box + "\n" if box else ""


def parse_tasks(text: str) -> list[dict]:
    """Extrai a lista de tarefas mesmo com texto ou ```json em volta."""
    text = (text or "").strip()
    candidates = [text]
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if m:
        candidates.insert(0, m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        items = data.get("tarefas") if isinstance(data, dict) else data if isinstance(data, list) else None
        if not isinstance(items, list):
            continue
        out = []
        for it in items[:MAX_TASKS_PER_AGENT]:
            if not isinstance(it, dict) or not str(it.get("titulo", "")).strip():
                continue
            risk = str(it.get("risco", "baixo")).lower().replace("é", "e")
            out.append({
                "title": str(it["titulo"]).strip()[:200],
                "rationale": str(it.get("porque", "")).strip()[:2000],
                "steps": str(it.get("passos", "")).strip()[:4000],
                "risk": risk if risk in ("baixo", "medio", "alto") else "medio",
                "kpi": str(it.get("kpi", "") or "").strip()[:48],
            })
        return out
    return []


async def plan_for_agent(db: DB, project: GrowthProject, agent: Agent, pa: ProjectAgent, plan: Plan, today: date) -> int:
    provider = pick_provider(pa, "agents_plan", db)
    reply = await provider.complete([ChatTurn("user", planning_prompt(db, project, agent, pa, today))])
    tasks = parse_tasks(reply)
    for t in tasks:
        db.add(Task(plan_id=plan.id, project_id=project.id, agent_slug=agent.slug, title=t["title"],
                    rationale=t["rationale"], steps=t["steps"], risk=t["risk"], kpi_key=t["kpi"]))
    db.commit()
    return len(tasks)


_plan_lock = asyncio.Lock()


async def generate_plan(today: date | None = None, actor: str = "agendador") -> int:
    """Gera (ou refaz, se falhou) o plano do dia. Retorna o id do plano."""
    async with _plan_lock:
        db = new_session()
        try:
            today = today or local_today()
            seed_defaults(db)
            plan = db.query(Plan).filter(Plan.day == today).one_or_none()
            fresh = plan is not None and plan.created_at > utcnow() - timedelta(minutes=30)
            if plan and (plan.status == "ready" or (plan.status == "generating" and fresh)):
                return plan.id
            if plan is None:
                plan = Plan(day=today)
                db.add(plan)
            plan.status, plan.error, plan.created_at = "generating", "", utcnow()
            db.commit()
            audit(db, actor, "plan_generate", today.isoformat())

            jobs = [
                (project.id, agent.id, pa.id, f"{project.name} · {agent.name}")
                for project in db.query(GrowthProject).filter(GrowthProject.enabled.is_(True)).order_by(GrowthProject.id)
                for agent, pa in team(db, project) if pa.enabled and agent.slug != COORDINATOR
            ]
            sem = asyncio.Semaphore(max(1, get_settings().agent_concurrency))
            errors: list[str] = []

            async def one(project_id: int, agent_id: int, pa_id: int, label: str):
                async with sem:
                    s = new_session()  # cada agente com sua sessão: rodam em paralelo
                    try:
                        await plan_for_agent(s, s.get(GrowthProject, project_id), s.get(Agent, agent_id),
                                             s.get(ProjectAgent, pa_id), s.get(Plan, plan.id), today)
                    except Exception as e:  # noqa: BLE001 - um agente com erro não derruba o plano
                        errors.append(f"{label}: {e}"[:300])
                    finally:
                        s.close()

            await asyncio.gather(*(one(*j) for j in jobs))
            db.refresh(plan)
            count = db.query(Task).filter(Task.plan_id == plan.id).count()
            plan.status = "ready" if count or not errors else "failed"
            if not jobs:
                plan.status, errors = "failed", ["nenhum projeto com agentes ativos"]
            plan.error = "\n".join(errors)
            db.commit()
            if plan.status == "ready":
                projects = len({j[0] for j in jobs})
                await notify(f"☀️ Plano de hoje pronto: {count} tarefas em {projects} projetos esperando sua aprovação."
                             + (f"\n{get_settings().public_url.rstrip('/')}/approvals" if get_settings().public_url else ""))
            return plan.id
        finally:
            db.close()



# ---------- colaboração entre agentes (mesmo projeto) ----------

_HANDOFF = re.compile(r"```(handoff|pedido)[ \t]*\n(.*?)```", re.S)
_NOTE = re.compile(r"```(recado|nota)[ \t]*\n(.*?)```", re.S)


def _json_items(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    return [i for i in items if isinstance(i, dict)]


def team_roster(db: DB, project: GrowthProject, exclude: str = "") -> str:
    return "\n".join(
        f"- {a.slug}: {a.name} ({a.area}){'' if pa.enabled else ' [desligado]'}"
        for a, pa in team(db, project) if a.slug != exclude
    )


def notes_for(db: DB, project_id: int, slug: str, limit: int = 8) -> str:
    since = utcnow() - timedelta(days=7)
    rows = (
        db.query(AgentNote).filter(
            AgentNote.project_id == project_id, AgentNote.created_at >= since,
            AgentNote.from_agent != slug, (AgentNote.to_agent == slug) | (AgentNote.to_agent == ""),
        ).order_by(AgentNote.id.desc()).limit(limit).all()
    )
    if not rows:
        return "nenhum recado"
    return "\n".join(f"- [{n.kind}] de {n.from_agent}: {n.text[:400]}" for n in rows)


def collaboration_guide(db: DB, project: GrowthProject, slug: str) -> str:
    return f"""TRABALHO EM TIME (só com agentes deste projeto)
Seu time em {project.name}:
{team_roster(db, project, exclude=slug)}

Recados recentes para você:
{notes_for(db, project.id, slug)}

Para pedir uma tarefa a outro agente (ela vai para a aprovação do owner), use no fim da resposta:
```pedido
{{"para": "slug_do_agente", "titulo": "o que ele deve fazer", "detalhes": "contexto e o que você precisa de volta", "risco": "baixo|medio|alto"}}
```
Para deixar um recado (informação útil, sem tarefa), use:
```recado
{{"para": "slug_do_agente ou vazio para o time", "texto": "..."}}
```
Quando a tarefa pedida terminar, você recebe o resultado como recado. Peça só o que for da área do outro."""


def process_collaboration(db: DB, task: Task, text: str) -> dict:
    """Lê os blocos ```pedido``` e ```recado``` da conversa de uma tarefa e cria pedidos/recados no MESMO projeto."""
    team_slugs = {
        pa.agent_slug for pa in db.query(ProjectAgent).filter(ProjectAgent.project_id == task.project_id).all()
    }
    link = db.get(TaskLink, task.id)
    depth = (link.depth if link else 0) + 1
    mission = db.get(Mission, task.mission_id) if task.mission_id else None
    inherit = mission is not None and mission.status == "active"
    created, notes, refused, approved = [], 0, [], []
    for _, raw in _HANDOFF.findall(text or ""):
        for item in _json_items(raw):
            to = str(item.get("para", "")).strip().lower()
            title = str(item.get("titulo", "")).strip()[:200]
            if not title or to not in team_slugs or to == task.agent_slug:
                refused.append(f"pedido inválido para '{to}'")
                continue
            if depth > MAX_HANDOFF_DEPTH:
                refused.append(f"cadeia de pedidos passou de {MAX_HANDOFF_DEPTH}")
                continue
            if len(created) >= MAX_HANDOFFS_PER_TASK:
                refused.append(f"mais de {MAX_HANDOFFS_PER_TASK} pedidos numa tarefa")
                continue
            risk = str(item.get("risco", "baixo")).lower().replace("é", "e")
            risk = risk if risk in ("baixo", "medio", "alto") else "medio"
            child = Task(
                project_id=task.project_id, agent_slug=to, title=title, status="proposed", mission_id=task.mission_id,
                rationale=f"Pedido de {task.agent_slug} (tarefa #{task.id}: {task.title[:120]})",
                steps=str(item.get("detalhes", "")).strip()[:4000],
                risk=risk,
            )
            db.add(child)
            db.flush()
            db.add(TaskLink(task_id=child.id, parent_id=task.id, requested_by=task.agent_slug, depth=depth,
                            wait=1 if item.get("esperar") is True else 0))
            db.add(AgentNote(project_id=task.project_id, from_agent=task.agent_slug, to_agent=to, kind="pedido",
                             text=title, task_id=child.id))
            created.append(child)
            if inherit and risk == "baixo":  # dentro de uma missão aprovada, pedido de risco baixo não pede de novo
                child.status, child.decided_at = "approved", utcnow()
                approved.append(child)
    for _, raw in _NOTE.findall(text or ""):
        for item in _json_items(raw):
            to = str(item.get("para", "") or "").strip().lower()
            body = str(item.get("texto", "")).strip()[:2000]
            if not body or (to and to not in team_slugs) or notes >= MAX_NOTES_PER_TASK:
                continue
            db.add(AgentNote(project_id=task.project_id, from_agent=task.agent_slug, to_agent=to, text=body,
                             task_id=task.id))
            notes += 1
    db.commit()
    for child in approved:
        appr.record_inherited(db, child)
    return {"tasks": created, "notes": notes, "refused": refused, "approved": [c.id for c in approved]}


def reply_to_requester(db: DB, task: Task, note: bool = True) -> list[int]:
    """Fecha o ciclo: quando uma tarefa pedida por outro agente termina, quem pediu recebe o resultado.

    Se quem pediu estava esperando (`esperar`) e esta era a última resposta que faltava, ele volta para a fila."""
    link = db.get(TaskLink, task.id)
    if link is None:
        return []
    if note:
        status = "concluiu" if task.status == "done" else "não conseguiu concluir"
        summary = re.sub(r"```.*?```", "", task.result or "", flags=re.S).strip()[:1200]
        db.add(AgentNote(project_id=task.project_id, from_agent=task.agent_slug, to_agent=link.requested_by,
                         kind="resposta", text=f"{status} \"{task.title}\": {summary}", task_id=task.id))
        db.commit()
    parent = db.get(Task, link.parent_id) if link.wait and link.parent_id else None
    if parent is None or parent.status != "waiting" or parent.wait_reason != "agente":
        return []
    still = (
        db.query(Task).join(TaskLink, TaskLink.task_id == Task.id)
        .filter(TaskLink.parent_id == parent.id, TaskLink.wait == 1, Task.status.notin_(missions.TERMINAL)).count()
    )
    if still:
        return []
    parent.status, parent.wait_reason = "approved", ""
    db.commit()
    return [parent.id]


def _fire(text: str) -> None:
    """Aviso no Telegram vindo de código síncrono (ou de uma thread do pool): agenda no loop do app, se houver."""
    try:
        asyncio.get_running_loop().create_task(notify(text))
    except RuntimeError:
        if runner.loop is not None:
            runner.loop.call_soon_threadsafe(lambda: runner.loop.create_task(notify(text)))


def _submit(ids: list[int]) -> None:
    for tid in ids:
        runner.submit(tid)


def finish(db: DB, task: Task, notify_requester: bool = True) -> list[int]:
    """Uma tarefa chegou ao fim: avisa quem pediu, libera as dependentes, fecha a missão e põe na fila o que destravou."""
    ids = reply_to_requester(db, task, note=notify_requester)
    more, notices = missions.settle(db, task)
    ids += more
    _submit(ids)
    for text in notices:
        _fire(text)
    return ids


def action_settled(db: DB, action: Action) -> list[int]:
    """Uma ação externa foi decidida e executada: a tarefa que esperava por ela volta para a fila quando não resta nenhuma."""
    task = db.get(Task, action.task_id) if action.task_id else None
    if task is None or task.status != "waiting" or task.wait_reason != "acao":
        return []
    open_ = db.query(Action).filter(
        Action.task_id == task.id, Action.status.in_(("awaiting_approval", "approved", "running"))
    ).count()
    if open_:
        return []
    task.status, task.wait_reason = "approved", ""
    db.commit()
    _submit([task.id])
    return [task.id]


def resume_context(db: DB, task: Task) -> str:
    """O que o agente precisa saber ao ser retomado: respostas dos pedidos que esperava e o desfecho das suas ações."""
    lines = []
    kids = (
        db.query(Task).join(TaskLink, TaskLink.task_id == Task.id)
        .filter(TaskLink.parent_id == task.id, TaskLink.wait == 1).order_by(Task.id).all()
    )
    for k in kids:
        res = re.sub(r"```.*?```", "", k.result or "", flags=re.S).strip()[:1500]
        lines.append(f"- pedido a {k.agent_slug} \"{k.title}\": {k.status}" + (f"\n  {res}" if res else ""))
    for a in db.query(Action).filter(Action.task_id == task.id).order_by(Action.id).all():
        extra = a.error if a.status in ("failed", "rejected") else (a.external_ref or a.result)[:300]
        lines.append(f"- ação {a.integration}.{a.operation}: {a.status}" + (f" ({extra})" if extra else ""))
    return (f"{RESUME_MARK} Você estava esperando e agora há novidades:\n" + "\n".join(lines)
            + "\nContinue de onde parou e conclua a tarefa; responda sem blocos bash quando terminar.")


def conversation_text(db: DB, cid: int) -> str:
    """Respostas do agente nesta tarefa; depois de uma retomada, só o que veio depois dela (não reprocessa o antigo)."""
    rows = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id).all()
    start = max((i + 1 for i, m in enumerate(rows) if m.role == "user" and m.content.startswith(RESUME_MARK)), default=0)
    return "\n\n".join(m.content for m in rows[start:] if m.role == "assistant")


# ---------- execução das tarefas aprovadas ----------

def task_brief(project: GrowthProject, agent: Agent, pa: ProjectAgent | None, task: Task, team: str = "",
               tools: str = "") -> str:
    return f"""Você está agindo como o agente **{agent.name}** ({agent.area}) do projeto **{project.name}**.
Missão: {mission(agent, pa)}

{project_context(project)}

O owner APROVOU esta tarefa do plano:
Título: {task.title}
Por quê: {task.rationale}
Passos sugeridos: {task.steps}

{tools}

{team}

Execute agora, do jeito mais seguro e direto, só neste projeto. Mudanças de código só por branch + PR (gos-pr),
nunca na main. Se descobrir números reais de KPIs deste projeto, informe num bloco assim (só chaves existentes):
```kpi
{{"chave_do_kpi": 123}}
```

{content.guide()}

Ao terminar, responda sem blocos bash com um resumo curto do resultado e o próximo passo recomendado."""


def sync_task(db: DB, task: Task) -> None:
    """Atualiza o status da tarefa a partir da conversa dela (útil depois de um 2FA no chat)."""
    if task.conversation_id is None:
        return
    pending = db.query(CommandRun).filter(
        CommandRun.conversation_id == task.conversation_id, CommandRun.status == "pending"
    ).count()
    last = (
        db.query(Message).filter(Message.conversation_id == task.conversation_id).order_by(Message.id.desc()).first()
    )
    if pending:
        task.status, task.wait_reason = "waiting", "2fa"
    elif last is not None and last.role == "assistant":
        task.status, task.wait_reason = "done", ""
        task.result = last.content[-4000:]
        task.finished_at = utcnow()
        ingest_kpi_blocks(db, task.project_id, last.content, f"agente:{task.agent_slug}")
        db.commit()
        full = conversation_text(db, task.conversation_id)
        content.ingest_blocks(db, task, full)
        collab = process_collaboration(db, task, full)
        ids = list(collab["approved"]) + missions.expand(db, task, full)
        waiting_on = [c for c in collab["tasks"] if (lk := db.get(TaskLink, c.id)) is not None and lk.wait]
        has_open_action = db.query(Action).filter(
            Action.task_id == task.id, Action.status.in_(("awaiting_approval", "approved", "running"))
        ).count()
        if waiting_on or has_open_action:  # não terminou: espera outro agente ou a decisão sobre uma ação externa
            task.status, task.wait_reason, task.finished_at = "waiting", "agente" if waiting_on else "acao", None
            db.commit()
            _submit(ids)
            return
        db.commit()
        _submit(ids)
        finish(db, task)
    db.commit()


def sync_task_for_conversation(db: DB, cid: int) -> None:
    task = db.query(Task).filter(Task.conversation_id == cid).one_or_none()
    if task is not None and (task.status == "running" or (task.status == "waiting" and task.wait_reason in ("", "2fa"))):
        sync_task(db, task)


async def execute_task(task_id: int) -> None:
    from . import actions  # import tardio: actions importa este módulo
    from .agent import run_agent  # import tardio: agent importa projects/providers

    db = new_session()
    try:
        task = db.get(Task, task_id)
        if task is None or task.status != "approved":
            return
        resuming = task.conversation_id is not None  # voltou de uma espera (outro agente ou ação externa)
        if not resuming and missions.hold_if_blocked(db, task):
            return
        agent = db.query(Agent).filter(Agent.slug == task.agent_slug).one()
        project = db.get(GrowthProject, task.project_id)
        pa = db.query(ProjectAgent).filter(
            ProjectAgent.project_id == project.id, ProjectAgent.agent_slug == agent.slug
        ).one_or_none()
        try:
            provider = pick_provider(pa, "agents_run", db)
        except ProviderError as e:
            task.status, task.result, task.finished_at = "failed", str(e), utcnow()
            db.commit()
            finish(db, task)
            return
        if resuming:
            conv = db.get(Conversation, task.conversation_id)
            task.status, task.wait_reason, task.finished_at = "running", "", None
            db.add(Message(conversation_id=conv.id, role="user", content=resume_context(db, task)))
        else:
            conv = Conversation(
                title=f"{agent.icon} {project.name} · {agent.name}: {task.title}"[:200],
                category=f"Agentes · {project.name}"[:40],
            )
            db.add(conv)
            db.commit()
            task.conversation_id, task.status = conv.id, "running"
            guide = collaboration_guide(db, project, agent.slug)
            tools = routines.toolbox(db, project, agent.slug, task)
            db.add(Message(conversation_id=conv.id, role="user",
                           content=task_brief(project, agent, pa, task, guide, tools)))
        db.commit()
        error = ""
        ticket = actions.issue_ticket(task)  # permite `gos-int` enquanto esta tarefa roda
        try:
            async for ev in run_agent(db, conv.id, provider, f"agente:{agent.slug}", "agendador"):
                if ev["type"] == "error":
                    error = ev["message"]
        finally:
            actions.revoke_ticket(ticket)
        if error:
            task.status, task.result, task.finished_at = "failed", error, utcnow()
            db.commit()
            finish(db, task)
        else:
            sync_task(db, task)
        if task.status == "waiting" and task.wait_reason == "acao":
            await notify(f"↗ {project.name} · {agent.name} espera você aprovar uma ação externa: {task.title}")
        elif task.status == "waiting" and task.wait_reason == "2fa":
            await notify(f"🔐 {project.name} · {agent.name} precisa do seu 2FA para continuar: {task.title}")
        elif task.status == "failed":
            await notify(f"⚠️ {project.name} · {agent.name} falhou em: {task.title}")
    finally:
        db.close()


class TaskRunner:
    """Fila com `GOS_RUNNER_WORKERS` tarefas ao mesmo tempo (padrão 2), no máximo uma por agente e projeto.

    Assim uma missão anda sem travar o resto e sem estourar os limites dos providers. Tarefa em espera
    (`waiting`) não ocupa worker: ela só volta para a fila quando o que esperava termina.

    As rotas síncronas do FastAPI rodam numa thread do pool, sem event loop. Por isso o runner guarda o
    loop do app (bind, no startup) e, fora dele, agenda a entrada na fila com call_soon_threadsafe.
    """

    def __init__(self):
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.busy: set[tuple[int, str]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        if loop is not self.loop:  # loop novo (startup): fila e workers nascem nele
            self.loop, self.queue, self.workers, self.busy = loop, asyncio.Queue(), [], set()

    def start(self) -> None:
        self.workers = [w for w in self.workers if not w.done()]
        while len(self.workers) < max(1, get_settings().runner_workers):
            self.workers.append(asyncio.create_task(self._loop()))

    def _enqueue(self, task_id: int) -> None:
        self.queue.put_nowait(task_id)
        self.start()

    def submit(self, task_id: int) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (self.loop is None or running is self.loop):
            self._enqueue(task_id)
        elif self.loop is not None:
            self.loop.call_soon_threadsafe(self._enqueue, task_id)
        else:
            raise RuntimeError("fila de tarefas ainda não iniciou")

    @staticmethod
    def _key(task_id: int) -> tuple[int, str] | None:
        db = new_session()
        try:
            t = db.get(Task, task_id)
            return (t.project_id, t.agent_slug) if t is not None else None
        finally:
            db.close()

    async def _loop(self):
        while True:
            tid = await self.queue.get()
            key = self._key(tid)
            if key is not None and key in self.busy:  # esse agente já está trabalhando neste projeto: tenta depois
                self.queue.put_nowait(tid)
                await asyncio.sleep(0.2)  # volta para o fim da fila e deixa os outros passarem
                continue
            if key is not None:
                self.busy.add(key)
            try:
                await execute_task(tid)
            except Exception:  # noqa: BLE001
                log.exception("tarefa %s falhou", tid)
            finally:
                self.busy.discard(key)


runner = TaskRunner()


def requeue_approved(db: DB, hours: int = 48) -> list[int]:
    """No startup: tarefas aprovadas recentemente que ficaram sem rodar (restart, erro) voltam para a fila."""
    since = utcnow() - timedelta(hours=hours)
    ids = [t.id for t in db.query(Task).filter(Task.status == "approved", Task.decided_at >= since).order_by(Task.id)]
    for tid in ids:
        runner.submit(tid)
    return ids


# ---------- agendador e avisos ----------

async def scheduler_loop() -> None:
    """Gera o plano do dia uma vez, a partir da hora configurada (fuso do owner)."""
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    while True:
        try:
            now = datetime.now(tz)
            if now.hour >= s.plan_hour:
                db = new_session()
                try:
                    exists = db.query(Plan).filter(Plan.day == now.date()).count()
                finally:
                    db.close()
                if not exists:
                    await generate_plan(now.date())
        except Exception:  # noqa: BLE001 - o agendador nunca pode morrer
            log.exception("agendador")
        try:
            await collect_daily(datetime.now(tz))
        except Exception:  # noqa: BLE001
            log.exception("coleta de KPIs")
        try:
            await run_automations(datetime.now(tz))
        except Exception:  # noqa: BLE001
            log.exception("automações")
        await asyncio.sleep(60)


_collected_on: date | None = None


async def collect_daily(now: datetime) -> list[str]:
    """Uma vez por dia, antes do plano, lê as integrações prontas e grava os KPIs (sem gastar IA)."""
    global _collected_on
    s = get_settings()
    if _collected_on == now.date() or now.hour < max(s.plan_hour - 1, 0):
        return []
    from . import actions  # import tardio: actions importa este módulo

    _collected_on = now.date()
    db = new_session()
    try:
        done = await actions.collect_kpis(db)
        actions.prune(db)
        return done
    finally:
        db.close()


async def run_automations(now: datetime) -> list[int]:
    """Atividades rotineiras que chegaram na hora viram tarefas (propostas, ou já na fila se o owner liberou)."""
    db = new_session()
    try:
        created = routines.run_due(db, now)
        waiting = []
        for t in created:
            if t.status == "approved":
                runner.submit(t.id)
            else:
                waiting.append(t)
        if waiting:
            names = {p.id: p.name for p in db.query(GrowthProject).all()}
            lines = "\n".join(f"• {names.get(t.project_id, '?')}: {t.title}" for t in waiting)
            await notify(f"🔁 Atividades rotineiras esperando sua aprovação:\n{lines}"
                         + (f"\n{get_settings().public_url.rstrip('/')}/board" if get_settings().public_url else ""))
        return [t.id for t in created]
    finally:
        db.close()


async def notify(text: str) -> bool:
    """Aviso opcional no Telegram (só saída: o bot não recebe comandos)."""
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                json={"chat_id": s.telegram_chat_id, "text": text, "disable_web_page_preview": True},
            )
        return r.status_code == 200
    except httpx.HTTPError:
        return False
