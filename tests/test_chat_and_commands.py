import json

import pytest

from app.providers.base import Provider
from tests.conftest import code


class ScriptedProvider(Provider):
    """Responde em sequência o que o teste definir e guarda o que recebeu."""

    id = "fake"
    label = "Fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def complete(self, history):
        self.seen.append([(t.role, t.content) for t in history])
        return self.replies.pop(0)


def sh(cmd: str, text: str = "Vou rodar:") -> str:
    return f"{text}\n\n```bash\n{cmd}\n```"


@pytest.fixture()
def use(monkeypatch):
    def _use(replies):
        p = ScriptedProvider(replies)
        monkeypatch.setattr("app.main.all_providers", lambda: {"fake": p})
        return p

    return _use


def events(resp):
    assert resp.status_code == 200, resp.text
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def types(evs):
    return [e["type"] for e in evs]


def new_conv(c):
    return c.post("/api/conversations").json()["id"]


def say(c, cid, text="faça isso"):
    return c.post(f"/api/conversations/{cid}/messages", json={"provider": "fake", "content": text})


def test_agent_runs_commands_by_itself_and_sees_the_output(logged, use):
    p = use([sh("echo ola > a.txt && cat a.txt"), "Pronto, criei o arquivo."])
    cid = new_conv(logged)
    evs = events(say(logged, cid))
    assert types(evs).count("approval") == 0
    cmd = [e for e in evs if e["type"] == "command" and e["run"]["status"] == "done"][0]["run"]
    assert cmd["output"].strip() == "ola"
    assert types(evs)[-1] == "done"
    # o segundo passo do provider recebeu a saída do primeiro comando
    last_turn = p.seen[1][-1]
    assert last_turn[0] == "user" and "ola" in last_turn[1] and "Código de saída: 0" in last_turn[1]


def test_dangerous_command_pauses_until_2fa_then_agent_resumes(logged, use):
    p = use([sh("rm -rf pasta-velha"), "Apaguei a pasta."])
    cid = new_conv(logged)
    evs = events(say(logged, cid))
    assert types(evs)[-1] == "approval"
    run = evs[-1]["run"]
    assert run["status"] == "pending" and len(p.replies) == 1  # laço parou antes do 2º passo

    bad = logged.post(f"/api/commands/{run['id']}/approve", json={"totp": "000000"})
    assert bad.status_code == 403
    ok = logged.post(f"/api/commands/{run['id']}/approve", json={"totp": code()}).json()
    assert ok["status"] == "done"

    evs2 = events(logged.post(f"/api/conversations/{cid}/continue", json={"provider": "fake"}))
    assert types(evs2)[-1] == "done" and evs2[1]["content"] == "Apaguei a pasta."
    assert "rm -rf pasta-velha" in p.seen[1][-1][1]


def test_rejected_command_is_reported_to_the_agent(logged, use):
    p = use([sh("rm -rf x"), "Ok, não apago."])
    cid = new_conv(logged)
    run = events(say(logged, cid))[-1]["run"]
    assert logged.post(f"/api/commands/{run['id']}/reject").json()["status"] == "rejected"
    events(logged.post(f"/api/conversations/{cid}/continue", json={"provider": "fake"}))
    assert "REJEITOU" in p.seen[1][-1][1]


def test_blocked_command_never_runs_and_agent_is_told(logged, use):
    p = use([sh("sudo rm -rf /"), "Entendi, outra abordagem."])
    cid = new_conv(logged)
    evs = events(say(logged, cid))
    blocked = [e for e in evs if e["type"] == "command"][0]["run"]
    assert blocked["status"] == "blocked"
    assert "BLOQUEADO" in p.seen[1][-1][1]


def test_agent_stops_after_max_steps(logged, use):
    from app.agent import MAX_STEPS

    use([sh("echo x")] * (MAX_STEPS + 2))
    evs = events(say(logged, new_conv(logged)))
    assert types(evs).count("step") == MAX_STEPS
    assert any(e["type"] == "notice" for e in evs)


def test_tool_messages_are_hidden_from_the_conversation_view(logged, use):
    use([sh("echo oi"), "feito"])
    cid = new_conv(logged)
    events(say(logged, cid))
    conv = logged.get(f"/api/conversations/{cid}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "assistant"]
    assert conv["commands"][0]["status"] == "done"


def test_continue_requires_something_to_continue(logged, use):
    use(["oi"])
    cid = new_conv(logged)
    events(say(logged, cid))
    assert logged.post(f"/api/conversations/{cid}/continue", json={"provider": "fake"}).status_code == 409


def test_manual_commands_follow_the_same_policy(logged):
    ok = logged.post("/api/commands", json={"command": "pwd"}).json()
    assert ok["status"] == "done" and ok["output"].strip().endswith("ws")
    assert logged.post("/api/commands", json={"command": "sudo ls"}).json()["status"] == "blocked"
    pend = logged.post("/api/commands", json={"command": "rm -rf x"}).json()
    assert pend["status"] == "pending"
    assert logged.post(f"/api/commands/{pend['id']}/reject").json()["status"] == "rejected"


def test_totp_code_cannot_be_replayed(logged):
    reused = code()
    a = logged.post("/api/commands", json={"command": "rm -rf a"}).json()
    assert logged.post(f"/api/commands/{a['id']}/approve", json={"totp": reused}).json()["status"] == "done"
    b = logged.post("/api/commands", json={"command": "rm -rf b"}).json()
    assert logged.post(f"/api/commands/{b['id']}/approve", json={"totp": reused}).status_code == 403


def test_commands_do_not_see_app_secrets(logged, monkeypatch):
    monkeypatch.setenv("GOS_OPENROUTER_API_KEY", "segredo-xyz")
    monkeypatch.setenv("SOME_API_TOKEN", "segredo-xyz")
    r = logged.post("/api/commands", json={"command": "env | grep -c segredo-xyz; true"}).json()
    assert r["output"].strip() == "0"
