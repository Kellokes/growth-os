# Growth OS

Sistema operacional de IA para empresas digitais: um chat onde você escolhe o provider
(Claude, Codex ou OpenRouter), pede o que precisa no VPS e aprova o que a IA vai executar.
O plano completo está em [docs/PLAN.md](docs/PLAN.md).

**Status:** etapa 1 (base segura + chat).

## Segurança

- Usuário único (o owner). Login com senha argon2 **e** código 2FA TOTP.
- Bloqueio por 15 min após 5 tentativas erradas; códigos 2FA não podem ser reutilizados.
- Sessão no servidor, cookie `HttpOnly` + `Secure` + `SameSite=Strict`, token CSRF em toda escrita.
- CSP restritiva, sem scripts de terceiros, sem `/docs` da API.
- A IA trabalha em laço (até 8 passos por pedido): propõe um comando, o Growth OS executa, devolve a saída e ela segue.
  - **Roda direto:** leitura, criar/editar arquivos, instalar dependências, testes, `git commit`, `docker ps/logs`…
  - **Pede seu 2FA e pausa:** `rm -r`, `git push --force`/para a `main`, `docker stop/down/up`, SQL destrutivo, `kill`, `.env`…
  - **Bloqueado sempre:** `sudo`, `curl | sh`, leitura de credenciais, socket do Docker, `/proc/*/environ`…
- Comandos rodam sem as variáveis secretas do app, com tempo limite e saída limitada, e o processo do app
  é marcado como não-inspecionável (`PR_SET_DUMPABLE=0`), então eles também não leem `/proc/<pid>/environ`.
- Container sem privilégios (`cap_drop: ALL`, `no-new-privileges`, raiz somente leitura),
  sem acesso ao socket do Docker e publicado só em `127.0.0.1` para o Caddy.
- Tudo vai para o log de auditoria (botão **Auditoria** na barra lateral).

## Instalação no VPS

```bash
git clone https://github.com/Kellokes/growth-os.git && cd growth-os
cp .env.example .env            # ajuste os valores, inclusive GOS_POSTGRES_PASSWORD
docker compose up -d --build    # sobe o Postgres e o app

# Crie o owner e cadastre o 2FA no seu app autenticador
docker compose exec growth-os python -m app.cli create-user felipe

# Login dos providers com a SUA conta (OAuth oficial de cada um)
docker compose exec -it -w /workspace growth-os claude login
docker compose exec -it -w /workspace growth-os codex login --device-auth
```

Depois adicione o bloco de [deploy/Caddyfile.snippet](deploy/Caddyfile.snippet) ao Caddyfile
do VPS (troque o domínio) e rode `caddy reload`. Se o Caddy roda em container, conecte-o à
rede do `growth-os` e use `reverse_proxy growth-os:8000` no lugar de `127.0.0.1:8100`.

Recuperação: `python -m app.cli reset-password felipe` e `python -m app.cli reset-2fa felipe`.

## Projetos do VPS (etapa 2)

- `docker-gateway` é o único container com o socket do Docker. Ele só lista containers (sem `Env`, mounts ou
  labels), lê logs e reinicia com o token `GOS_GATEWAY_TOKEN`, que o `deploy.sh` gera sozinho no `.env`.
- O app monta só o Caddyfile compartilhado (`GOS_CADDYFILE_HOST`, padrão `/opt/takeone/Caddyfile`), em modo
  leitura. `GOS_CADDY_VARS` resolve `{$DOMAIN}`; o `deploy.sh` copia os valores do container do Caddy.
- A IA usa `gos-docker ps|logs|restart` (restart pede 2FA e o app executa pelo gateway) e `gos-pr` para abrir PRs.
- GitHub: crie um token *fine-grained* só com os repos dos projetos, permissões **Contents** e
  **Pull requests** em leitura e escrita, e coloque em `GOS_GITHUB_TOKEN` no `.env`. Ele fica num arquivo do
  tmpfs para o credential helper do git; comandos não recebem o token no ambiente e leitura direta é bloqueada.
  Mesmo assim trate-o como acessível à IA: o escopo pequeno e a proteção da `main` nos repos são o limite real.

## Agentes e plano da manhã (etapa 3)

- Tudo é por projeto: cada projeto tem o seu time (9 áreas), os seus KPIs e as suas tarefas. Na primeira
  visita os projetos do compose do VPS são importados sozinhos; dá para criar outros em Agentes.

- Os agentes do mesmo projeto trabalham em time. Ao terminar uma tarefa, um agente pode pedir trabalho a
  outro com um bloco ```pedido``` (vira tarefa nova que **também** espera a sua aprovação) ou deixar um
  ```recado```. Quem pediu recebe a resposta sozinho quando a tarefa termina (ou quando você rejeita).
  Limites: até 3 pedidos por tarefa, corrente de no máximo 3 níveis, nunca entre projetos. A conversa
  aparece em Agentes, em "Conversa do time".

- O agendador roda dentro do app e gera o plano uma vez por dia a partir de `GOS_PLAN_HOUR`
  (padrão 7h, fuso `GOS_TIMEZONE`). O botão "Gerar plano" faz o mesmo na hora.
- Modelos (ícone de controles no topo, `/settings`): para cada atividade (Padrão, Threads, Agentes · plano,
  Agentes · execução, Imagens, Vídeos, Áudio) você escolhe provider, modelo e nível de raciocínio. Modelo
  vazio = padrão da conta (Claude/Codex) ou `GOS_OPENROUTER_MODEL`. Threads e cada agente podem trocar na
  hora; a thread lembra a escolha. Claude recebe `--model`/`--effort`, Codex `--model` e
  `-c model_reasoning_effort=...`, OpenRouter `model` e `reasoning.effort`.
