"""Etapa 3: agentes por área, KPIs e o plano da manhã.

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
from .db import Agent, CommandRun, Conversation, Message, Metric, MetricPoint, Plan, Task, new_session, utcnow
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
    """Cria os agentes e KPIs padrão que ainda não existem (não altera os que o owner editou)."""
    have = {a.slug for a in db.query(Agent.slug).all()}
    for slug, name, area, icon, mission in DEFAULT_AGENTS:
        if slug not in have:
            db.add(Agent(slug=slug, name=name, area=area, icon=icon, mission=mission))
    keys = {m.key for m in db.query(Metric.key).all()}
    for key, name, area, unit, direction in DEFAULT_METRICS:
        if key not in keys:
            db.add(Metric(key=key, name=name, area=area, unit=unit, direction=direction))
    db.commit()


def local_today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


# ---------- KPIs ----------

def record_point(db: DB, key: str, value: float, day: date | None = None, source: str = "manual", note: str = "") -> MetricPoint:
    metric = db.query(Metric).filter(Metric.key == key).one_or_none()
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


def metric_series(db: DB, days: int = 90) -> list[dict]:
    since = local_today() - timedelta(days=days)
    metrics = db.query(Metric).order_by(Metric.area, Metric.id).all()
    points = (
        db.query(MetricPoint).filter(MetricPoint.day >= since).order_by(MetricPoint.day).all()
    )
    by_metric: dict[int, list[MetricPoint]] = {}
    for p in points:
        by_metric.setdefault(p.metric_id, []).append(p)
    out = []
    for m in metrics:
        pts = by_metric.get(m.id, [])
        last = pts[-1].value if pts else None
        prev = pts[-2].value if len(pts) > 1 else None
        out.append({
            "key": m.key, "name": m.name, "area": m.area, "unit": m.unit, "direction": m.direction,
            "target": m.target, "description": m.description,
            "points": [{"day": p.day.isoformat(), "value": p.value, "source": p.source} for p in pts],
            "last": last, "prev": prev,
        })
    return out


def kpi_snapshot(db: DB, days: int = 30) -> str:
    """Texto curto com os KPIs para os agentes lerem."""
    lines = []
    for m in metric_series(db, days):
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


def ingest_kpi_blocks(db: DB, text: str, source: str) -> list[str]:
    """Agentes podem reportar números num bloco ```kpi {"chave": valor}```. Só chaves já cadastradas."""
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
                record_point(db, str(key), float(value), source=source)
                saved.append(str(key))
            except (KeyError, TypeError, ValueError):
                continue
    return saved


# ---------- providers ----------

def pick_provider(agent: Agent | None = None) -> Provider:
    providers = all_providers()
    for pid in [agent.provider if agent else None, get_settings().agent_provider]:
        p = providers.get(pid or "")
        if p and p.available()[0]:
            return p
    for p in providers.values():
        if p.available()[0]:
            return p
    raise ProviderError("nenhum provider disponível para os agentes")


# ---------- plano da manhã ----------

def _yesterday_report(db: DB, slug: str) -> str:
    since = utcnow() - timedelta(days=3)
    rows = (
        db.query(Task).filter(Task.agent_slug == slug, Task.created_at >= since).order_by(Task.id.desc()).limit(8).all()
    )
    if not rows:
        return "nenhuma tarefa recente"
    return "\n".join(f"- [{t.status}] {t.title}" + (f" → {t.result[:200]}" if t.result else "") for t in rows)


def planning_prompt(db: DB, agent: Agent, today: date) -> str:
    return f"""MODO PLANEJAMENTO. Nesta mensagem NÃO escreva blocos ```bash e não rode nada.

Você é o agente **{agent.name}** ({agent.area}) do Growth OS.
Missão: {agent.mission}

Hoje é {today.isoformat()}.

KPIs da empresa (últimos 30 dias):
{kpi_snapshot(db)}

Suas tarefas recentes:
{_yesterday_report(db, agent.slug)}

Proponha de 1 a {MAX_TASKS_PER_AGENT} tarefas para HOJE, concretas e de alto impacto na sua área.
Cada tarefa precisa ser executável por você com o que o Growth OS oferece (shell no /workspace, gos-docker,
git e gos-pr nos repos dos projetos, internet), ou preparar algo para o owner decidir.
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


async def plan_for_agent(db: DB, agent: Agent, plan: Plan, today: date) -> int:
    provider = pick_provider(agent)
    reply = await provider.complete([ChatTurn("user", planning_prompt(db, agent, today))])
    tasks = parse_tasks(reply)
    for t in tasks:
        db.add(Task(plan_id=plan.id, agent_slug=agent.slug, title=t["title"], rationale=t["rationale"],
                    steps=t["steps"], risk=t["risk"], kpi_key=t["kpi"]))
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

            agents = db.query(Agent).filter(Agent.enabled.is_(True)).order_by(Agent.id).all()
            sem = asyncio.Semaphore(max(1, get_settings().agent_concurrency))
            errors: list[str] = []

            async def one(agent: Agent):
                async with sem:
                    s = new_session()  # cada agente com sua sessão: rodam em paralelo
                    try:
                        await plan_for_agent(s, s.get(Agent, agent.id), s.get(Plan, plan.id), today)
                    except Exception as e:  # noqa: BLE001 - um agente com erro não derruba o plano
                        errors.append(f"{agent.name}: {e}"[:300])
                    finally:
                        s.close()

            await asyncio.gather(*(one(a) for a in agents))
            db.refresh(plan)
            count = db.query(Task).filter(Task.plan_id == plan.id).count()
            plan.status = "ready" if count or not errors else "failed"
            plan.error = "\n".join(errors)
            db.commit()
            if plan.status == "ready":
                await notify(f"☀️ Plano de hoje pronto: {count} tarefas de {len(agents)} agentes esperando sua aprovação."
                             + (f"\n{get_settings().public_url.rstrip('/')}/plan" if get_settings().public_url else ""))
            return plan.id
        finally:
            db.close()


# ---------- execução das tarefas aprovadas ----------

def task_brief(agent: Agent, task: Task) -> str:
    return f"""Você está agindo como o agente **{agent.name}** ({agent.area}).
Missão: {agent.mission}

O owner APROVOU esta tarefa do plano de hoje:
Título: {task.title}
Por quê: {task.rationale}
Passos sugeridos: {task.steps}

Execute agora, do jeito mais seguro e direto. Mudanças de código só por branch + PR (gos-pr), nunca na main.
Se descobrir números reais de KPIs, informe num bloco assim (só chaves existentes):
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
        ingest_kpi_blocks(db, last.content, f"agente:{task.agent_slug}")
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
        try:
            provider = pick_provider(agent)
        except ProviderError as e:
            task.status, task.result = "failed", str(e)
            db.commit()
            return
        conv = Conversation(title=f"{agent.icon} {agent.name}: {task.title}"[:200])
        db.add(conv)
        db.commit()
        task.conversation_id, task.status = conv.id, "running"
        db.add(Message(conversation_id=conv.id, role="user", content=task_brief(agent, task)))
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
            await notify(f"🔐 {agent.name} precisa do seu 2FA para continuar: {task.title}")
        elif task.status == "failed":
            await notify(f"⚠️ {agent.name} falhou em: {task.title}")
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
