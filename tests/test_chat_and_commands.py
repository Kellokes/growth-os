from app.providers.base import Provider
from tests.conftest import code


class FakeProvider(Provider):
    id = "fake"
    label = "Fake"

    async def complete(self, history):
        return f"eco: {history[-1].content}\n\n```bash\nls\n```"


def test_chat_roundtrip(logged, monkeypatch):
    monkeypatch.setattr("app.main.all_providers", lambda: {"fake": FakeProvider()})
    cid = logged.post("/api/conversations").json()["id"]
    r = logged.post(f"/api/conversations/{cid}/messages", json={"provider": "fake", "content": "oi"})
    assert r.status_code == 200
    assert r.json()["content"].startswith("eco: oi")
    conv = logged.get(f"/api/conversations/{cid}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["title"] == "oi"


def test_safe_command_runs(logged):
    r = logged.post("/api/commands", json={"command": "pwd"}).json()
    assert r["status"] == "done" and r["output"].strip().endswith("ws")


def test_blocked_command_never_runs(logged):
    r = logged.post("/api/commands", json={"command": "sudo rm -rf /"}).json()
    assert r["status"] == "blocked"


def test_risky_command_needs_valid_totp(logged):
    r = logged.post("/api/commands", json={"command": "mkdir criado && echo ok"}).json()
    assert r["status"] == "pending"
    bad = logged.post(f"/api/commands/{r['id']}/approve", json={"totp": "000000"})
    assert bad.status_code == 403
    ok = logged.post(f"/api/commands/{r['id']}/approve", json={"totp": code()}).json()
    assert ok["status"] == "done" and "ok" in ok["output"]
    again = logged.post(f"/api/commands/{r['id']}/approve", json={"totp": code()})
    assert again.status_code == 409


def test_totp_code_cannot_be_replayed(logged):
    reused = code()
    first = logged.post("/api/commands", json={"command": "touch a"}).json()
    assert logged.post(f"/api/commands/{first['id']}/approve", json={"totp": reused}).json()["status"] == "done"
    second = logged.post("/api/commands", json={"command": "touch b"}).json()
    assert logged.post(f"/api/commands/{second['id']}/approve", json={"totp": reused}).status_code == 403


def test_reject_command(logged):
    r = logged.post("/api/commands", json={"command": "touch x"}).json()
    assert logged.post(f"/api/commands/{r['id']}/reject").json()["status"] == "rejected"


def test_commands_do_not_see_secrets(logged, monkeypatch):
    monkeypatch.setenv("GOS_OPENROUTER_API_KEY", "segredo-xyz")
    monkeypatch.setenv("SOME_API_TOKEN", "segredo-xyz")
    r = logged.post("/api/commands", json={"command": "env | grep -c segredo-xyz; true"}).json()
    done = logged.post(f"/api/commands/{r['id']}/approve", json={"totp": code()}).json()
    assert done["output"].strip() == "0"