- Provider dos agentes: o escolhido no agente, senão a regra de Modelos da atividade, senão
  `GOS_AGENT_PROVIDER` (padrão `claude`); se não estiver disponível, usa o primeiro que estiver.
- Aviso no Telegram (opcional): crie um bot com o @BotFather, mande uma mensagem para ele e preencha
  `GOS_TELEGRAM_BOT_TOKEN` e `GOS_TELEGRAM_CHAT_ID`. O bot só envia; não aceita comandos.
- `GOS_PUBLIC_URL` (ex.: `https://os.takeonestudio.io`) coloca o link do plano no aviso.

## Kanban, habilidades, integrações e rotinas

- **Kanban** (`/board`): cada atividade dos agentes numa coluna da etapa em que está (aguardando você, na fila,
  em execução, esperando 2FA, concluída, falhou/rejeitada), por projeto ou todos. Arrastar uma proposta para
  "Na fila" aprova; uma falha arrastada para "Na fila" (ou "Tentar de novo") roda de novo.
- **Automações** (`/automations`), sempre do projeto escolhido:
  - Habilidades: playbooks com os passos que os agentes marcados seguem. Entram no prompt do plano e da
    execução. Há uma biblioteca com modelos prontos.
  - Integrações: catálogo grátis/self-hosted primeiro (Umami, Plausible, Search Console, Meta Graph API,
    Postiz, Uptime Kuma, Stripe, API do projeto). A tela guarda só a configuração pública. A chave vai no
    `.env` do VPS no nome que a tela mostra (ex.: `GOS_INT_ZOMBIE_PARTY_UMAMI_API_KEY`) e o app precisa
    reiniciar para ler. Comandos da IA não recebem variáveis `GOS_*`.
  - Rotinas: atividades que se repetem (todo dia, semana ou mês, numa hora). Na hora marcada viram tarefa
    do agente. Por padrão entram como proposta e esperam aprovação; "Rodar sem pedir aprovação" coloca
    direto na fila (comandos perigosos continuam pedindo 2FA). Não empilham: se a anterior ainda está
    aberta, a rodada é pulada.

## Central de conteúdo

`/content`, por projeto ou todos. Junta tudo que sai para fora e o que os agentes entregam:

- **Peças**: posts (Instagram, TikTok, LinkedIn, X...), e-mails de contato e parceria, artigos. Os agentes
  entregam cada peça num bloco ```` ```conteudo ```` no fim da tarefa (o formato está no prompt da execução).
  Toda peça de agente nasce "aguardando você", inclusive as de rotinas com auto-aprovação. Aprovada, ela fica
  agendada (com data) ou pronta para sair. Até as integrações de envio existirem (etapa 4), o app não publica
  nem envia nada: você copia o texto, publica e marca como enviada (com o link, se quiser).
- **Agenda**: peças com data e as próximas execuções das rotinas, dia a dia (hora de Brasília).
- **Saídas**: o resultado das tarefas concluídas ou que falharam, com as peças que cada uma gerou.

## Banco de dados

Postgres 16 em container próprio (`growth-os-postgres`), sem porta publicada: só o app alcança, pela rede
interna do Docker. Os dados ficam no volume `growth-os_gos-pgdata`. A senha vem de `GOS_POSTGRES_PASSWORD` no `.env`.

**Migrando de uma instalação antiga em SQLite** (uma vez, antes de usar o app novo):

```bash
# 1. Gere a senha e grave no .env do VPS
echo "GOS_POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
# 2. Suba o Postgres e o app novo, depois copie os dados do SQLite antigo
docker compose up -d --build
docker compose exec growth-os python -m app.cli migrate-sqlite /data/growth_os.db
```

O comando se recusa a rodar se o destino já tiver dados, então não duplica nada.
O SQLite antigo continua no volume `gos-data` como backup.

Rodar os testes no Postgres: `TEST_DATABASE_URL=postgresql+psycopg://usuario@host/banco pytest`.

## Deploy automático

A cada merge na `main`, o GitHub Actions ([deploy.yml](.github/workflows/deploy.yml)) roda os testes
e, se passarem, conecta no VPS por SSH e executa `scripts/deploy.sh`: atualiza o código, reconstrói o
container, espera ficar `healthy` e volta para a versão anterior sozinho se não ficar.

Configuração (uma vez):

1. No VPS: `cd ~/growth-os && git pull && ./scripts/setup-deploy-key.sh`
2. No GitHub, em Settings > Secrets and variables > Actions, crie os 4 secrets que o script mostra:
   `VPS_HOST`, `VPS_USER`, `VPS_KNOWN_HOSTS` e `VPS_SSH_KEY`. Depois apague a chave privada do VPS.
3. Em Settings > Environments, crie o ambiente `production` (opcional: exigir sua aprovação a cada deploy).
4. Em Settings > Branches, proteja a `main` exigindo PR aprovado e o check `test`.

A chave do deploy só executa o `deploy.sh` (`command=` fixo, sem terminal e sem túnel); se vazar, não dá shell no VPS.

## Desenvolvimento

```bash
pip install -e ".[dev]"
GOS_COOKIE_SECURE=false uvicorn app.main:app --reload
pytest
```

## Estrutura

```
app/
  main.py          rotas (páginas, chat, comandos, auditoria)
  growth.py        agentes, plano do dia, fila de tarefas e agendador
  routines.py      habilidades, integrações e rotinas (automações)
  security.py      senha, 2FA, sessões, CSRF
  executor.py      classificação de risco e execução de comandos
  providers/       Claude CLI, Codex CLI, OpenRouter
  templates/, static/   interface
deploy/            Caddy
docs/PLAN.md       plano das 4 etapas
```
