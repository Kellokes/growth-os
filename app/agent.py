"""Laço do agente: a IA propõe comandos, o Growth OS executa conforme a política e devolve a saída.

Cada passo: provider responde -> blocos ```bash são extraídos -> cada um é classificado:
  safe/auto  -> roda direto e a saída volta para a IA
  approval   -> o laço PAUSA e espera o 2FA do owner (retomado por /continue)
  blocked    -> não roda; a IA recebe o aviso e escolhe outro caminho
O laço para quando a resposta não tem mais comandos, ao pausar por aprovação ou no limite de passos.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from .audit import audit
from . import actions
from .config import get_settings
from .projects import GatewayError, restart_container, restart_target
from .db import CommandRun, Message
from .executor import classify, run_command
from .providers import ChatTurn, Provider, ProviderError

HEARTBEAT_SECONDS = 15  # linha "ping" no stream enquanto a IA pensa: proxies não cortam a conexão parada
KEEP_FULL_TOOL_RESULTS = 6  # saídas de comando mais antigas que isso vão resumidas para a IA
OLD_TOOL_CHARS = 1500
_BLOCK = re.compile(r"```(?:bash|sh|shell|zsh|console)[ \t]*\n(.*?)```", re.S)


def extract_commands(text: str) -> list[str]:
    return [m.group(1).strip() for m in _BLOCK.finditer(text) if m.group(1).strip()]


def cmd_json(r: CommandRun) -> dict:
    return {
        "id": r.id, "command": r.command, "risk": r.risk, "status": r.status,
        "exit_code": r.exit_code, "output": r.output,
    }


def tool_message(run: CommandRun, after_pause: bool = False) -> str:
    if run.status == "blocked":
        body = f"O comando foi BLOQUEADO pela política de segurança ({run.output}). Escolha outra abordagem."
    elif run.status == "rejected":
        body = "O owner REJEITOU este comando. Não tente o mesmo de novo; pergunte o que ele prefere."
    else:
        body = f"Código de saída: {run.exit_code}\n```\n{run.output[-6000:]}\n```"
    msg = f"Resultado de `{run.command[:300]}`:\n{body}"
    if after_pause:
        msg += "\n(Se você propôs mais comandos depois deste, eles NÃO foram executados; proponha-os de novo se ainda forem necessários.)"
    return msg


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n[... saída antiga cortada para caber no contexto ...]\n" + text[-limit // 2:]


def build_history(db: DB, cid: int, budget: int | None = None) -> list[ChatTurn]:
    """Conversa que vai para a IA, cabendo num orçamento de caracteres.

    Mantém sempre o pedido original (primeira mensagem do owner) e as mensagens mais recentes; saídas de
    comando antigas vão resumidas. Sem isso, um laço longo cresce até estourar o contexto do modelo
    (o OpenRouter responde 400/502 e a thread trava no "continue").
    """
    budget = budget or get_settings().agent_history_chars
    rows = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id).all()
    turns = [ChatTurn("assistant" if m.role == "assistant" else "user", m.content) for m in rows]
    tool_idx = [i for i, m in enumerate(rows) if m.role == "tool"]
    for i in tool_idx[:-KEEP_FULL_TOOL_RESULTS]:
        turns[i] = ChatTurn("user", _shorten(turns[i].content, OLD_TOOL_CHARS))
    if not turns:
        return turns

    # Pedido original fica sempre; depois dele, as mensagens mais recentes que couberem.
    first = next((i for i, m in enumerate(rows) if m.role == "user"), 0)
    head, rest = turns[first], turns[first + 1:]
    used, tail = len(head.content), []
    for t in reversed(rest):
        if tail and used + len(t.content) > budget:
            break
        if not tail and used + len(t.content) > budget:  # nem a última cabe sozinha: vai cortada
            t = ChatTurn(t.role, _shorten(t.content, max(budget - used, 2000)))
        tail.append(t)
        used += len(t.content)
    tail.reverse()
    omitted = len(rest) - len(tail)
    if omitted:
        note = f"[{omitted} mensagens antigas desta thread foram omitidas para caber no contexto do modelo]"
        return [head, ChatTurn("user", note), *tail]
    return [head, *tail]


async def execute_run(db: DB, run: CommandRun) -> None:
    s = get_settings()
    target = restart_target(run.command)
    if target:  # só chega aqui depois do 2FA: restart não é safe/auto
        try:
            await restart_container(target)
            run.exit_code, run.output = 0, f"container {target} reiniciado pelo gateway"
        except GatewayError as e:
            run.exit_code, run.output = 1, str(e)
        run.status = "done" if run.exit_code == 0 else "failed"
        db.commit()
        return
    ticket = actions.ticket_for_conversation(run.conversation_id)  # só tarefas de agente em execução têm ticket
    result = await run_in_threadpool(
        run_command, run.command, Path(s.workspace_dir), s.command_timeout_seconds, s.command_max_output_bytes,
        {"TASK_TICKET": ticket} if ticket else None,
    )
    run.exit_code = result.exit_code
    run.output = result.output
    run.status = "done" if result.exit_code == 0 else "failed"
    db.commit()


async def _complete(provider: Provider, history: list[ChatTurn]) -> AsyncIterator[dict | str]:
    """Chama o provider; enquanto ele pensa, solta {"type": "ping"}. O último item é o texto da resposta."""
    task = asyncio.ensure_future(provider.complete(history))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
            if done:
                break
            yield {"type": "ping"}
        yield task.result()
    finally:
        if not task.done():
            task.cancel()


async def run_agent(db: DB, cid: int, provider: Provider, actor: str, ip: str) -> AsyncIterator[dict]:
    """Gera eventos para a interface: step, ping, message, command, approval, notice, done, error."""
    max_steps = max(1, get_settings().agent_max_steps)
    for step in range(max_steps):
        yield {"type": "step", "n": step + 1, "provider": provider.id}
        reply = ""
        try:
            async for item in _complete(provider, build_history(db, cid)):
                if isinstance(item, dict):
                    yield item
                else:
                    reply = item
        except ProviderError as e:
            audit(db, actor, "chat_error", f"{provider.id}: {e}", ip)
            yield {"type": "error", "message": str(e), "retry": e.retryable}
            return
        except Exception as e:  # noqa: BLE001 - erro inesperado do provider (rede etc.): avisa e deixa tentar de novo
            audit(db, actor, "chat_error", f"{provider.id}: {type(e).__name__}", ip)
            yield {"type": "error", "message": f"{provider.id} falhou ({type(e).__name__}). Tente de novo.", "retry": True}
            return
        m = Message(conversation_id=cid, role="assistant", provider=provider.id, model=provider.model,
                    content=reply or "(sem resposta)")
        db.add(m)
        db.commit()
        yield {"type": "message", "id": m.id, "provider": provider.id, "model": provider.model, "content": m.content}

        commands = extract_commands(reply)
        if not commands:
            yield {"type": "done"}
            return

        results: list[str] = []
        paused = False
        for command in commands:
            cls = classify(command)
            status = {"safe": "running", "auto": "running", "approval": "pending", "blocked": "blocked"}[cls.risk]
            run = CommandRun(
                conversation_id=cid, command=command, risk=cls.risk, status=status,
                output=cls.reason if cls.risk == "blocked" else "",
            )
            db.add(run)
            db.commit()
            audit(db, actor, f"command_{cls.risk}", command, ip)

            if cls.risk == "approval":
                yield {"type": "approval", "run": cmd_json(run), "reason": cls.reason}
                paused = True
                break
            if cls.risk in ("safe", "auto"):
                yield {"type": "command", "run": cmd_json(run)}
                await execute_run(db, run)
            yield {"type": "command", "run": cmd_json(run)}
            results.append(tool_message(run))

        for text in results:
            db.add(Message(conversation_id=cid, role="tool", content=text))
        db.commit()
        if paused:
            return  # retomado por /continue depois que o owner decidir
    yield {"type": "notice", "continue": True,
           "message": f"Parei depois de {max_steps} passos seguidos para você acompanhar. "
                      "Clique em Continuar (ou escreva \"continue\") para eu seguir de onde parei."}
    yield {"type": "done"}
