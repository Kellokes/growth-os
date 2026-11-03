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
from .db import Agent, Automation, Event, GrowthProject, Integration, ProjectAgent, Skill, Task, utcnow

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
     "1. Rode primeiro: gos-int run project_api docs '{}'. Ele devolve a documentação cadastrada na integração com os "
     "endpoints que existem; a URL base já é a da integração. Use só esses endpoints, com gos-int run project_api get "
     "'{\"caminho\": \"/...\"}'. Não chute rotas: se o get der 404 duas vezes seguidas, pare e diga qual endpoint faltou.\n"
     "   Se a documentação aceitar datas, use a semana atual (últimos 7 dias) e a anterior (7 dias antes) para comparar. "
     "Calcule as datas com: date -d '7 days ago' +%F.\n"
     "2. Compare cada KPI do projeto com a semana anterior e com a meta.\n"
     "3. Destaque os 3 maiores ganhos e as 3 maiores quedas, com hipótese de causa.\n"
     "4. Liste o que falta medir (KPIs sem dado) e como obter.\n"
     "5. Termine com 3 prioridades para a próxima semana."),
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
    ("motion_roteiro", "Roteiro de motion graphics",
     "Como planejar um vídeo curto animado (reel, story) antes de programar: formato, ritmo e entrega.", "design,social,marketing",
     "Use antes de qualquer animação. Fonte: regras da casa, alinhadas às práticas do Remotion.\n"
     "1. Objetivo: uma frase (o que o espectador deve sentir ou fazer) e o CTA final.\n"
     "2. Formato: reel e story = vertical 9:16, 1080x1920, 30 fps; feed = 4:5, 1080x1350. Duração de 6 a 15 s; o gancho "
     "(movimento + texto) precisa aparecer no primeiro segundo.\n"
     "3. Áreas seguras: deixe livres cerca de 14% do topo e 20% da base (a interface do app cobre essas faixas). Nenhum "
     "texto ou logo nelas.\n"
     "4. Texto: no máximo 6 palavras por tela, contraste forte com o fundo, fonte grossa. Toda palavra visível em português correto.\n"
     "5. Ritmo: uma ideia por cena, cenas de 1 a 2 s, entradas e saídas com easing (nunca linear), movimento principal "
     "sempre na direção da leitura.\n"
     "6. Marca: use as cores, fontes e o template aprovado do projeto (Layouts). Não invente identidade nova.\n"
     "7. Roteiro por cena: tempo inicial e final, o que aparece, o que se move, o texto exato.\n"
     "8. Escolha a ferramenta: vídeo completo = habilidade de Remotion; cena 3D = Remotion + three.js; animação de "
     "elementos de HTML ou SVG = GSAP.\n"
     "9. Entrega: o código da composição numa branch do repositório do projeto (pasta motion/) com PR, o roteiro e um bloco "
     "```conteudo``` com formato reel ou story e a descrição do que aparece. O render em MP4 feito pelo sistema chega numa etapa "
     "seguinte: não prometa vídeo pronto, não baixe nada da internet e não rode comandos fora do projeto."),
    ("motion_remotion", "Vídeo programado com Remotion",
     "Remotion (React): vídeos determinísticos, quadro a quadro, com texto, imagens, áudio e transições.", "design,social,marketing",
     "Resumo revisado das skills oficiais do Remotion (remotion-dev/remotion, packages/skills, versão 4.0.540, commit "
     "fe9f0c372706f2039da6b2a0a92b92aa746e69cf). Licença do Remotion: grátis para equipes de até 3 pessoas.\n"
     "REGRA DE OURO: cada frame é uma função do número do frame. Nada anima sozinho.\n"
     "- Use useCurrentFrame() e useVideoConfig() (fps, width, height, durationInFrames). Proibido: CSS transition ou "
     "animation, setTimeout/setInterval, requestAnimationFrame, Math.random() (use random('semente') do remotion).\n"
     "- Movimento: interpolate(frame, [0, 0.3 * fps], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}). "
     "Easing.bezier para curvas; spring() para entradas elásticas (damping alto = sem quique).\n"
     "- Propriedades individuais (scale, translate, rotate) no style, sem montar strings de transform.\n"
     "- Estrutura: <Composition id width height fps durationInFrames component /> na raiz (formato vertical = 1080x1920, 30 fps). "
     "Cenas dentro de <Sequence from durationInFrames>; em fila, <Series>; transições com @remotion/transitions "
     "(<TransitionSeries>, fade, slide, wipe). Fundo e camadas com <AbsoluteFill>.\n"
     "- Mídia: <Img>, <Video>, <Audio> do remotion (nunca as tags nativas), arquivos em public/ com staticFile(). Fontes: "
     "@remotion/google-fonts ou fonte local; só use depois de carregada.\n"
     "- Duração: calcule em segundos * fps; para tamanho variável, calculateMetadata.\n"
     "- Parâmetros editáveis (textos, cores) como props com schema zod, para trocar sem mexer no código.\n"
     "- Fluxo: crie o projeto com npx create-video@latest --yes --blank, rode npx remotion studio para a prévia e NÃO "
     "renderize a menos que peçam. Renderização manual: npx remotion render <id> saida.mp4.\n"
     "- Respeite mudanças que o owner fez no código; não sobrescreva.\n"
     "SEGURANÇA: instale só remotion, @remotion/*, react, react-dom, three, @react-three/fiber, @react-three/drei e gsap. "
     "Não rode scripts baixados da internet, não use curl | sh, não leia .env nem chaves. Sem mídia externa: use só "
     "arquivos do projeto."),
    ("motion_threejs", "Cenas 3D com three.js",
     "Three.js + React Three Fiber dentro de vídeo: objetos, luz, materiais, shaders e câmera, determinísticos.", "design,social,marketing",
     "Resumo revisado a partir da documentação oficial do three.js e das regras de 3D do Remotion (remotion-markup/3d.md, "
     "commit fe9f0c372706f2039da6b2a0a92b92aa746e69cf). Use com a habilidade de Remotion.\n"
     "DENTRO DO REMOTION:\n"
     "- Instale @remotion/three (npx remotion add @remotion/three). Todo 3D fica dentro de <ThreeCanvas width height> "
     "(width e height vêm de useVideoConfig) e SEMPRE com luz (ambientLight + directionalLight).\n"
     "- Nada anima sozinho: proibido useFrame() do @react-three/fiber. Toda rotação, posição, câmera e uniform vem de "
     "useCurrentFrame(). Senão o vídeo pisca no render.\n"
     "- <Sequence> dentro do <ThreeCanvas> precisa de layout=\"none\".\n"
     "BASE DO THREE.JS: Scene + câmera + luzes + Mesh (geometria + material). PerspectiveCamera com fov 35 a 50 dá aspecto "
     "de produto; posicione e use lookAt. Materiais: MeshStandardMaterial ou MeshPhysicalMaterial (PBR: color, roughness, "
     "metalness); MeshBasicMaterial para formas chapadas sem luz. Cor em sRGB; renderer com toneMapping ACESFilmic fica mais natural.\n"
     "LOOK: luz de três pontos (principal, preenchimento, contraluz), fundo com gradiente ou cor da marca, formas simples "
     "(esfera, toro, caixa arredondada, instâncias) com material bonito valem mais que modelos pesados. Texto legível "
     "fica em HTML por cima do canvas, não em 3D (acentos e quebra de linha em português).\n"
     "MOVIMENTO: ease com interpolate/spring do Remotion; rotação contínua = frame * velocidade; câmera orbitando = "
     "posição por seno e cosseno do frame.\n"
     "SHADERS: ShaderMaterial com uniforms; o tempo do uniform vem do frame, nunca de clock.\n"
     "PERFORMANCE: reuse geometrias e materiais, InstancedMesh para muitas cópias, poucas luzes e sombras só se precisar, "
     "pós-processamento (bloom, DOF) é pesado: teste a duração do render antes.\n"
     "SEGURANÇA: modelos GLTF/texturas só de arquivos do projeto (public/); nada de URLs externas nem CDN."),
    ("motion_gsap", "Animação de HTML e SVG com GSAP",
     "GSAP: tweens, timelines e easing para animar elementos, textos e SVG com controle fino.", "design,social,marketing",
     "Resumo revisado das skills oficiais do GSAP (greensock/gsap-skills, licença MIT, commit "
     "aed9cfd3277740755f6bfc1155c7aa645403b760).\n"
     "QUANDO USAR: animar elementos de HTML ou SVG (textos, ícones, logos, morph de formas) com sequência e controle. "
     "Para vídeo final prefira Remotion; GSAP entra como motor de animação dentro dele.\n"
     "BÁSICO: gsap.to(alvo, vars) vai do estado atual até vars; from() entra de vars até o estado atual; fromTo() define "
     "início e fim; set() aplica na hora. Vars comuns: duration (s), delay, ease ('power3.out', 'back.out(1.7)', "
     "'elastic.out(1, 0.3)'), stagger (0.1 ou {each, from:'center'}), repeat, yoyo. Nomes em camelCase.\n"
     "TRANSFORMS: use os atalhos do GSAP (x, y, scale, rotation, skewX), não a string transform. Anime transform e "
     "opacity: ficam na GPU e rodam a 60 fps; evite width, height, top, left, margin.\n"
     "TIMELINE: const tl = gsap.timeline({defaults: {duration: 0.5, ease: 'power2.out'}}); tl.to(...).to(...). Posição "
     "no terceiro argumento: 1 (tempo absoluto), '+=0.5' (depois do fim), '<' (junto do início do anterior), "
     "'<0.2', 'label'. Use labels para cenas e timelines aninhadas para blocos reutilizáveis.\n"
     "DENTRO DO REMOTION: crie a timeline pausada (gsap.timeline({paused: true})) e a cada frame faça tl.time(frame / fps) "
     "(em useLayoutEffect), assim o resultado é determinístico. Não deixe o GSAP rodar sozinho no render.\n"
     "BOAS PRÁTICAS: stagger no lugar de vários tweens com delay manual; will-change: transform nos elementos animados; "
     "gsap.matchMedia() para respeitar prefers-reduced-motion; limpe os tweens ao desmontar (gsap.context().revert()).\n"
     "SEGURANÇA: use só o núcleo e plugins gratuitos instalados por npm (gsap); não carregue scripts de CDN nem de URL externa."),
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
     "about": "Agenda e publica posts em várias redes. Rascunhos saem direto; agendar e publicar sempre pedem a sua aprovação.",
     "fields": [("url", "URL do Postiz", "https://postiz.seudominio.com")],
     "secret": "API_KEY", "kpis": ""},
    {"kind": "telegram", "name": "Telegram do owner", "icon": "✈️", "category": "Avisos", "cost": "Grátis",
     "about": "Os agentes avisam você no Telegram. Por padrão cada aviso pede aprovação; escreva \"sim\" em liberar para dispensar.",
     "fields": [("chat_id", "ID do chat", "123456789"), ("preaprovar", "Liberar avisos sem pedir aprovação", "sim ou não")],
     "secret": "BOT_TOKEN", "kpis": ""},
    {"kind": "email", "name": "E-mail (SMTP próprio)", "icon": "✉️", "category": "E-mail", "cost": "Grátis (o seu SMTP)",
     "about": "Envia e-mails de contato e parceria, um por vez. Cada envio pede aprovação com 2FA.",
     "fields": [("host", "Servidor SMTP", "smtp.seudominio.com"), ("port", "Porta", "587"),
                ("from", "Remetente", "contato@seudominio.com"), ("user", "Usuário (se for outro)", "opcional")],
     "secret": "PASSWORD", "kpis": ""},
    {"kind": "uptime_kuma", "name": "Uptime Kuma", "icon": "🟢", "category": "Infra", "cost": "Open source · self-hosted",
     "about": "Monitora se os sites estão no ar e avisa quando caem.",
     "fields": [("url", "URL do Uptime Kuma", "https://status.seudominio.com"), ("status_page", "Slug da página de status", "projeto")],
     "secret": "", "kpis": "uptime"},
    {"kind": "stripe", "name": "Stripe", "icon": "💳", "category": "Pagamentos", "cost": "Grátis (cobra só por venda)",
     "about": "Receita, MRR e churn direto da conta. Use uma chave restrita só de leitura.",
     "fields": [("account", "Conta", "acct_... ou nome")],
     "secret": "RESTRICTED_KEY", "kpis": "mrr, receita, churn"},
    {"kind": "project_api", "name": "API do próprio projeto", "icon": "🔌", "category": "Projeto", "cost": "Já é seu",
     "about": "Endpoints do próprio produto para os agentes lerem números reais. A coleta diária lê /daily e casa cada evento do catálogo com o KPI de mesmo nome, sem configurar campos.",
     "fields": [("base_url", "URL base", "https://api.seudominio.com"), ("docs", "Onde está a documentação", "/docs ou link"),
                ("kpi_path", "Endpoint da leitura diária (padrão /daily)", "/daily?from={ontem}&to={ontem}")],
     "secret": "TOKEN", "kpis": "eventos do /daily, cadastros, usuarios_ativos"},
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


