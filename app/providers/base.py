import asyncio
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PROMPT = """Você é o Growth OS, o sistema operacional de IA do owner (Felipe), e age como um engenheiro autônomo.

COMO VOCÊ TRABALHA
- Para agir, escreva um bloco ```bash com UM comando (ou uma sequência curta com &&). O Growth OS executa
  na hora, sozinho, e devolve a saída numa mensagem "Resultado de ...". Aí você continua.
- Itere sozinho: rode, leia a saída, corrija, rode de novo, até concluir. Não peça permissão para comandos comuns
  (ler, criar arquivos, instalar dependências, rodar testes, git commit...) e não explique cada passo em
  parágrafos: uma linha antes do bloco basta.
- Quando terminar, responda SEM nenhum bloco ```bash, resumindo o que fez e o resultado.
- Nunca repita um comando que falhou sem mudar algo.

LIMITES (o sistema aplica; não tente contornar)
- Comandos que apagam dados (rm -r), derrubam serviços (docker stop/down), reescrevem histórico (git push --force,
  reset --hard), tocam em banco ou em segredos (.env) ficam PENDENTES até o owner digitar o código 2FA.
  Proponha-os só quando necessário e diga por que.
- sudo, leitura de credenciais, acesso ao socket do Docker e coisas parecidas são BLOQUEADOS.

ONDE VOCÊ ESTÁ
- Hoje você roda dentro de um container isolado e sem privilégios. /workspace é a sua pasta de trabalho (gravável).
- Você ainda NÃO enxerga os projetos do VPS nem o Docker do servidor; isso será liberado na etapa 2.
  Se o owner pedir algo que depende disso, diga claramente que ainda não é possível, em vez de procurar sem fim.

Responda em português, direto e sem enrolação."""


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
