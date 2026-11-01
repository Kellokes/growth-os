FROM python:3.12-slim

# Node é necessário para os CLIs oficiais do Claude Code e do Codex.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git nodejs npm \
 && npm install -g @anthropic-ai/claude-code @openai/codex \
 && npm cache clean --force \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Ferramentas da etapa 2: Docker pelo gateway, git com token restrito e PRs.
COPY scripts/gos-docker scripts/gos-pr scripts/gos-git-credential scripts/gos-self /usr/local/bin/
RUN chmod 755 /usr/local/bin/gos-docker /usr/local/bin/gos-pr /usr/local/bin/gos-git-credential /usr/local/bin/gos-self \
 && git config --system credential.https://github.com.helper /usr/local/bin/gos-git-credential \
 && git config --system user.name "Growth OS" \
 && git config --system user.email "growth-os@users.noreply.github.com" \
 && git config --system init.defaultBranch main

RUN useradd --create-home --uid 10001 gos
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
# pytest entra para a IA rodar os testes da cópia do próprio código (gos-self test) antes de abrir PR.
RUN pip install --no-cache-dir . "pytest>=8"

# Repositório e versão do próprio Growth OS (o deploy.sh passa o commit). Sem prefixo GOS_ de
# propósito: o executor tira as variáveis GOS_ dos comandos, e estas não são segredo.
ARG GROWTH_OS_COMMIT=""
ENV GROWTH_OS_REPO=Kellokes/growth-os \
    GROWTH_OS_COMMIT=${GROWTH_OS_COMMIT}

# Os volumes nomeados herdam o dono destas pastas; sem isso /data fica do root
# e o app (usuário gos) não consegue criar o banco e reinicia em loop.
RUN mkdir -p /data /workspace && chown -R gos:gos /data /workspace /home/gos

USER gos
ENV GOS_DATABASE_URL=sqlite:////data/growth_os.db \
    GOS_WORKSPACE_DIR=/workspace \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${GOS_TRUSTED_PROXIES:-127.0.0.1}\""]
