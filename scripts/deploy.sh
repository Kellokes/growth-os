#!/usr/bin/env bash
# Deploy do Growth OS no VPS. Chamado pelo GitHub Actions via SSH (chave restrita)
# ou manualmente: ~/growth-os/scripts/deploy.sh
set -euo pipefail

# Tudo dentro de main(): o bash lê a função inteira antes de executar, então o
# "git merge" que atualiza este próprio arquivo não corrompe a execução em andamento.
# Etapa 2: prepara sozinho o que o compose precisa, sem sobrescrever nada que já exista.
ensure_stage2() {
  [ -n "$(tail -c1 .env)" ] && echo >> .env  # garante quebra de linha antes de acrescentar
  if ! grep -q '^GOS_GATEWAY_TOKEN=.\+' .env; then
    echo "GOS_GATEWAY_TOKEN=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')" >> .env
    echo "==> Token do gateway do Docker gerado no .env"
    ENV_CHANGED=1
  fi
  if ! grep -q '^GOS_CADDY_VARS=' .env; then
    caddyfile="${GOS_CADDYFILE_HOST:-/opt/takeone/Caddyfile}"
    caddy="${GOS_CADDY_CONTAINER:-takeone-caddy-1}"
    vars=""
    if [ -f "$caddyfile" ]; then
      for name in $(grep -o '{\$[A-Za-z_][A-Za-z0-9_]*' "$caddyfile" | cut -c3- | sort -u); do
        case "$name" in *KEY*|*SECRET*|*TOKEN*|*PASS*|*HASH*) continue ;; esac
        val="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$caddy" 2>/dev/null | grep "^$name=" | head -1 | cut -d= -f2-)"
        [ -n "$val" ] && vars="$vars${vars:+,}$name=$val"
      done
    fi
    echo "GOS_CADDY_VARS=$vars" >> .env
    echo "==> Variáveis do Caddyfile: ${vars:-nenhuma}"
    ENV_CHANGED=1
  fi
}

main() {
  cd "$(dirname "$0")/.."
  BRANCH="${DEPLOY_BRANCH:-main}"

  if ! grep -q '^GOS_POSTGRES_PASSWORD=.\+' .env 2>/dev/null; then
    echo "!! Falta GOS_POSTGRES_PASSWORD no .env do VPS. Deploy cancelado (nada foi alterado)." >&2
    exit 1
  fi

  ENV_CHANGED=0
  ensure_stage2

  PREV="$(git rev-parse HEAD)"
  echo "==> Atualizando para origin/$BRANCH (atual: ${PREV:0:7})"
  git fetch --quiet origin "$BRANCH"
  git checkout --quiet "$BRANCH"
  git merge --ff-only "origin/$BRANCH"
  NEW="$(git rev-parse HEAD)"

  if [ "$PREV" = "$NEW" ] && [ "$ENV_CHANGED" = 0 ] && docker compose ps --status running --quiet growth-os | grep -q .; then
    echo "==> Nada novo e o container já está rodando. Fim."
    exit 0
  fi

  echo "==> Build e subida (${NEW:0:7})"
  docker compose up -d --build

  wait_healthy() {
    for _ in $(seq 1 30); do
      status="$(docker inspect --format '{{.State.Health.Status}}' growth-os 2>/dev/null || echo starting)"
      [ "$status" = "healthy" ] && return 0
      sleep 3
    done
    return 1
  }

  if wait_healthy; then
    echo "==> Deploy ok: ${NEW:0:7} está saudável"
    docker image prune -f >/dev/null
    exit 0
  fi

  echo "!! Container não ficou saudável. Voltando para ${PREV:0:7}" >&2
  docker compose logs --tail 40 growth-os >&2 || true
  git reset --hard "$PREV"
  docker compose up -d --build
  wait_healthy && echo "==> Rollback ok" >&2 || echo "!! Rollback também falhou, verifique o VPS" >&2
  exit 1
}

main "$@"
exit $?