def _gos_int_help(db: DB, project: GrowthProject, slug: str) -> str:
    from . import actions  # import tardio: actions importa este módulo

    cat = actions.catalog(db, project, slug)
    if not cat:
        return ""
    lines = []
    for c in cat:
        for o in c["operacoes"]:
            lines.append(f"- {c['integracao']} {o['operacao']}({o['parametros']}) [{o['efeito']}]: {o['sobre']}")
    return ("Para usar uma integração pronta, rode no bash (a chave fica com o app, você só recebe o resultado):\n"
            "  gos-int list\n  gos-int run INTEGRAÇÃO OPERAÇÃO '{\"parametro\": valor}'\n"
            "Operações [read] respondem na hora. Operações [external] (publicar, enviar) NUNCA rodam sozinhas: viram um "
            "pedido em Aprovações e você continua o resto do trabalho. Disponíveis agora:\n" + "\n".join(lines))


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
        parts.append(_gos_int_help(db, project, slug))
    return "\n\n".join(parts)


# ---------- automações ----------

EVENTS = {
    "site.down": "Site fora do ar (Uptime Kuma abaixo de 99%)",
    "kpi.updated": "KPI piorou (queda de um indicador que deve subir, alta de um que deve cair)",
    "content.sent": "Peça de conteúdo enviada ou publicada",
    "action.failed": "Ação numa integração falhou",
}
EVENT_MAX_AGE_HOURS = 24  # evento mais velho que isso não dispara rotina (ex.: o app ficou fora do ar)


