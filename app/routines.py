"""Habilidades, integrações e automações (atividades rotineiras) — tudo POR PROJETO.

- Habilidade: um playbook (instruções) que os agentes escolhidos recebem ao planejar e ao executar.
- Integração: ferramenta externa do projeto (analytics, redes, pagamentos). Aqui só fica a configuração
  pública (URL, id do site...). Chaves e tokens NUNCA passam pela tela nem pelo banco: ficam no .env do VPS,
  numa variável com nome fixo, e a tela só mostra se ela existe.
- Automação: atividade que se repete (todo dia, semana ou mês, numa hora). Na hora marcada vira uma tarefa
  do agente. Por padrão ela entra como proposta e espera a aprovação do owner, igual ao plano do dia.
"""

import json
import logging
import os
import re
from datetime import date, datetime

from sqlalchemy.orm import Session as DB

from .audit import audit
from .db import Agent, Automation, GrowthProject, Integration, ProjectAgent, Skill, Task, utcnow

log = logging.getLogger("routines")

WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
OPEN_STATUSES = ("proposed", "approved", "running", "waiting")

# ---------- biblioteca de habilidades ----------

# chave, nome, descrição curta, agentes, instruções
SKILL_TEMPLATES = [
    ("concorrentes_redes", "Análise de concorrentes nas redes",
     "Lê os últimos posts dos concorrentes e acha o que está engajando.", "social,marketing",
     "1. Liste os concorrentes do projeto (pergunte ao owner se não houver na descrição).\n"
     "2. Para cada um, veja os 10 posts mais recentes públicos: formato, gancho, tema, horário e engajamento visível.\n"
     "3. Agrupe padrões que se repetem nos posts com mais engajamento.\n"
     "4. Entregue: tabela curta por concorrente + 3 ideias de post adaptadas ao nosso projeto, com gancho pronto."),
    ("seo_landing", "Auditoria de SEO da landing",
     "Checa título, meta tags, velocidade e conteúdo das páginas principais.", "growth,design,tech",
     "1. Baixe as páginas principais dos domínios do projeto com curl.\n"
     "2. Verifique title, meta description, h1, og:tags, canonical, robots.txt e sitemap.xml.\n"
     "3. Meça tamanho e tempo de resposta (curl -w) e aponte imagens ou scripts pesados.\n"
     "4. Entregue uma lista priorizada (impacto x esforço). Correções de código só por branch + PR."),
    ("relatorio_kpis", "Relatório semanal de KPIs",
     "Resume a semana: o que subiu, o que caiu e o porquê provável.", "growth,financeiro",
     "1. Compare cada KPI do projeto com a semana anterior e com a meta.\n"
     "2. Destaque os 3 maiores ganhos e as 3 maiores quedas, com hipótese de causa.\n"
     "3. Liste o que falta medir (KPIs sem dado) e como obter.\n"
     "4. Termine com 3 prioridades para a próxima semana."),
    ("saude_containers", "Checagem de saúde dos containers",
     "Vê containers, reinícios e erros recentes nos logs do projeto.", "tech",
     "1. Rode gos-docker ps no projeto compose e anote status e reinícios.\n"
     "2. Leia os últimos logs de cada container (gos-docker logs) procurando erros, timeouts e 5xx.\n"
     "3. Teste os domínios do projeto com curl -sI e anote código e tempo.\n"
     "4. Entregue: tudo ok, ou o problema com evidência e a correção proposta. Reiniciar pede 2FA."),
    ("pauta_conteudo", "Pauta de conteúdo da semana",
     "Monta 5 posts com gancho, formato e legenda.", "social",
     "1. Use os KPIs de social e o que funcionou nas últimas semanas.\n"
     "2. Proponha 5 posts: formato (reels, carrossel, stories), gancho, roteiro curto e legenda com CTA.\n"
     "3. Sugira dia e horário para cada um.\n"
     "4. Nada é publicado sem o owner aprovar."),
    ("revisao_pr", "Revisão de código e segurança",
     "Revisa os PRs abertos e as dependências do repositório.", "tech,produto",
     "1. Liste os PRs abertos do repositório do projeto.\n"
     "2. Para cada um, leia o diff procurando bugs, segredos no código e falhas de segurança.\n"
     "3. Verifique dependências desatualizadas ou com vulnerabilidade conhecida.\n"
     "4. Entregue os achados por PR. Nunca faça merge nem push na main."),
]

