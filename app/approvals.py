"""Aprovações: tudo que espera uma decisão do owner, num lugar só.

A fila pendente é calculada ao vivo a partir das tabelas de origem (tarefas propostas, missões, conteúdo
pendente e ações externas), então nunca fica fora de sincronia. Cada decisão vira uma linha em `approvals`
(histórico) e um evento na linha do tempo do projeto.
"""

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session as DB

from . import content as cnt
from .db import (
    Action, AgentNote, Approval, ContentItem, GrowthProject, Mission, Task, TaskLink, Event, utcnow,
)

KINDS = ("task", "mission", "content", "action")
RISK_ORDER = {"alto": 0, "medio": 1, "baixo": 2}
NEEDS_2FA = {"action": ("alto",)}  # ação externa de risco alto exige o código 2FA no cartão


class ApprovalError(Exception):
    """Decisão inválida (não existe, já decidida, falta 2FA...). `code` é o status HTTP sugerido."""

    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


def emit(db: DB, project_id: int, type_: str, subject_kind: str = "", subject_id: int | None = None,
         payload: dict | None = None) -> Event:
    ev = Event(project_id=project_id, type=type_, subject_kind=subject_kind, subject_id=subject_id,
               payload=json.dumps(payload or {}, ensure_ascii=False)[:4000])
    db.add(ev)
    db.commit()
    return ev


def _plain(text: str, limit: int) -> str:
    return re.sub(r"```.*?```", "", text or "", flags=re.S).strip()[:limit]


def task_origin(t: Task, link: TaskLink | None) -> str:
    return "rotina" if t.automation_id else "pedido" if link else "plano" if t.plan_id else "chat" if t.mission_id else "owner"


def _card_task(t: Task, link: TaskLink | None) -> dict:
    by = f"agente:{link.requested_by}" if link else f"rotina:{t.automation_id}" if t.automation_id else \
        "plano" if t.plan_id else "owner"
    return {
        "kind": "task", "ref_id": t.id, "project_id": t.project_id, "agent": t.agent_slug,
        "title": t.title, "preview": t.steps or t.rationale, "why": t.rationale, "risk": t.risk,
        "requested_by": by, "origin": task_origin(t, link), "mission_id": t.mission_id,
        "needs_2fa": False, "at": t.created_at,
    }


def _card_content(c: ContentItem) -> dict:
    where = f" para {c.recipient}" if c.recipient else ""
    return {
        "kind": "content", "ref_id": c.id, "project_id": c.project_id, "agent": c.agent_slug,
        "title": c.title or f"{c.channel}{where}", "preview": c.body, "why": c.purpose, "risk": "baixo",
        "requested_by": f"agente:{c.agent_slug}" if c.agent_slug else "owner", "origin": "conteudo",
        "channel": c.channel, "mission_id": None, "needs_2fa": False, "at": c.created_at,
    }


def _card_mission(m: Mission) -> dict:
    return {
        "kind": "mission", "ref_id": m.id, "project_id": m.project_id, "agent": m.lead_agent,
        "title": m.title, "preview": m.goal, "why": "", "risk": "medio",
        "requested_by": f"chat:{m.conversation_id}" if m.conversation_id else m.origin, "origin": "missao",
        "mission_id": m.id, "needs_2fa": False, "at": m.created_at,
    }


