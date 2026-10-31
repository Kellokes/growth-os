from sqlalchemy.orm import Session as DB

from .db import AuditLog


def audit(db: DB, actor: str, action: str, detail: str = "", ip: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail[:4000], ip=ip))
    db.commit()
