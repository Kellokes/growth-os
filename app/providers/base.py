import asyncio
import copy
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
- Você roda num container isolado e sem privilégios. /workspace é a sua pasta de trabalho (gravável).
- Os projetos do owner rodam em outros containers do mesmo VPS, atrás de um Caddy compartilhado. Você os
  enxerga só por estas ferramentas (não existe docker CLI nem socket):
    gos-docker ps [projeto]            containers por projeto, estado e saúde
    gos-docker logs NOME [--tail N]    logs recentes de um container
    gos-docker restart NOME            reinicia; fica pendente até o owner aprovar com 2FA
  Não há exec dentro dos containers nem acesso às pastas deles no servidor (têm segredos).
- O Caddyfile compartilhado está em /projects/caddy/Caddyfile, SÓ LEITURA. Nunca proponha recarregar o Caddy:
  um reload troca a configuração inteira e já derrubou sites. Mudanças no Caddy, o owner faz à mão.

COMO MUDAR CÓDIGO DOS PROJETOS
- Sempre via GitHub: git clone https://github.com/DONO/REPO.git dentro do /workspace, crie uma branch nova,
  faça commit, git push -u origin BRANCH e abra o PR com:
    gos-pr create DONO/REPO --head BRANCH --title "..." --body "..."
  (também: gos-pr list DONO/REPO, gos-pr view DONO/REPO N). O login do git já está configurado.
- Nunca dê push na main/master e nunca faça merge: o owner revisa e faz merge no GitHub, e o deploy do
  projeto segue o fluxo dele. Rode os testes do projeto antes do push quando existirem.
- Se o push ou o PR falharem por falta de acesso, diga ao owner que falta o token do GitHub para aquele repo.

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
    # Escolhidos por atividade/thread (ver app/modelcfg.py). Vazio = padrão da conta/CLI/modelo.
    model: str = ""
    reasoning: str = ""

    def configured(self, model: str = "", reasoning: str = "") -> "Provider":
        """Cópia deste provider com o modelo e o raciocínio escolhidos (o original não muda)."""
        p = copy.copy(self)
        p.model, p.reasoning = model or "", reasoning or ""
        return p

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def complete(self, history: list[ChatTurn]) -> str:
        raise NotImplementedError
