"""Laço do agente: a IA propõe comandos, o Growth OS executa conforme a política e devolve a saída.

Cada passo: provider responde -> blocos ```bash são extraídos -> cada um é classificado:
  safe/auto  -> roda direto e a saída volta para a IA
  approval   -> o laço PAUSA e espera o 2FA do owner (retomado por /continue)
  blocked    -> não roda; a IA recebe o aviso e escolhe outro caminho
O laço para quando a resposta não tem mais comandos, ao pausar por aprovação ou no limite de passos.
"""

import re
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from .audit import audit
from .config import get_settings
from .db import CommandRun, Message
from .executor import classify, run_command
from .providers import ChatTurn, Provider, ProviderError

MAX_STEPS = 8
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


def build_history(db: DB, cid: int) -> list[ChatTurn]:
    rows = db.query(Message).filter(Message.conversation_id == cid).order_by(Message.id).all()
    turns = []
    for m in rows[-40:]:
        role = "assistant" if m.role == "assistant" else "user"  # "tool" volta como mensagem do usuário
        turns.append(ChatTurn(role, m.content))
    return turns


async def execute_run(db: DB, run: CommandRun) -> None:
    s = get_settings()
    result = await run_in_threadpool(
        run_command, run.command, Path(s.workspace_dir), s.command_timeout_seconds, s.command_max_output_bytes
    )
    run.exit_code = result.exit_code
    run.output = result.output
    run.status = "done" if result.exit_code == 0 else "failed"
    db.commit()


async def run_agent(db: DB, cid: int, provider: Provider, actor: str, ip: str) -> AsyncIterator[dict]:
    """Gera eventos para a interface: step, message, command, approval, notice, done, error."""
    for step in range(MAX_STEPS):
        yield {"type": "step", "n": step + 1, "provider": provider.id}
        try:
            reply = await provider.complete(build_history(db, cid))
        except ProviderError as e:
            audit(db, actor, "chat_error", f"{provider.id}: {e}", ip)
            yield {"type": "error", "message": str(e)}
            return
        m = Message(conversation_id=cid, role="assistant", provider=provider.id, content=reply or "(sem resposta)")
        db.add(m)
        db.commit()
        yield {"type": "message", "id": m.id, "provider": provider.id, "content": m.content}

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
    yield {"type": "notice", "message": f"Parei depois de {MAX_STEPS} passos seguidos. Diga \"continue\" se quiser que eu siga."}
    yield {"type": "done"}
