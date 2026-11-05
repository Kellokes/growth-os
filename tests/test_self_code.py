"""O Growth OS lendo e mudando o próprio código: gos-self e o aviso de arquivos sensíveis do gos-pr."""

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from app.executor import classify
from app.providers.base import SYSTEM_PROMPT

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(SCRIPTS / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def remote(tmp_path):
    """Um 'GitHub' local: repositório bare com uma main e um teste que passa."""
    src = tmp_path / "src"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "tests").mkdir()
    (src / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    sh("git", "add", ".", cwd=src)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "remote.git"
    sh("git", "clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture()
def gos_self(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return load("gos-self", monkeypatch, GROWTH_OS_WORKSPACE=tmp_path / "ws", GROWTH_OS_REMOTE=remote,
                GITHUB_TOKEN_FILE=tmp_path / "nada")


def test_sync_clones_then_updates_without_touching_work(gos_self, capsys):
    gos_self.cmd_sync()
    assert (gos_self.CHECKOUT / ".git").is_dir()
    assert "clonado" in capsys.readouterr().out
    gos_self.cmd_branch("ajuste")
    (gos_self.CHECKOUT / "novo.txt").write_text("x")
    gos_self.cmd_sync()  # com trabalho pendente só faz fetch
    assert "não foi mexida" in capsys.readouterr().out
    assert (gos_self.CHECKOUT / "novo.txt").exists()


def test_branch_rules(gos_self):
    gos_self.cmd_sync()
    for bad in ("main", "a b", "x;rm", ""):
        with pytest.raises(SystemExit):
            gos_self.cmd_branch(bad)
    gos_self.cmd_branch("melhora-chat")
    assert gos_self.git("rev-parse", "--abbrev-ref", "HEAD") == "gos/melhora-chat"
    (gos_self.CHECKOUT / "f.txt").write_text("x")
    with pytest.raises(SystemExit):  # não troca de branch com alteração pendente
        gos_self.cmd_branch("outra")


def test_tests_run_in_checkout_and_pr_needs_branch_and_commit(gos_self):
    gos_self.cmd_sync()
    assert gos_self.run_tests([]) == 0
    with pytest.raises(SystemExit, match="branch"):
        gos_self.cmd_pr(["--title", "x"])  # ainda na main
    gos_self.cmd_branch("vazia")
    with pytest.raises(SystemExit, match="commits novos"):
        gos_self.cmd_pr(["--title", "x"])


def test_info_without_token_or_checkout(gos_self, capsys):
    gos_self.cmd_info()
    out = capsys.readouterr().out
    assert "ainda não existe" in out and "sem token" in out


def test_pr_warns_on_sensitive_files(monkeypatch):
    pr = load("gos-pr", monkeypatch, GROWTH_OS_REPO="Kellokes/growth-os")
    files = ["app/main.py", "app/executor.py", ".github/workflows/deploy.yml", "scripts/deploy.sh",
             "docker-compose.yml", "README.md", "web/Dockerfile"]
    assert pr.sensitive_files("Kellokes/growth-os", files) == [
        "app/executor.py", ".github/workflows/deploy.yml", "scripts/deploy.sh", "docker-compose.yml", "web/Dockerfile"]
    # Em outro repo, executor.py não tem o mesmo peso.
    assert "app/executor.py" not in pr.sensitive_files("Kellokes/zombie-party", files)


def test_policy_and_prompt():
    for cmd in ("gos-self info", "gos-self sync", "gos-self branch x", "gos-self test",
                'gos-self pr --title "Melhora o chat" --body "detalhes"'):
        assert classify(cmd).risk in ("safe", "auto"), cmd
    # Push na main continua pedindo 2FA, mesmo dentro da cópia do próprio código.
    assert classify("cd /workspace/growth-os && git push origin main").risk == "approval"
    assert "gos-self" in SYSTEM_PROMPT and "/workspace/growth-os" in SYSTEM_PROMPT