def describe_schedule(a: Automation) -> str:
    if a.frequency == "event":
        extra = f" · {a.event_filter}" if a.event_filter else ""
        extra += f" · piora de {a.event_min}%+" if a.event_type == "kpi.updated" else ""
        return f"Quando: {EVENTS.get(a.event_type, a.event_type)}{extra}"
    h = f"{a.hour}h"
    if a.frequency == "weekly":
        return f"Toda {WEEKDAYS[a.weekday % 7]} às {h}"
    if a.frequency == "monthly":
        return f"Todo dia {a.monthday} às {h}"
    return f"Todo dia às {h}"


def scheduled_today(a: Automation, today: date) -> bool:
    if a.frequency == "event":
        return False
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


def is_code_report(db: DB, a: Automation) -> bool:
    """Relatório de KPIs feito em código: nasce concluído, então não precisa esperar a execução anterior."""
    skill = db.get(Skill, a.skill_id) if a.skill_id else None
    return skill is not None and skill.template == "relatorio_kpis"


def has_open_run(db: DB, a: Automation) -> bool:
    return db.query(Task).filter(Task.automation_id == a.id, Task.status.in_(OPEN_STATUSES)).count() > 0


def create_run(db: DB, a: Automation, approved: bool, actor: str, context: str = "") -> Task:
    skill = db.get(Skill, a.skill_id) if a.skill_id else None
    if skill is not None and skill.template == "relatorio_kpis":
        return _create_kpi_report(db, a, actor)
    t = Task(
        project_id=a.project_id, agent_slug=a.agent_slug, title=a.title[:200],
        steps=(a.steps + ("\n\n" + context if context else ""))[:4000],
        rationale=f"Atividade rotineira: {describe_schedule(a).lower()}" + (f" · habilidade {skill.name}" if skill else ""),
        status="approved" if approved else "proposed", decided_at=utcnow() if approved else None,
        automation_id=a.id,
    )
    db.add(t)
    db.commit()
    audit(db, actor, "automation_run", f"#{a.id} → tarefa #{t.id} ({t.status}): {a.title}"[:500])
    return t