# ---------- catálogo de integrações (grátis / self-hosted primeiro) ----------

INTEGRATIONS = [
    {"kind": "umami", "name": "Umami", "icon": "📊", "category": "Analytics", "cost": "Open source · self-hosted",
     "about": "Visitas, páginas e origens do tráfego, sem cookies. Roda num container no próprio VPS.",
     "fields": [("url", "URL do Umami", "https://analytics.seudominio.com"), ("website_id", "ID do site", "uuid do site no Umami")],
     "secret": "API_KEY", "kpis": "visitantes, conversao_cadastro"},
    {"kind": "plausible", "name": "Plausible CE", "icon": "📈", "category": "Analytics", "cost": "Open source · self-hosted",
     "about": "Alternativa leve ao Google Analytics, também roda no VPS.",
     "fields": [("url", "URL do Plausible", "https://plausible.seudominio.com"), ("site_id", "Domínio do site", "seudominio.com")],
     "secret": "API_KEY", "kpis": "visitantes"},
    {"kind": "search_console", "name": "Google Search Console", "icon": "🔎", "category": "SEO", "cost": "Grátis",
     "about": "Cliques, impressões e posição no Google para os domínios do projeto.",
     "fields": [("property", "Propriedade", "sc-domain:seudominio.com")],
     "secret": "SERVICE_ACCOUNT_JSON", "kpis": "visitantes"},
    {"kind": "meta_graph", "name": "Instagram / Facebook", "icon": "📸", "category": "Redes sociais", "cost": "Grátis (Graph API)",
     "about": "Seguidores, alcance e engajamento dos posts pela API oficial da Meta.",
     "fields": [("ig_user_id", "ID da conta do Instagram", "1784..."), ("page_id", "ID da página do Facebook", "opcional")],
     "secret": "ACCESS_TOKEN", "kpis": "seguidores, engajamento"},
    {"kind": "postiz", "name": "Postiz", "icon": "🗓", "category": "Redes sociais", "cost": "Open source · self-hosted",
     "about": "Agenda posts em várias redes. Os agentes deixam rascunhos; publicar continua sendo decisão sua.",
     "fields": [("url", "URL do Postiz", "https://postiz.seudominio.com")],
     "secret": "API_KEY", "kpis": ""},
    {"kind": "uptime_kuma", "name": "Uptime Kuma", "icon": "🟢", "category": "Infra", "cost": "Open source · self-hosted",
     "about": "Monitora se os sites estão no ar e avisa quando caem.",
     "fields": [("url", "URL do Uptime Kuma", "https://status.seudominio.com"), ("status_page", "Slug da página de status", "projeto")],
     "secret": "", "kpis": "uptime"},
    {"kind": "stripe", "name": "Stripe", "icon": "💳", "category": "Pagamentos", "cost": "Grátis (cobra só por venda)",
     "about": "Receita, MRR e churn direto da conta. Use uma chave restrita só de leitura.",
     "fields": [("account", "Conta", "acct_... ou nome")],
     "secret": "RESTRICTED_KEY", "kpis": "mrr, receita, churn"},
    {"kind": "project_api", "name": "API do próprio projeto", "icon": "🔌", "category": "Projeto", "cost": "Já é seu",
     "about": "Endpoints do próprio produto (usuários, pedidos, uso) para os agentes lerem números reais.",
     "fields": [("base_url", "URL base", "https://api.seudominio.com"), ("docs", "Onde está a documentação", "/docs ou link")],
     "secret": "TOKEN", "kpis": "usuarios_ativos, cadastros"},
]
INTEGRATION_BY_KIND = {i["kind"]: i for i in INTEGRATIONS}

# título, agente, habilidade (template), frequência, dia da semana, dia do mês, hora
AUTOMATION_TEMPLATES = [
    ("Checar a saúde dos sites e containers", "tech", "saude_containers", "daily", 0, 1, 8),
    ("Relatório semanal de KPIs", "growth", "relatorio_kpis", "weekly", 0, 1, 9),
    ("Montar a pauta de conteúdo da semana", "social", "pauta_conteudo", "weekly", 4, 1, 10),
    ("Olhar o que os concorrentes postaram", "social", "concorrentes_redes", "weekly", 2, 1, 10),
    ("Auditoria de SEO das páginas principais", "growth", "seo_landing", "monthly", 0, 1, 9),
]


