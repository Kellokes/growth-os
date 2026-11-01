# Growth OS — Plano

Sistema operacional de IA para administrar empresas digitais a partir de um chat,
com agentes especialistas que trabalham sozinhos, mas só executam o que o owner aprova.

**Premissas:** um único usuário (o owner), segurança em primeiro lugar, Python,
roda em Docker no VPS da Hostinger atrás do Caddy que já está lá.

## Etapa 1 — Base segura + chat

- App FastAPI em Docker, exposto pelo Caddy existente (HTTPS automático).
- Login só do owner: senha (argon2) + 2FA TOTP, bloqueio após tentativas erradas,
  sessão em cookie `HttpOnly`/`Secure`/`SameSite=Strict`, proteção CSRF, CSP restritiva.
- Chat com seletor de provider:
  - **Claude** via Claude Code CLI logado com a conta do owner (`claude login`).
  - **Codex** via Codex CLI logado com a conta do owner (`codex login`).
  - **OpenRouter** via API key (para outras atividades).
- A IA propõe comandos; o owner clica para executar. Comandos de leitura rodam direto,
  qualquer outro pede código 2FA, e padrões perigosos são bloqueados.
- Log de auditoria de tudo (login, chat, comando, aprovação).
- Nesta etapa o OS fica isolado: sem acesso ao socket do Docker nem aos 3 projetos.

## Etapa 2 — Gestão dos projetos (feita)

- Tela **Projetos**: containers agrupados por projeto do compose, saúde, domínios do Caddy com status HTTP,
  logs e restart com 2FA.
- Docker só por um **gateway próprio** (`gateway/`): lista sem variáveis de ambiente, logs e restart com
  token que só o app tem. Sem exec, sem criar/remover containers, sem acesso a arquivos.
- Caddyfile montado só leitura; a IA nunca recarrega o Caddy (mudanças nele são manuais, com o procedimento seguro).
- Código dos projetos via GitHub: a IA clona no /workspace, cria branch, dá push e abre PR (`gos-pr`) com um
  token fine-grained só dos repos dos projetos. Nunca push na main, nunca merge.

## Etapa 3 — Agentes por área + plano da manhã + dashboard (feita, tudo POR PROJETO)

- Cada projeto (TakeOne, Zombie Party, A Ilha, novos) tem o seu time de agentes, os seus KPIs e as suas
  tarefas. Os projetos vêm do VPS (projetos do compose) ou são criados à mão; o seletor de projeto fica
  sempre visível no topo do Dashboard, do Plano do dia e de Agentes.

- 9 agentes (Growth, CRM, Marketing, Social, Design, Produto, Tech, CS, Financeiro), cada um com missão
  editável, liga/desliga e provider próprio (tela **Agentes**).
- Todo dia às `GOS_PLAN_HOUR` (fuso `GOS_TIMEZONE`) cada agente ativo lê os KPIs e as tarefas recentes e
  propõe até 3 tarefas. Aviso opcional no Telegram.
- Tela **Plano do dia**: aprovar/rejeitar item por item ou tudo; tarefa avulsa para um agente.
  Só tarefa aprovada roda, uma por vez, no laço normal da IA (comando perigoso continua pedindo 2FA).
- **Dashboard**: KPIs por área com tendência, variação, meta, gráfico interativo e visão de tabela,
  mais o estado da operação (plano, 2FA pendente, containers). Números entram à mão ou pelos agentes
  (bloco ```kpi```); as fontes automáticas chegam na etapa 4.

## Etapa 4 — Integrações de growth

- APIs das redes sociais e plataformas de anúncios (Meta, Google Ads, TikTok, etc.).
- Monitoramento de concorrentes e de posts/campanhas que estão funcionando.
- APIs dos próprios projetos (pedidos, uso, logs de clientes) alimentando agentes e dashboard.
