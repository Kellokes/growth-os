from app.config import get_settings
from tests.conftest import PASSWORD, code, login


def test_pages_require_login(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/conversations").status_code == 401


def test_login_needs_totp(client):
    r = login(client, totp="000000")
    assert r.status_code == 401
    assert "gos_session" not in client.cookies


def test_password_step_opens_2fa_screen_without_session(client):
    r = client.post("/login", data={"username": "owner", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"
    assert "gos_session" not in client.cookies
    page = client.get("/login/2fa")
    assert page.status_code == 200 and 'name="totp"' in page.text
    # Sem 2FA, nada de páginas internas.
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_wrong_password_stays_on_step_one(client):
    r = client.post("/login", data={"username": "owner", "password": "errada"}, follow_redirects=False)
    assert r.status_code == 401 and "Usuário ou senha inválidos" in r.text
    assert "gos_login" not in client.cookies


def test_2fa_screen_needs_password_step(client):
    assert client.get("/login/2fa", follow_redirects=False).headers["location"].startswith("/login")
    r = client.post("/login/2fa", data={"totp": code()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "gos_session" not in client.cookies


def test_2fa_code_cannot_be_reused(client):
    c = code()
    assert login(client, totp=c).status_code == 303
    client.cookies.clear()
    assert login(client, totp=c).status_code == 401


def test_2fa_attempts_are_limited(client):
    from app.security import LOGIN_CHALLENGE_ATTEMPTS

    client.post("/login", data={"username": "owner", "password": PASSWORD}, follow_redirects=False)
    for _ in range(LOGIN_CHALLENGE_ATTEMPTS - 1):
        assert client.post("/login/2fa", data={"totp": "000000"}, follow_redirects=False).status_code == 401
    r = client.post("/login/2fa", data={"totp": "000000"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    # O desafio acabou: nem o código certo entra sem refazer a senha.
    r = client.post("/login/2fa", data={"totp": code()}, follow_redirects=False)
    assert r.status_code == 303 and "gos_session" not in client.cookies


def test_lockout_after_failures(client):
    for _ in range(get_settings().max_login_failures):
        client.post("/login", data={"username": "owner", "password": "errada"})
    r = login(client)
    assert r.status_code == 401


def test_wrong_2fa_codes_count_toward_lockout(client):
    for _ in range(get_settings().max_login_failures):
        login(client, totp="000000")
    r = client.post("/login", data={"username": "owner", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 401


def test_challenge_expires(client, monkeypatch):
    from datetime import timedelta

    from app import security

    client.post("/login", data={"username": "owner", "password": PASSWORD}, follow_redirects=False)
    later = security.utcnow() + timedelta(seconds=security.LOGIN_CHALLENGE_SECONDS + 1)
    monkeypatch.setattr(security, "utcnow", lambda: later)
    r = client.post("/login/2fa", data={"totp": code()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?expirou=1"
    assert "gos_session" not in client.cookies


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
