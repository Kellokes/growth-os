"""Executor de comandos com política de risco em três níveis.

- blocked:  nunca roda (escalada de privilégio, leitura de credenciais, formatação de disco...).
- approval: perigoso ou irreversível; só roda com o código 2FA do owner.
- auto:     todo o resto roda direto, inclusive escrita no /workspace, git comum, pip, testes...
            "safe" é o subconjunto somente-leitura e tem o mesmo tratamento; só muda o rótulo.

A política precisa ser tolerante o bastante para a IA trabalhar sem travar, mas qualquer
comando que apague dados, derrube serviço, reescreva histórico ou toque em segredos cai em approval.
"""

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAFE_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "find", "tree", "du", "df",
    "whoami", "date", "uname", "uptime", "free", "echo", "stat", "file", "which",
    "ps", "sort", "uniq", "cut", "diff", "basename", "dirname",
}
SAFE_GIT_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "remote"}

BLOCKED_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\*|\$HOME)(\s|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+.*\bof=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\b(sudo|su|doas)\b",
    r"\b(curl|wget)\b[^|]*\|\s*(ba|z|da)?sh\b",
    r"\bchmod\s+(-R\s+)?[0-7]*777\s+/",
    r">\s*/dev/sd[a-z]",
    r"\bdocker\.sock\b",
    r"/etc/(shadow|sudoers)",
    r"\bnc\b.*\s-e\s",
    # Credenciais dos CLIs logados, chaves SSH e o ambiente do próprio app.
    r"\.(claude|codex|ssh)(/|\b)",
    r"\.claude\.json",
    r"/proc/[^\s/]*/(environ|mem|maps)",
]

# Rodam só com 2FA: apagam dados, derrubam serviços, reescrevem histórico ou mexem em segredos.
APPROVAL_PATTERNS = [
    (r"\b(rm|rmdir|shred|unlink)\b.*\s-[a-zA-Z]*[rR]|\b(rm|rmdir|shred)\b.*--recursive|\bshred\b|\btruncate\b", "apaga ou sobrescreve dados"),
    (r"\bfind\b.*(-delete|-exec\b|-execdir\b)", "find com ação destrutiva"),
    (r"\bgit\s+push\b.*(--force|-f\b|--force-with-lease|--delete|\s:\w)", "force push ou apagar branch remota"),
    (r"\bgit\s+push\b.*\b(main|master|production|prod)\b", "push para a branch principal"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-\w*f|branch\s+-D|checkout\s+--\s|stash\s+(drop|clear))", "descarta trabalho no git"),
    (r"\bdocker\s+(compose\s+)?(rm|rmi|stop|kill|restart|down|prune|exec|run|up|build|pull|cp)\b", "altera containers"),
    (r"\bdocker\s+(volume|network|system|image|container)\s+(rm|prune|remove)\b", "remove recursos do Docker"),
    (r"\b(drop|truncate)\s+(table|database|schema)\b|\bdelete\s+from\b|\balter\s+table\b", "altera ou apaga dados do banco"),
    (r"\b(kill|pkill|killall)\b", "encerra processos"),
    (r"\b(chmod|chown|chgrp)\s+(-\w*R|--recursive)", "muda permissões recursivamente"),
    (r"\b(systemctl|service|crontab|at)\b", "altera serviços ou agendamentos"),
    (r"(^|[\s/\"'=])\.env(\.|\b)|\bprintenv\b|\bsecrets?\b.*\b(cat|echo|print)\b", "toca em variáveis ou segredos"),
    (r"\|\s*(ba|z|da)?sh\b|\b(eval|exec)\b|\bbase64\s+(-d|--decode)", "executa código montado dinamicamente"),
    (r">\s*/(etc|usr|bin|sbin|lib|var|opt|root|home|boot|data)\b", "escreve fora do /workspace"),
    (r"\b(npm|pnpm|yarn)\s+(publish|unpublish)\b|\bpip\s+uninstall\b|\b(apt|apt-get|apk|yum|dnf)\b", "instala/remove pacotes do sistema ou publica"),
]
_APPROVAL_COMPILED = [(re.compile(p, re.I), why) for p, why in APPROVAL_PATTERNS]


@dataclass
class Classification:
    risk: str  # safe | auto | approval | blocked
    reason: str


def classify(command: str) -> Classification:
    cmd = command.strip()
    if not cmd:
        return Classification("blocked", "comando vazio")
    if len(cmd) > 8000:
        return Classification("blocked", "comando muito longo")
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, cmd):
            return Classification("blocked", f"padrão bloqueado: {pat}")
    for rx, why in _APPROVAL_COMPILED:
        if rx.search(cmd):
            return Classification("approval", why)
    # Somente-leitura "puro": um comando conhecido, sem operadores de shell.
    if not re.search(r"[;&|`$<>]|\$\(", cmd):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return Classification("auto", "comando comum")
        if parts[0] == "git" and len(parts) > 1 and parts[1] in SAFE_GIT_SUBCOMMANDS:
            return Classification("safe", "git somente leitura")
        if parts[0] in SAFE_COMMANDS:
            return Classification("safe", "comando somente leitura")
    return Classification("auto", "comando comum")


# Variáveis que nunca vão para os comandos executados.
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASS|CREDENTIAL)", re.I)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GOS_") and not _SECRET_ENV.search(k)}


@dataclass
class RunResult:
    exit_code: int
    output: str


def run_command(command: str, workdir: Path, timeout: int, max_bytes: int) -> RunResult:
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=workdir,
            env=_clean_env(),
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return RunResult(124, f"[tempo limite de {timeout}s excedido]")
    out = proc.stdout + (b"\n" + proc.stderr if proc.stderr else b"")
    text = out[:max_bytes].decode("utf-8", errors="replace")
    if len(out) > max_bytes:
        text += "\n[saída truncada]"
    return RunResult(proc.returncode, text)
