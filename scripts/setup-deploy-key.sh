#!/usr/bin/env bash
# Rode UMA vez no VPS. Cria uma chave SSH que só consegue executar o deploy.sh
# (command= fixo, sem terminal, sem túnel) e mostra os valores para os secrets do GitHub.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/growth_os_deploy"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys"

if [ -e "$KEY" ]; then
  echo "A chave $KEY já existe. Apague-a antes (e a linha dela em authorized_keys) para gerar outra." >&2
  exit 1
fi

ssh-keygen -q -t ed25519 -N "" -C "growth-os-deploy" -f "$KEY"
echo "command=\"$REPO_DIR/scripts/deploy.sh\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $(cat "$KEY.pub")" >> "$HOME/.ssh/authorized_keys"

HOST_IP="$(curl -4 -s ifconfig.me)"
echo
echo "=== Secrets do GitHub (Settings > Secrets and variables > Actions) ==="
echo "VPS_HOST       = $HOST_IP"
echo "VPS_USER       = $(whoami)"
echo
echo "VPS_KNOWN_HOSTS ="
ssh-keyscan -t ed25519 "$HOST_IP" 2>/dev/null
echo
echo "VPS_SSH_KEY ="
cat "$KEY"
echo
echo "Depois de salvar os secrets no GitHub, apague a chave privada daqui:"
echo "  shred -u $KEY"
