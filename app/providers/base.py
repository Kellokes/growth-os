import asyncio
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PROMPT = """Você é o Growth OS, o sistema operacional de IA do owner.
Você roda num VPS Linux e ajuda a criar e alterar projetos.
Você NÃO executa nada diretamente. Quando um comando de shell for necessário,
proponha-o num bloco de código ```bash (um comando por bloco, curto e explícito).
O owner revisa e clica para executar; comandos que alteram o sistema pedem 2FA.
Seja direto, responda em português e explique o que cada comando faz."""


@dataclass
class ChatTurn:
    role: str  # user | assistant
    content: str


class ProviderError(RuntimeError):
    pass


def render_transcript(history: list[ChatTurn]) -> str:
    """CLIs recebem um único prompt; a conversa vai como transcrição."""
    lines = []
    for turn in history[:-1]:
        who = "Owner" if turn.role == "user" else "Growth OS"
        lines.append(f"{who}: {turn.content}")
    context = "\n\n".join(lines)
    last = history[-1].content if history else ""
    if context:
        return f"Conversa até agora:\n\n{context}\n\nNova mensagem do owner:\n{last}"
    return last


async def run_cli(args: list[str], cwd: Path, timeout: int, stdin: str | None = None) -> str:
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ProviderError(f"CLI não encontrado: {args[0]}") from e
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except asyncio.TimeoutError as e:
        proc.kill()
        raise ProviderError(f"{args[0]} excedeu {timeout}s") from e
    if proc.returncode != 0:
        msg = (err or out).decode(errors="replace").strip()[-800:]
        raise ProviderError(f"{args[0]} falhou (código {proc.returncode}): {msg}")
    return out.decode(errors="replace")


class Provider:
    id: str = ""
    label: str = ""

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def complete(self, history: list[ChatTurn]) -> str:
        raise NotImplementedError
