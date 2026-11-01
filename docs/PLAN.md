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

## Etapa 3 — Agentes por área + plan approval matinal + dashboard

- Um agente por área da empresa digital, cada um com sua responsabilidade e skills:
  CRM, Growth, Marketing (pago e orgânico), Social, Design, Produto, Tech, Suporte/CS, Financeiro.
- Agendador roda os agentes continuamente; cada agente gera **propostas** numa fila.
- Todo dia de manhã: notificação (Telegram ou e-mail) com o plano do dia; o owner aprova
  ou rejeita item por item; só o aprovado é executado e o resultado volta para o dashboard.
- **Dashboard de growth** com indicadores reais por projeto (aquisição, ativação, receita,
  retenção, engajamento nas redes, campanhas). Os mesmos indicadores são a entrada diária
  dos agentes para decidir o que propor.

## Etapa 4 — Integrações de growth

- APIs das redes sociais e plataformas de anúncios (Meta, Google Ads, TikTok, etc.).
- Monitoramento de concorrentes e de posts/campanhas que estão funcionando.
- APIs dos próprios projetos (pedidos, uso, logs de clientes) alimentando agentes e dashboard.
