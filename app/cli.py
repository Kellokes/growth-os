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


def migrate_sqlite(path: str) -> None:
    """Copia todos os dados de um SQLite antigo para o banco configurado em GOS_DATABASE_URL."""
    from sqlalchemy import create_engine, insert, select

    from .db import Base

    engine = init_db()
    if engine.dialect.name == "sqlite":
        sys.exit("GOS_DATABASE_URL ainda aponta para SQLite; aponte para o Postgres antes de migrar.")
    src = create_engine(f"sqlite:///{path}")
    with src.connect() as sc, engine.begin() as dc:
        # sorted_tables respeita as chaves estrangeiras (users antes de sessions etc.).
        for table in Base.metadata.sorted_tables:
            if dc.execute(select(table).limit(1)).first() is not None:
                sys.exit(f"A tabela {table.name} já tem dados no destino; migração cancelada para não duplicar.")
        for table in Base.metadata.sorted_tables:
            rows = [dict(r._mapping) for r in sc.execute(select(table))]
            if rows:
                dc.execute(insert(table), rows)
            print(f"{table.name}: {len(rows)} linhas")
        # Ajusta as sequências de id para o próximo insert não colidir.
        from sqlalchemy import text

        for table in Base.metadata.sorted_tables:
            pk = [c for c in table.primary_key.columns if c.autoincrement is True or c.type.python_type is int]
            if pk:
                dc.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                    f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 1))"
                ))
    print("Migração concluída.")


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
    sub.add_parser("migrate-sqlite").add_argument("path", help="ex.: /data/growth_os.db")
    args = parser.parse_args()
    if args.cmd == "migrate-sqlite":
        migrate_sqlite(args.path)
        return
    {"create-user": create_user, "reset-2fa": reset_2fa, "reset-password": reset_password}[args.cmd](args.username)


if __name__ == "__main__":
    main()