def _create_kpi_report(db: DB, a: Automation, actor: str) -> Task:
    """O relatório de KPIs é conta, não análise: a tabela sai do código, a tarefa já nasce concluída e não usa IA."""
    from . import growth, kpireport  # import tardio: growth importa este módulo

    now = utcnow()
    t = Task(
        project_id=a.project_id, agent_slug=a.agent_slug, title=a.title[:200], steps=a.steps[:4000],
        rationale=f"Atividade rotineira: {describe_schedule(a).lower()} · calculada em código, sem IA",
        status="done", decided_at=now, finished_at=now, automation_id=a.id,
        result=kpireport.weekly_report(db, a.project_id, growth.local_today()),
    )
    db.add(t)
    db.commit()
    audit(db, actor, "automation_run", f"#{a.id} → relatório #{t.id} (sem IA): {a.title}"[:500])
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


def event_matches(a: Automation, ev: Event, payload: dict) -> bool:
    if a.event_type != ev.type:
        return False
    if a.event_type == "kpi.updated":
        if a.event_filter and payload.get("kpi") != a.event_filter:
            return False
        return float(payload.get("worse_pct", 0)) >= a.event_min
    if a.event_filter and a.event_filter not in (payload.get("integration"), payload.get("site")):
        return False
    return True


def dispatch_events(db: DB) -> list[Task]:
    """Rotinas por evento: cada evento novo do projeto dispara as rotinas que o esperam (propostas, ou na fila se liberadas).

    Mesmas regras das rotinas por horário: nunca empilha duas execuções abertas da mesma rotina."""
    import json
    from datetime import timedelta

    events = db.query(Event).filter(Event.handled == 0).order_by(Event.id).limit(200).all()
    if not events:
        return []
    autos = db.query(Automation).filter(Automation.enabled.is_(True), Automation.frequency == "event").all()
    old = utcnow() - timedelta(hours=EVENT_MAX_AGE_HOURS)
    created = []
    for ev in events:
        ev.handled = 1
        if ev.created_at < old:
            continue
        try:
            payload = json.loads(ev.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        for a in autos:
            if a.project_id != ev.project_id or not event_matches(a, ev, payload):
                continue
            project = db.get(GrowthProject, a.project_id)
            pa = db.query(ProjectAgent).filter(
                ProjectAgent.project_id == a.project_id, ProjectAgent.agent_slug == a.agent_slug
            ).one_or_none()
            if project is None or not project.enabled or pa is None or not pa.enabled or has_open_run(db, a):
                continue
            ctx = f"EVENTO que disparou esta rotina: {ev.type} em {ev.created_at:%d/%m %H:%M} UTC\n{json.dumps(payload, ensure_ascii=False)[:800]}"
            created.append(create_run(db, a, a.auto_approve, "evento", ctx))
    db.commit()
    return created


def automation_json(db: DB, a: Automation, now: datetime, agents: dict[str, Agent] | None = None) -> dict:
    last = db.query(Task).filter(Task.automation_id == a.id).order_by(Task.id.desc()).first()
    skill = db.get(Skill, a.skill_id) if a.skill_id else None
    return {
        "id": a.id, "agent": a.agent_slug, "title": a.title, "steps": a.steps, "skill_id": a.skill_id,
        "skill": skill.name if skill else "", "frequency": a.frequency, "weekday": a.weekday, "monthday": a.monthday,
        "hour": a.hour, "auto_approve": a.auto_approve, "enabled": a.enabled,
        "event_type": a.event_type, "event_filter": a.event_filter, "event_min": a.event_min,
        "schedule": describe_schedule(a), "next_run": next_run(a, now) if a.enabled else "",
        "last_run_on": a.last_run_on.isoformat() if a.last_run_on else None,
        "last_task": {"id": last.id, "status": last.status, "conversation_id": last.conversation_id} if last else None,
    }


def skill_json(s: Skill) -> dict:
    return {"id": s.id, "name": s.name, "description": s.description, "instructions": s.instructions,
            "agents": split_agents(s.agents), "template": s.template, "enabled": s.enabled}