def _card_action(a: Action) -> dict:
    try:
        shown = json.dumps(json.loads(a.params or "{}"), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        shown = a.params
    return {
        "kind": "action", "ref_id": a.id, "project_id": a.project_id, "agent": "",
        "title": a.title or f"{a.integration}: {a.operation}", "preview": shown, "why": f"{a.integration} · {a.operation}",
        "risk": a.risk, "requested_by": f"tarefa:{a.task_id}" if a.task_id else "agente", "origin": "acao-externa",
        "mission_id": None, "needs_2fa": a.risk in NEEDS_2FA["action"], "at": a.created_at,
    }


def pending(db: DB, project_id: int | None = None, kind: str | None = None) -> list[dict]:
    """Cartões esperando decisão: risco alto primeiro, depois os mais antigos."""
    def scoped(q, col):
        return q.filter(col == project_id) if project_id is not None else q

    cards: list[dict] = []
    if kind in (None, "task"):
        rows = scoped(db.query(Task).filter(Task.status == "proposed"), Task.project_id).all()
        links = {lk.task_id: lk for lk in db.query(TaskLink).filter(TaskLink.task_id.in_([t.id for t in rows])).all()} if rows else {}
        cards += [_card_task(t, links.get(t.id)) for t in rows]
    if kind in (None, "content"):
        cards += [_card_content(c) for c in scoped(db.query(ContentItem).filter(ContentItem.status == "pending"),
                                                  ContentItem.project_id).all()]
    if kind in (None, "mission"):
        cards += [_card_mission(m) for m in scoped(db.query(Mission).filter(Mission.status == "awaiting_approval"),
                                                  Mission.project_id).all()]
    if kind in (None, "action"):
        cards += [_card_action(a) for a in scoped(db.query(Action).filter(Action.status == "awaiting_approval"),
                                                 Action.project_id).all()]
    cards.sort(key=lambda c: (RISK_ORDER.get(c["risk"], 3), c["at"]))
    return cards


def card_json(card: dict, projects: dict[int, GrowthProject]) -> dict:
    p = projects.get(card["project_id"])
    out = {k: v for k, v in card.items() if k not in ("project_id", "at")}
    out["project"] = p.slug if p else ""
    out["id"] = f"{card['kind']}-{card['ref_id']}"
    out["at"] = card["at"].isoformat()
    return out


def history(db: DB, project_id: int | None = None, limit: int = 50) -> list[Approval]:
    q = db.query(Approval)
    if project_id is not None:
        q = q.filter(Approval.project_id == project_id)
    return q.order_by(Approval.id.desc()).limit(max(1, min(limit, 200))).all()


def _record(db: DB, project_id: int, kind: str, ref_id: int, title: str, requested_by: str, risk: str,
            approve: bool, via: str, note: str = "") -> Approval:
    row = Approval(project_id=project_id, kind=kind, ref_id=ref_id, title=title[:200], requested_by=requested_by[:48],
                   risk=risk, status="approved" if approve else "rejected", decided_via=via, note=note[:1000])
    db.add(row)
    db.commit()
    emit(db, project_id, f"approval.{row.status}", kind, ref_id, {"title": title[:120], "via": via})
    return row


def decide_task(db: DB, t: Task, approve: bool, via: str = "clique") -> None:
    """Aprovar é o portão: só tarefa aprovada roda. A fila (runner) é acionada por quem chama."""
    if t.status != "proposed":
        raise ApprovalError("esta tarefa não está aguardando aprovação")
    link = db.get(TaskLink, t.id)
    t.status = "approved" if approve else "rejected"
    t.decided_at = utcnow()
    db.commit()
    if not approve and link is not None:
        db.add(AgentNote(project_id=t.project_id, from_agent=t.agent_slug, to_agent=link.requested_by,
                         kind="resposta", text=f"o owner rejeitou o pedido \"{t.title}\"", task_id=t.id))
        db.commit()
    by = f"agente:{link.requested_by}" if link else f"rotina:{t.automation_id}" if t.automation_id else "plano" if t.plan_id else "owner"
    _record(db, t.project_id, "task", t.id, t.title, by, t.risk, approve, via)


def decide_content(db: DB, c: ContentItem, approve: bool, note: str = "") -> None:
    if c.status != "pending":
        raise ApprovalError("este conteúdo não está aguardando aprovação")
    c.status = "approved" if approve else "rejected"
    c.note = "" if approve else note.strip()
    c.decided_at = utcnow()
    cnt.touch(c)
    db.commit()
    _record(db, c.project_id, "content", c.id, c.title or c.body[:80], f"agente:{c.agent_slug}" if c.agent_slug else "owner",
            "baixo", approve, "clique", note)


def decide_mission(db: DB, m: Mission, approve: bool, note: str = "") -> None:
    if m.status != "awaiting_approval":
        raise ApprovalError("esta missão não está aguardando aprovação")
    m.status = "active" if approve else "cancelled"
    if not approve:
        m.finished_at = utcnow()
    db.commit()
    _record(db, m.project_id, "mission", m.id, m.title, m.origin, "medio", approve, "clique", note)


def decide_action(db: DB, a: Action, approve: bool, totp_ok: bool, note: str = "") -> None:
    """Aprovar uma ação externa só a libera (`approved`); quem executa é o conector, na fase de integrações."""
    if a.status != "awaiting_approval":
        raise ApprovalError("esta ação não está aguardando aprovação")
    if approve and a.risk in NEEDS_2FA["action"] and not totp_ok:
        raise ApprovalError("código 2FA inválido", 403)
    a.status = "approved" if approve else "rejected"
    if not approve:
        a.error, a.finished_at = (note.strip() or "rejeitada pelo owner")[:500], utcnow()
    db.commit()
    _record(db, a.project_id, "action", a.id, a.title or f"{a.integration}: {a.operation}",
            f"tarefa:{a.task_id}" if a.task_id else "agente", a.risk, approve,
            "2fa" if (approve and a.risk in NEEDS_2FA["action"]) else "clique", note)


def counts(db: DB, project_id: int | None = None) -> dict[str, int]:
    out = {k: 0 for k in KINDS}
    for c in pending(db, project_id):
        out[c["kind"]] += 1
    out["total"] = sum(out[k] for k in KINDS)
    return out


def record_action_release(db: DB, a: Action, requested_by: str) -> None:
    """Ação que o owner já liberou de antemão (config da integração): entra no histórico como `liberada`."""
    _record(db, a.project_id, "action", a.id, a.title or f"{a.integration}: {a.operation}", requested_by, a.risk, True, "liberada")
