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
  security.py      senha, 2FA, sessões, CSRF
  executor.py      classificação de risco e execução de comandos
  providers/       Claude CLI, Codex CLI, OpenRouter
  templates/, static/   interface
deploy/            Caddy
docs/PLAN.md       plano das 4 etapas
```
