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
- A IA nunca executa nada sozinha: ela propõe comandos e você clica em **Executar**.
  - Leitura (`ls`, `cat`, `git status`…) roda direto.
  - Qualquer outro comando pede seu código 2FA.
  - Padrões destrutivos (`rm -rf /`, `sudo`, `curl | sh`, leitura de credenciais, socket do Docker…) são bloqueados.
- Comandos rodam sem as variáveis secretas do app, com tempo limite e saída limitada.
- Container sem privilégios (`cap_drop: ALL`, `no-new-privileges`, raiz somente leitura),
  sem acesso ao socket do Docker e publicado só em `127.0.0.1` para o Caddy.
- Tudo vai para o log de auditoria (botão **Auditoria** na barra lateral).

## Instalação no VPS

```bash
git clone https://github.com/Kellokes/growth-os.git && cd growth-os
cp .env.example .env            # ajuste os valores
docker compose up -d --build

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
