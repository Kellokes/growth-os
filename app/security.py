"""Autenticação do owner: senha argon2 + TOTP, sessões no servidor, CSRF."""

import hashlib
import hmac
import secrets
import time
from datetime import timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DB

from .config import get_settings
from .db import LoginChallenge, Session, User, get_db, utcnow

SESSION_COOKIE = "gos_session"
# Cookie da etapa 2 do login: vale poucos minutos e só para /login.
LOGIN_COOKIE = "gos_login"
LOGIN_CHALLENGE_SECONDS = 300
LOGIN_CHALLENGE_ATTEMPTS = 5
_hasher = PasswordHasher()
# Hash usado quando o usuário não existe, para o tempo de resposta não revelar isso.
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str, after_step: int = 0) -> int | None:
    """Retorna a janela (timestep) do código válido, ou None.

    Aceita ±1 janela de 30s para tolerar relógio, mas só janelas posteriores
    a `after_step`, então um código já usado não serve de novo.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // totp.interval
    for step in (now_step - 1, now_step, now_step + 1):
        if step > after_step and hmac.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


def consume_totp(db: DB, user: User, code: str) -> bool:
    step = verify_totp(user.totp_secret, code, user.last_totp_step or 0)
    if step is None:
        return False
    user.last_totp_step = step
    db.commit()
    return True


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _register_failure(db: DB, user: User) -> None:
    s = get_settings()
    user.failed_logins += 1
    if user.failed_logins >= s.max_login_failures:
        user.locked_until = utcnow() + timedelta(minutes=s.lockout_minutes)
        user.failed_logins = 0
        # Bloqueou: nenhum desafio aberto sobrevive.
        db.query(LoginChallenge).filter(LoginChallenge.user_id == user.id).delete()
    db.commit()


def _is_locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > utcnow())


def check_password(db: DB, username: str, password: str) -> tuple[User | None, str]:
    """Etapa 1 do login. Retorna (usuário, motivo). Motivo só vai para a auditoria, nunca para a tela.

    Senha certa ainda não zera o contador de falhas: isso só acontece depois do 2FA.
    """
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None, "unknown_user"
    if _is_locked(user):
        return None, "locked"
    if not verify_password(user.password_hash, password):
        _register_failure(db, user)
        return None, "bad_password"
    return user, "ok"


def create_login_challenge(db: DB, user: User) -> str:
    """Abre a etapa 2 (2FA). Um desafio por usuário: abrir outro descarta o anterior."""
    db.query(LoginChallenge).filter(LoginChallenge.user_id == user.id).delete()
    token = secrets.token_urlsafe(32)
    db.add(LoginChallenge(
        token_hash=_hash_token(token), user_id=user.id,
        expires_at=utcnow() + timedelta(seconds=LOGIN_CHALLENGE_SECONDS),
    ))
    db.commit()
    return token


def lookup_login_challenge(db: DB, token: str | None) -> LoginChallenge | None:
    if not token:
        return None
    ch = db.get(LoginChallenge, _hash_token(token))
    if ch is None:
        return None
    if ch.expires_at <= utcnow() or ch.attempts >= LOGIN_CHALLENGE_ATTEMPTS:
        db.delete(ch)
        db.commit()
        return None
    return ch


def complete_login_challenge(db: DB, token: str | None, code: str) -> tuple[User | None, str]:
    """Etapa 2 do login: o mesmo TOTP anti-replay usado para confirmar comandos perigosos."""
    ch = lookup_login_challenge(db, token)
    if ch is None:
        return None, "no_challenge"
    user = db.get(User, ch.user_id)
    if user is None:
        db.delete(ch)
        db.commit()
        return None, "no_challenge"
    if _is_locked(user):
        db.delete(ch)
        db.commit()
        return None, "locked"
    if not consume_totp(db, user, code):
        ch.attempts += 1
        if ch.attempts >= LOGIN_CHALLENGE_ATTEMPTS:
            db.delete(ch)
        _register_failure(db, user)
        return None, "bad_totp"
    db.delete(ch)
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
    return user, "ok"


def create_session(db: DB, user: User) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    sess = Session(
        token_hash=_hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=utcnow() + timedelta(hours=get_settings().session_hours),
    )
    db.add(sess)
    db.commit()
    return token, sess


def destroy_session(db: DB, token: str | None) -> None:
    if token:
        db.query(Session).filter(Session.token_hash == _hash_token(token)).delete()
        db.commit()


def lookup_session(db: DB, token: str | None) -> Session | None:
    if not token:
        return None
    sess = db.get(Session, _hash_token(token))
    if sess is None:
        return None
    if sess.expires_at <= utcnow():
        db.delete(sess)
        db.commit()
        return None
    return sess


class Auth:
    def __init__(self, user: User, session: Session):
        self.user = user
        self.session = session


def current_auth(request: Request, db: DB = Depends(get_db)) -> Auth:
    sess = lookup_session(db, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    user = db.get(User, sess.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, sess.csrf_token):
            raise HTTPException(status_code=403, detail="csrf")
    return Auth(user, sess)


def client_ip(request: Request) -> str:
    # O Caddy define X-Forwarded-For; o uvicorn só confia nele com --forwarded-allow-ips.
    return request.client.host if request.client else ""
