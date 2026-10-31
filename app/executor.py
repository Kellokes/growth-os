"""Executor de comandos com classificação de risco.

Regras (segurança primeiro):
- blocked: padrões destrutivos ou de escalada; nunca rodam.
- safe: comandos somente-leitura de uma lista fechada, sem operadores de shell perigosos.
- approval: todo o resto; exige código 2FA do owner antes de rodar.
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
    "ps",
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
    r"\bcrontab\s+-r\b",
    # Credenciais dos CLIs logados e chaves SSH nunca podem ser lidas por comandos.
    r"\.(claude|codex|ssh)(/|\b)",
    r"\.claude\.json",
]
_SHELL_OPERATORS = re.compile(r"[;&|`$<>]|\$\(")


@dataclass
class Classification:
    risk: str  # safe | approval | blocked
    reason: str


def classify(command: str) -> Classification:
    cmd = command.strip()
    if not cmd:
        return Classification("blocked", "comando vazio")
    if len(cmd) > 4000:
        return Classification("blocked", "comando muito longo")
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, cmd):
            return Classification("blocked", f"padrão bloqueado: {pat}")
    if _SHELL_OPERATORS.search(cmd):
        return Classification("approval", "usa operadores de shell")
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return Classification("approval", "não foi possível analisar o comando")
    prog = parts[0]
    if prog == "git" and len(parts) > 1 and parts[1] in SAFE_GIT_SUBCOMMANDS:
        return Classification("safe", "git somente leitura")
    if prog in SAFE_COMMANDS and prog != "find":
        return Classification("safe", "comando somente leitura")
    if prog == "find" and not any(p in ("-exec", "-execdir", "-delete", "-ok") for p in parts):
        return Classification("safe", "find sem ações")
    return Classification("approval", "pode alterar o sistema")


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
