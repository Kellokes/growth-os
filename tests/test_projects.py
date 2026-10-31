import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app import projects as proj
from app.executor import classify

from .conftest import code

ROOT = Path(__file__).resolve().parent.parent

CADDYFILE = """
{
    email admin@example.com
}

(common) {
    encode gzip
}

{$DOMAIN} {
    reverse_proxy web:3000
}

media.{$DOMAIN} {
    reverse_proxy storage:8333
}

zombieparty.com.br, www.zombieparty.com.br {
    # comentário com { chave
    reverse_proxy zombie-party-app-1:3000
}

www.ailhajogo.com.br {
    redir https://ailhajogo.com.br{uri} permanent
}

os.{$DOMAIN} {
    @api path /api/*
    handle @api {
        reverse_proxy growth-os:8000 {
            flush_interval -1
        }
    }
    reverse_proxy growth-os:8000
}
"""

CONTAINERS = [
    {"id": "a1", "name": "takeone-web-1", "image": "takeone-web", "state": "running", "status": "Up 2 hours (healthy)",
     "health": "healthy", "project": "takeone", "service": "web", "networks": [], "ports": []},
    {"id": "a2", "name": "storage", "image": "seaweedfs", "state": "exited", "status": "Exited (1)",
     "health": "", "project": "takeone", "service": "storage", "networks": [], "ports": []},
    {"id": "b1", "name": "zombie-party-app-1", "image": "zp", "state": "running", "status": "Up", "health": "",
     "project": "zombie-party", "service": "app", "networks": [], "ports": []},
]


def test_parse_caddyfile_resolves_vars_and_skips_globals():
    sites = proj.parse_caddyfile(CADDYFILE, {"DOMAIN": "takeonestudio.io"})
    hosts = [s["hosts"] for s in sites]
    assert hosts == [
        ["takeonestudio.io"], ["media.takeonestudio.io"], ["zombieparty.com.br", "www.zombieparty.com.br"],
        ["www.ailhajogo.com.br"], ["os.takeonestudio.io"],
    ]
    assert sites[0]["upstream"] == "web:3000"
    assert sites[3]["redirect"].startswith("https://ailhajogo.com.br")
    assert sites[4]["upstream"] == "growth-os:8000"


def test_parse_caddy_vars():
    assert proj.parse_caddy_vars("DOMAIN=a.io, OTHER=b") == {"DOMAIN": "a.io", "OTHER": "b"}
    assert proj.parse_caddy_vars("") == {}


def test_group_projects_status_and_sites():
    sites = proj.parse_caddyfile(CADDYFILE, {"DOMAIN": "takeonestudio.io"})
    out = {p["name"]: p for p in proj.group_projects(CONTAINERS, sites)}
    assert out["takeone"]["status"] == "warn"  # storage parado
    assert out["zombie-party"]["status"] == "ok"
    assert out["zombie-party"]["sites"][0]["container"] == "zombie-party-app-1"
    assert [s["hosts"][0] for s in out["takeone"]["sites"]] == ["takeonestudio.io", "media.takeonestudio.io"]
    assert [s["hosts"][0] for s in out["_caddy"]["sites"]] == ["www.ailhajogo.com.br", "os.takeonestudio.io"]


def test_restart_target_is_strict():
    assert proj.restart_target("gos-docker restart takeone-web-1") == "takeone-web-1"
    assert proj.restart_target("gos-docker restart x; rm -rf /tmp") is None
    assert proj.restart_target("gos-docker restart") is None
    assert proj.restart_target("echo gos-docker restart a") is None


def test_classify_stage2_tools():
    assert classify("gos-docker ps").risk == "safe"
    assert classify("gos-docker logs takeone-web-1 --tail 50").risk == "safe"
    assert classify("gos-docker restart takeone-web-1").risk == "approval"
    assert classify("gos-pr create a/b --head x --title y").risk == "auto"
    for cmd in ["cat /tmp/gos-secrets/github_token", "git credential fill", "git config --global credential.helper store",
                "cat /run/secrets/x", "gos-git-credential get"]:
        assert classify(cmd).risk == "blocked", cmd


