"""Administração local: `python -m app.cli create-user <usuario>`.

Roda dentro do container (docker compose exec growth-os python -m app.cli ...).
"""

import argparse
import getpass
import sys

import pyotp

from .db import User, init_db, new_session
from .security import hash_password, new_totp_secret


def create_user(username: str) -> None:
    init_db()
    db = new_session()
    if db.query(User).count() > 0:
        sys.exit("Já existe um owner. O Growth OS é de usuário único; use reset-2fa ou reset-password.")
    pw = getpass.getpass("Senha (mín. 14 caracteres): ")
    if len(pw) < 14 or pw != getpass.getpass("Repita a senha: "):
        sys.exit("Senha curta demais ou não confere.")
    secret = new_totp_secret()
    db.add(User(username=username, password_hash=hash_password(pw), totp_secret=secret))
    db.commit()
    _print_totp(username, secret)


def reset_2fa(username: str) -> None:
    init_db()
    db = new_session()
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        sys.exit("Usuário não encontrado.")
    user.totp_secret = new_totp_secret()
    db.commit()
    _print_totp(username, user.totp_secret)


def reset_password(username: str) -> None:
    init_db()
    db = new_session()
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        sys.exit("Usuário não encontrado.")
    pw = getpass.getpass("Nova senha (mín. 14 caracteres): ")
    if len(pw) < 14 or pw != getpass.getpass("Repita a senha: "):
        sys.exit("Senha curta demais ou não confere.")
    user.password_hash = hash_password(pw)
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
    print("Senha atualizada.")


def _print_totp(username: str, secret: str) -> None:
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Growth OS")
    print("\nCadastre no seu app autenticador (Google Authenticator, 1Password, Authy):")
    print(f"  Segredo: {secret}")
    print(f"  URI:     {uri}\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="growth-os")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("create-user", "reset-2fa", "reset-password"):
        sub.add_parser(name).add_argument("username")
    args = parser.parse_args()
    {"create-user": create_user, "reset-2fa": reset_2fa, "reset-password": reset_password}[args.cmd](args.username)


if __name__ == "__main__":
    main()
