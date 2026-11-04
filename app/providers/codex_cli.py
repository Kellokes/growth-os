import shutil
import tempfile
from pathlib import Path

from ..config import get_settings
from .base import SYSTEM_PROMPT, ChatTurn, Provider, render_transcript, run_cli


class CodexCLIProvider(Provider):
    """Codex via Codex CLI, usando o login oficial do owner (`codex login`).

    Roda em sandbox somente-leitura; comandos passam pelo executor do Growth OS.
    """

    id = "codex"
    label = "Codex (login da sua conta)"

    def available(self) -> tuple[bool, str]:
        if shutil.which(get_settings().codex_bin) is None:
            return False, "Codex CLI não instalado"
        return True, ""

    async def complete_with_image(self, prompt: str, image: bytes, mime: str) -> str:
        """Codex enxerga a imagem anexada (--image). A imagem vai num arquivo temporário, apagado ao final."""
        s = get_settings()
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / ("layout." + ("jpg" if "jpeg" in mime else mime.split("/")[-1] or "png"))
            img.write_bytes(image)
            out_file = Path(tmp) / "last.txt"
            args = [s.codex_bin, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                    "--output-last-message", str(out_file), f"--image={img}"]
            if self.model:
                args += ["--model", self.model]
            if self.reasoning:
                args += ["-c", f'model_reasoning_effort="{self.reasoning}"']
            args.append("-")
            raw = await run_cli(args, Path(tmp), s.provider_timeout_seconds, stdin=prompt)
            if out_file.exists():
                return out_file.read_text(errors="replace").strip()
            return raw.strip()

    async def complete(self, history: list[ChatTurn]) -> str:
        s = get_settings()
        prompt = f"{SYSTEM_PROMPT}\n\n{render_transcript(history)}"
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "last.txt"
            args = [
                s.codex_bin, "exec",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--output-last-message", str(out_file),
            ]
            if self.model:
                args += ["--model", self.model]
            if self.reasoning:
                args += ["-c", f'model_reasoning_effort="{self.reasoning}"']
            args.append("-")
            raw = await run_cli(args, Path(s.workspace_dir), s.provider_timeout_seconds, stdin=prompt)
            if out_file.exists():
                return out_file.read_text(errors="replace").strip()
            return raw.strip()