@pytest.fixture()
def gateway(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        p = request.url.path
        if p == "/containers":
            return httpx.Response(200, json=CONTAINERS)
        if p.endswith("/logs"):
            return httpx.Response(200, text="linha 1\nlinha 2\n")
        if p.endswith("/restart"):
            if request.headers.get("X-Gateway-Token") != "tok":
                return httpx.Response(401, json={"error": "token inválido"})
            return httpx.Response(200, json={"ok": True})
        if request.method == "HEAD":
            return httpx.Response(403 if request.url.host.startswith("media.") else 200)
        return httpx.Response(404)

    monkeypatch.setattr(proj, "_transport", httpx.MockTransport(handler))
    return calls


@pytest.fixture()
def caddy(tmp_path, monkeypatch):
    f = tmp_path / "Caddyfile"
    f.write_text(CADDYFILE)
    monkeypatch.setenv("GOS_CADDYFILE_PATH", str(f))
    monkeypatch.setenv("GOS_CADDY_VARS", "DOMAIN=takeonestudio.io")
    monkeypatch.setenv("GOS_GATEWAY_TOKEN", "tok")
    from app.config import get_settings
    get_settings.cache_clear()
    return f


def test_projects_page_requires_login(client):
    r = client.get("/projects", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/projects").status_code == 401


def test_projects_overview(logged, gateway, caddy):
    assert logged.get("/projects").status_code == 200
    data = logged.get("/api/projects").json()
    assert data["errors"] == []
    names = [p["name"] for p in data["projects"]]
    assert names[:2] == ["takeone", "zombie-party"]
    doms = logged.get("/api/projects/domains").json()
    assert doms["media.takeonestudio.io"] == {"code": 403, "ok": True}
    assert doms["zombieparty.com.br"]["ok"] is True


def test_projects_overview_reports_gateway_down(logged, caddy, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("off")

    monkeypatch.setattr(proj, "_transport", httpx.MockTransport(boom))
    data = logged.get("/api/projects").json()
    assert data["errors"] and "gateway" in data["errors"][0]
    assert any(p["name"] == "_caddy" for p in data["projects"])


def test_logs_and_restart_need_2fa(logged, gateway, caddy):
    r = logged.get("/api/containers/takeone-web-1/logs?tail=50")
    assert r.json()["logs"].startswith("linha 1")
    assert gateway[-1].url.params["tail"] == "50"
    assert logged.get("/api/containers/bad%20name/logs").status_code == 502

    assert logged.post("/api/containers/takeone-web-1/restart", json={"totp": "000000"}).status_code == 403
    assert not any(c.url.path.endswith("/restart") for c in gateway)
    r = logged.post("/api/containers/takeone-web-1/restart", json={"totp": code()})
    assert r.status_code == 200, r.text
    assert gateway[-1].headers["X-Gateway-Token"] == "tok"
    actions = [a["action"] for a in logged.get("/api/audit").json()]
    assert "container_restart" in actions and "restart_failed" in actions


def test_agent_restart_goes_through_gateway_after_approval(logged, gateway, caddy):
    cid = logged.post("/api/conversations").json()["id"]
    run = logged.post("/api/commands", json={"command": "gos-docker restart zombie-party-app-1", "conversation_id": cid}).json()
    assert run["status"] == "pending"
    assert not any(c.url.path.endswith("/restart") for c in gateway)
    done = logged.post(f"/api/commands/{run['id']}/approve", json={"totp": code()}).json()
    assert done["status"] == "done" and "reiniciado" in done["output"]
    assert gateway[-1].url.path == "/containers/zombie-party-app-1/restart"


# ---------- gateway (stdlib, roda no próprio container) ----------

def _gateway_module():
    spec = importlib.util.spec_from_file_location("gos_gateway", ROOT / "gateway" / "gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gateway_sanitize_drops_env_and_labels():
    gw = _gateway_module()
    raw = {
        "Id": "abcdef1234567890", "Names": ["/takeone-web-1"], "Image": "web", "State": "running",
        "Status": "Up 3 hours (unhealthy)", "Labels": {"com.docker.compose.project": "takeone", "secret": "x"},
        "Ports": [{"PrivatePort": 3000, "Type": "tcp"}, {"IP": "127.0.0.1", "PrivatePort": 8000, "PublicPort": 8100, "Type": "tcp"}],
        "NetworkSettings": {"Networks": {"takeone_default": {}}}, "Env": ["DB_PASSWORD=x"], "Mounts": [{"Source": "/"}],
    }
    out = gw.sanitize(raw)
    assert out["name"] == "takeone-web-1" and out["health"] == "unhealthy" and out["project"] == "takeone"
    assert "127.0.0.1:8100->8000/tcp" in out["ports"]
    dumped = json.dumps(out)
    assert "DB_PASSWORD" not in dumped and "secret" not in dumped and "Mounts" not in dumped


def test_gateway_demux():
    gw = _gateway_module()
    frames = b"\x01\x00\x00\x00\x00\x00\x00\x06hello\n" + b"\x02\x00\x00\x00\x00\x00\x00\x04err\n"
    assert gw.demux(frames) == "hello\nerr\n"
    assert gw.demux(b"plain tty log\n") == "plain tty log\n"


def test_gos_git_credential_only_for_github(tmp_path):
    tok = tmp_path / "t"
    tok.write_text("ghp_abc\n")
    script = str(ROOT / "scripts" / "gos-git-credential")
    env = {"GITHUB_TOKEN_FILE": str(tok), "PATH": "/usr/bin:/bin"}
    out = subprocess.run(["sh", script, "get"], input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True, env=env)
    assert out.stdout == "username=x-access-token\npassword=ghp_abc\n"
    out = subprocess.run(["sh", script, "get"], input="protocol=https\nhost=evil.com\n\n", capture_output=True, text=True, env=env)
    assert out.stdout == ""
