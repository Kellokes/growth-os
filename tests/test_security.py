from app.config import get_settings
from tests.conftest import PASSWORD, code


def test_pages_require_login(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/conversations").status_code == 401


def test_login_needs_totp(client):
    r = client.post("/login", data={"username": "owner", "password": PASSWORD, "totp": "000000"})
    assert r.status_code == 401
    assert "gos_session" not in client.cookies


def test_lockout_after_failures(client):
    for _ in range(get_settings().max_login_failures):
        client.post("/login", data={"username": "owner", "password": "errada", "totp": "000000"})
    r = client.post("/login", data={"username": "owner", "password": PASSWORD, "totp": code()})
    assert r.status_code == 401


def test_csrf_required_on_writes(logged):
    token = logged.headers.pop("X-CSRF-Token")
    assert logged.post("/api/conversations").status_code == 403
    logged.headers["X-CSRF-Token"] = token
    assert logged.post("/api/conversations").status_code == 200


def test_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_audit_records_login(logged):
    actions = [a["action"] for a in logged.get("/api/audit").json()]
    assert "login_ok" in actions
