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
    Agent, CommandRun, Conversation, GrowthProject, Message, Metric, MetricPoint, Plan, ProjectAgent, Task,
    new_session, utcnow,
)
from .providers import ChatTurn, Provider, ProviderError, all_providers

log = logging.getLogger("growth")

MAX_TASKS_PER_AGENT = 3

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

def pick_provider(pa: ProjectAgent | None = None) -> Provider:
    providers = all_providers()
    for pid in [pa.provider if pa else None, get_settings().agent_provider]:
        p = providers.get(pid or "")
        if p and p.available()[0]:
            return p
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

Proponha de 1 a {MAX_TASKS_PER_AGENT} tarefas para HOJE, concretas e de alto impacto na sua área, só para este projeto.
Cada tarefa precisa ser executável por você com o que o Growth OS oferece (shell no /workspace, gos-docker,
git e gos-pr no repositório do projeto, internet), ou preparar algo para o owner decidir.
Se faltam dados nos KPIs da sua área, uma boa tarefa é descobrir como obtê-los.
Não repita tarefas já concluídas. Risco "alto" é para o que mexe em produção.

Responda SOMENTE com JSON válido, sem texto antes ou depois:
{{"tarefas": [{{"titulo": "...", "porque": "impacto esperado e KPI", "passos": "passo 1; passo 2; ...", "risco": "baixo|medio|alto", "kpi": "chave do KPI ou vazio"}}]}}"""


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
    provider = pick_provider(pa)
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
                for agent, pa in team(db, project) if pa.enabled
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
                             + (f"\n{get_settings().public_url.rstrip('/')}/plan" if get_settings().public_url else ""))
            return plan.id
        finally:
            db.close()


# ---------- execução das tarefas aprovadas ----------

def task_brief(project: GrowthProject, agent: Agent, pa: ProjectAgent | None, task: Task) -> str:
    return f"""Você está agindo como o agente **{agent.name}** ({agent.area}) do projeto **{project.name}**.
Missão: {mission(agent, pa)}

{project_context(project)}

O owner APROVOU esta tarefa do plano:
Título: {task.title}
Por quê: {task.rationale}
Passos sugeridos: {task.steps}

Execute agora, do jeito mais seguro e direto, só neste projeto. Mudanças de código só por branch + PR (gos-pr),
nunca na main. Se descobrir números reais de KPIs deste projeto, informe num bloco assim (só chaves existentes):
```kpi
{{"chave_do_kpi": 123}}
```
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
        task.status = "waiting"
    elif last is not None and last.role == "assistant":
        task.status = "done"
        task.result = last.content[-4000:]
        task.finished_at = utcnow()
        ingest_kpi_blocks(db, task.project_id, last.content, f"agente:{task.agent_slug}")
    db.commit()


def sync_task_for_conversation(db: DB, cid: int) -> None:
    task = db.query(Task).filter(Task.conversation_id == cid).one_or_none()
    if task is not None and task.status in ("running", "waiting"):
        sync_task(db, task)


async def execute_task(task_id: int) -> None:
    from .agent import run_agent  # import tardio: agent importa projects/providers

    db = new_session()
    try:
        task = db.get(Task, task_id)
        if task is None or task.status != "approved":
            return
        agent = db.query(Agent).filter(Agent.slug == task.agent_slug).one()
        project = db.get(GrowthProject, task.project_id)
        pa = db.query(ProjectAgent).filter(
            ProjectAgent.project_id == project.id, ProjectAgent.agent_slug == agent.slug
        ).one_or_none()
        try:
            provider = pick_provider(pa)
        except ProviderError as e:
            task.status, task.result = "failed", str(e)
            db.commit()
            return
        conv = Conversation(title=f"{agent.icon} {project.name} · {agent.name}: {task.title}"[:200])
        db.add(conv)
        db.commit()
        task.conversation_id, task.status = conv.id, "running"
        db.add(Message(conversation_id=conv.id, role="user", content=task_brief(project, agent, pa, task)))
        db.commit()
        error = ""
        async for ev in run_agent(db, conv.id, provider, f"agente:{agent.slug}", "agendador"):
            if ev["type"] == "error":
                error = ev["message"]
        if error:
            task.status, task.result, task.finished_at = "failed", error, utcnow()
            db.commit()
        else:
            sync_task(db, task)
        if task.status == "waiting":
            await notify(f"🔐 {project.name} · {agent.name} precisa do seu 2FA para continuar: {task.title}")
        elif task.status == "failed":
            await notify(f"⚠️ {project.name} · {agent.name} falhou em: {task.title}")
    finally:
        db.close()


class TaskRunner:
    """Fila única: uma tarefa por vez, para não sobrecarregar o VPS nem os limites dos providers."""

    def __init__(self):
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.worker: asyncio.Task | None = None

    def start(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._loop())

    def submit(self, task_id: int) -> None:
        self.queue.put_nowait(task_id)
        self.start()

    async def _loop(self):
        while True:
            tid = await self.queue.get()
            try:
                await execute_task(tid)
            except Exception:  # noqa: BLE001
                log.exception("tarefa %s falhou", tid)


runner = TaskRunner()


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
        await asyncio.sleep(60)


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