def split_agents(raw: str) -> list[str]:
    return [s for s in (x.strip() for x in (raw or "").split(",")) if s]


def secret_env_name(project: GrowthProject, kind: str) -> str:
    """Nome fixo da variável no .env do VPS. Gerado por nós (nunca vem do usuário)."""
    meta = INTEGRATION_BY_KIND.get(kind) or {}
    if not meta.get("secret"):
        return ""
    slug = re.sub(r"[^A-Z0-9]+", "_", project.slug.upper()).strip("_")
    return f"GOS_INT_{slug}_{kind.upper()}_{meta['secret']}"


def integrations_for(db: DB, project: GrowthProject) -> list[dict]:
    rows = {r.kind: r for r in db.query(Integration).filter(Integration.project_id == project.id).all()}
    out = []
    for meta in INTEGRATIONS:
        row = rows.get(meta["kind"])
        config = _config(row)
        env = secret_env_name(project, meta["kind"])
        has_secret = bool(env and os.environ.get(env))
        filled = bool(config) and all(config.get(k) for k, *_ in meta["fields"][:1])
        if row is None or not filled:
            status = "off"
        elif env and not has_secret:
            status = "needs_secret"
        else:
            status = "ready"
        out.append({
            "kind": meta["kind"], "name": meta["name"], "icon": meta["icon"], "category": meta["category"],
            "cost": meta["cost"], "about": meta["about"], "kpis": meta["kpis"],
            "fields": [{"key": k, "label": label, "placeholder": ph} for k, label, ph in meta["fields"]],
            "config": config, "enabled": bool(row and row.enabled), "connected": row is not None,
            "secret_env": env, "has_secret": has_secret, "status": status if (row is None or row.enabled) else "paused",
        })
    return out


