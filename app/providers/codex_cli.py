import shutil
import tempfile
from pathlib import Path

from ..config import get_settings
from .base import SYSTEM_PROMPT, ChatTurn, Provider, ProviderError, render_transcript, run_cli


def explain_codex(model: str, raw: str) -> str | None:
    """Troca o despejo do Codex CLI por uma frase clara quando o erro é de modelo."""
    low = raw.lower()
    name = model or "padrão"
    if "not supported when using codex with a chatgpt account" in low:
        return (f"O modelo \"{name}\" não é aceito pelo Codex com login ChatGPT. "
                "Em Modelos, escolha outro (GPT-6 Astra, 6.1 Sol, 6.1 Terra, 6 Sol ou 6 Luna) ou deixe em branco para usar o padrão da conta.")
    if "model metadata" in low and "not found" in low:
        return f"O Codex não conhece o modelo \"{name}\". Escolha outro em Modelos ou deixe em branco para o padrão da conta."
    return None


async def run_codex(model: str, args: list[str], cwd: Path, timeout: int, stdin: str) -> str:
    try:
        return await run_cli(args, cwd, timeout, stdin=stdin)
    except ProviderError as e:
        msg = explain_codex(model, str(e))
        if msg:
            raise ProviderError(msg) from e
        raise


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
            raw = await run_codex(self.model, args, Path(tmp), s.provider_timeout_seconds, prompt)
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
            raw = await run_codex(self.model, args, Path(s.workspace_dir), s.provider_timeout_seconds, prompt)
            if out_file.exists():
                return out_file.read_text(errors="replace").strip()
            return raw.strip()
