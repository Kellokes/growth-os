import os

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.security import hash_password

PASSWORD = "uma-senha-bem-longa-123"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # só para testes


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _offset["n"] = -1
    # TEST_DATABASE_URL=postgresql+psycopg://... roda a suíte inteira no Postgres.
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("GOS_DATABASE_URL", url)
    monkeypatch.setenv("GOS_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("GOS_COOKIE_SECURE", "false")
    monkeypatch.setenv("GOS_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    engine = dbmod.init_db()
    dbmod.Base.metadata.drop_all(engine)
    dbmod.Base.metadata.create_all(engine)
    s = dbmod.new_session()
    s.add(dbmod.User(username="owner", password_hash=hash_password(PASSWORD), totp_secret=TOTP_SECRET))
    s.commit()
    from app.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


_offset = {"n": -1}


def code() -> str:
    """Cada chamada usa uma janela de 30s diferente, porque códigos 2FA não podem ser reutilizados."""
    import time

    step = _offset["n"]
    _offset["n"] = step + 1 if step < 1 else -1
    return pyotp.TOTP(TOTP_SECRET).at(time.time() + 30 * step)


@pytest.fixture()
def logged(client):
    r = client.post("/login", data={"username": "owner", "password": PASSWORD, "totp": code()}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/").text
    csrf = page.split('data-csrf="')[1].split('"')[0]
    client.headers["X-CSRF-Token"] = csrf
    return client