def _config(row: Integration | None) -> dict:
    if row is None:
        return {}
    try:
        data = json.loads(row.config or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_integration(db: DB, project: GrowthProject, kind: str, config: dict, enabled: bool | None) -> Integration:
    meta = INTEGRATION_BY_KIND.get(kind)
    if meta is None:
        raise KeyError(kind)
    allowed = {k for k, *_ in meta["fields"]}
    clean = {k: str(v).strip()[:300] for k, v in (config or {}).items() if k in allowed and str(v or "").strip()}
    row = db.query(Integration).filter(Integration.project_id == project.id, Integration.kind == kind).one_or_none()
    if row is None:
        row = Integration(project_id=project.id, kind=kind)
        db.add(row)
    row.config = json.dumps(clean, ensure_ascii=False)
    if enabled is not None:
        row.enabled = enabled
    db.commit()
    return row


# ---------- contexto que os agentes recebem ----------

def skills_for_agent(db: DB, project_id: int, slug: str) -> list[Skill]:
    rows = db.query(Skill).filter(Skill.project_id == project_id, Skill.enabled.is_(True)).order_by(Skill.id).all()
    return [s for s in rows if not split_agents(s.agents) or slug in split_agents(s.agents)]


def toolbox(db: DB, project: GrowthProject, slug: str, task: Task | None = None, full: bool = True) -> str:
    """Habilidades e integrações deste projeto que o agente pode usar (texto para o prompt)."""
    parts = []
    focus = None
    if task is not None and task.automation_id:
        auto = db.get(Automation, task.automation_id)
        if auto is not None and auto.skill_id:
            focus = db.get(Skill, auto.skill_id)
    skills = skills_for_agent(db, project.id, slug)
    if focus is not None and focus.project_id == project.id:
        parts.append(f"Esta tarefa é uma atividade rotineira. Siga a habilidade **{focus.name}**:\n{focus.instructions.strip()}")
        skills = [s for s in skills if s.id != focus.id]
    if skills:
        if full:
            lines = [f"- **{s.name}**: {s.description}\n  {s.instructions.strip()[:800]}" for s in skills]
        else:
            lines = [f"- **{s.name}**: {s.description}" for s in skills]
        parts.append("Habilidades que você tem neste projeto (use quando fizer sentido):\n" + "\n".join(lines))
    ready = [i for i in integrations_for(db, project) if i["status"] in ("ready", "needs_secret")]
    if ready:
        lines = []
        for i in ready:
            cfg = ", ".join(f"{k}={v}" for k, v in i["config"].items())
            note = "" if i["status"] == "ready" else " (a chave ainda não foi configurada no VPS)"
            lines.append(f"- {i['name']} ({i['category']}): {cfg}{note}")
        parts.append("Integrações ligadas neste projeto (configuração pública; chaves não ficam com você):\n" + "\n".join(lines))
    return "\n\n".join(parts)


# ---------- automações ----------

def describe_schedule(a: Automation) -> str:
    h = f"{a.hour}h"
    if a.frequency == "weekly":
        return f"Toda {WEEKDAYS[a.weekday % 7]} às {h}"
    if a.frequency == "monthly":
        return f"Todo dia {a.monthday} às {h}"
    return f"Todo dia às {h}"


def scheduled_today(a: Automation, today: date) -> bool:
    if a.frequency == "weekly":
        return today.weekday() == a.weekday
    if a.frequency == "monthly":
        return today.day == a.monthday
    return True


def is_due(a: Automation, now: datetime) -> bool:
    return a.enabled and a.last_run_on != now.date() and now.hour >= a.hour and scheduled_today(a, now.date())


def next_run(a: Automation, now: datetime) -> str:
    """Próxima data em que a automação roda (ISO), olhando até 62 dias à frente."""
    from datetime import timedelta

    for i in range(0, 62):
        d = now.date() + timedelta(days=i)
        if not scheduled_today(a, d):
            continue
        if i == 0 and (a.last_run_on == d or now.hour > a.hour):
            continue
        return datetime(d.year, d.month, d.day, a.hour).isoformat()
    return ""


def has_open_run(db: DB, a: Automation) -> bool:
    return db.query(Task).filter(Task.automation_id == a.id, Task.status.in_(OPEN_STATUSES)).count() > 0


def create_run(db: DB, a: Automation, approved: bool, actor: str) -> Task:
    skill = db.get(Skill, a.skill_id) if a.skill_id else None
    t = Task(
        project_id=a.project_id, agent_slug=a.agent_slug, title=a.title[:200], steps=a.steps,
        rationale=f"Atividade rotineira: {describe_schedule(a).lower()}" + (f" · habilidade {skill.name}" if skill else ""),
        status="approved" if approved else "proposed", decided_at=utcnow() if approved else None,
        automation_id=a.id,
    )
    db.add(t)
    db.commit()
    audit(db, actor, "automation_run", f"#{a.id} → tarefa #{t.id} ({t.status}): {a.title}"[:500])
    return t


def run_due(db: DB, now: datetime) -> list[Task]:
    """Cria as tarefas das automações que chegaram na hora. Não empilha: se a anterior ainda está aberta, pula."""
    created = []
    for a in db.query(Automation).filter(Automation.enabled.is_(True)).order_by(Automation.id).all():
        if not is_due(a, now):
            continue
        a.last_run_on = now.date()
        db.commit()
        project = db.get(GrowthProject, a.project_id)
        pa = db.query(ProjectAgent).filter(
            ProjectAgent.project_id == a.project_id, ProjectAgent.agent_slug == a.agent_slug
        ).one_or_none()
        if project is None or not project.enabled or pa is None or not pa.enabled or has_open_run(db, a):
            continue
        created.append(create_run(db, a, a.auto_approve, "agendador"))
    return created


def automation_json(db: DB, a: Automation, now: datetime, agents: dict[str, Agent] | None = None) -> dict:
    last = db.query(Task).filter(Task.automation_id == a.id).order_by(Task.id.desc()).first()
    skill = db.get(Skill, a.skill_id) if a.skill_id else None
    return {
        "id": a.id, "agent": a.agent_slug, "title": a.title, "steps": a.steps, "skill_id": a.skill_id,
        "skill": skill.name if skill else "", "frequency": a.frequency, "weekday": a.weekday, "monthday": a.monthday,
        "hour": a.hour, "auto_approve": a.auto_approve, "enabled": a.enabled,
        "schedule": describe_schedule(a), "next_run": next_run(a, now) if a.enabled else "",
        "last_run_on": a.last_run_on.isoformat() if a.last_run_on else None,
        "last_task": {"id": last.id, "status": last.status, "conversation_id": last.conversation_id} if last else None,
    }


def skill_json(s: Skill) -> dict:
    return {"id": s.id, "name": s.name, "description": s.description, "instructions": s.instructions,
            "agents": split_agents(s.agents), "template": s.template, "enabled": s.enabled}
