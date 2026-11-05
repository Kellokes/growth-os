import json
import shutil
import tempfile
from pathlib import Path

from ..config import get_settings
from .base import SYSTEM_PROMPT, ChatTurn, Provider, ProviderError, render_transcript, run_cli


class ClaudeCLIProvider(Provider):
    """Claude via Claude Code CLI, usando o login OAuth oficial do owner (`claude login`).

    Roda sem nenhuma ferramenta habilitada: o Claude só conversa e propõe comandos,
    quem executa é o executor do Growth OS com aprovação.
    """

    id = "claude"
    label = "Claude (login da sua conta)"

    def available(self) -> tuple[bool, str]:
        if shutil.which(get_settings().claude_bin) is None:
            return False, "Claude Code CLI não instalado"
        return True, ""

    async def complete_with_image(self, prompt: str, image: bytes, mime: str) -> str:
        """Claude lê a imagem com a ferramenta Read, liberada só para uma pasta temporária que contém essa imagem."""
        s = get_settings()
        with tempfile.TemporaryDirectory() as tmp:
            name = "layout." + ("jpg" if "jpeg" in mime else mime.split("/")[-1] or "png")
            (Path(tmp) / name).write_bytes(image)
            args = [s.claude_bin, "-p", "--output-format", "json", "--tools", "Read", "--allowedTools", "Read",
                    "--permission-mode", "dontAsk", "--system-prompt", "Você converte imagens de layout em HTML. Responda só o pedido."]
            if self.model:
                args += ["--model", self.model]
            if self.reasoning:
                args += ["--effort", self.reasoning]
            raw = await run_cli(args, Path(tmp), s.provider_timeout_seconds,
                                stdin=f"Primeiro leia a imagem do arquivo ./{name}.\n\n{prompt}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()
        if data.get("is_error"):
            raise ProviderError(str(data.get("result") or "erro do Claude"))
        return str(data.get("result", "")).strip()

    async def complete(self, history: list[ChatTurn]) -> str:
        s = get_settings()
        args = [
            s.claude_bin, "-p",
            "--output-format", "json",
            "--tools", "",
            "--system-prompt", SYSTEM_PROMPT,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.reasoning:
            args += ["--effort", self.reasoning]
        raw = await run_cli(args, Path(s.workspace_dir), s.provider_timeout_seconds, stdin=render_transcript(history))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()
        if data.get("is_error"):
            raise ProviderError(str(data.get("result") or "erro do Claude"))
        return str(data.get("result", "")).strip()
