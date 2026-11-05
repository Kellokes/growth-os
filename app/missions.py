"""Missões: um objetivo que passa por vários agentes do mesmo projeto.

Fluxo: a IA da thread (ou o Coordenador) emite um bloco ```missao```; vira UMA missão aguardando aprovação,
com as tarefas já criadas mas paradas. Aprovada, as tarefas sem dependência entram na fila e as demais ficam em
`waiting/agente` até a anterior terminar. Cada tarefa que termina libera as seguintes; quando todas terminam, a
missão fecha e o resumo volta para a thread onde o owner pediu. O orçamento (tarefas e passos) para a missão
e pede ao owner para liberar mais.
"""

import json
import re

from sqlalchemy.orm import Session as DB

from . import approvals as appr
from .db import Conversation, GrowthProject, Message, Mission, ProjectAgent, Task, utcnow

MAX_MISSION_TASKS = 10
TERMINAL = ("done", "failed", "rejected")
_BLOCK = re.compile(r"```missao[ \t]*\n(.*?)```", re.S)
_RISKS = ("baixo", "medio", "alto")


def _items(raw: str) -> list[dict]:
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        return []
    return [d for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict)]


def parse(text: str) -> list[dict]:
    return [it for raw in _BLOCK.findall(text or "") for it in _items(raw)]


def _team(db: DB, project: GrowthProject) -> set[str]:
    return {pa.agent_slug for pa in db.query(ProjectAgent).filter(ProjectAgent.project_id == project.id).all()}


def _add_tasks(db: DB, m: Mission, items: list, status: str, via: str = "") -> tuple[list[Task], list[str]]:
    """Cria as tarefas da missão (agentes do projeto, no máximo MAX_MISSION_TASKS). `depende` aponta para um `id` do bloco."""
    project = db.get(GrowthProject, m.project_id)
    slugs = _team(db, project)
    made: dict[str, Task] = {}
    ordered: list[Task] = []
    refused: list[str] = []
    existing = db.query(Task).filter(Task.mission_id == m.id).count()
    for i, it in enumerate(items):
        agent = str(it.get("agente", "")).strip().lower()
        title = str(it.get("titulo", "")).strip()[:200]
        if not title or agent not in slugs:
            refused.append(f"tarefa inválida ({agent or '?'})")
            continue
        if existing + len(ordered) >= MAX_MISSION_TASKS:
            refused.append(f"mais de {MAX_MISSION_TASKS} tarefas na missão")
            break
        risk = str(it.get("risco", "baixo")).lower().replace("é", "e")
        t = Task(
            project_id=m.project_id, agent_slug=agent, title=title, status=status, mission_id=m.id,
            rationale=f"Missão: {m.title}"[:500], steps=str(it.get("detalhes", "")).strip()[:4000],
            risk=risk if risk in _RISKS else "baixo",
        )
        db.add(t)
        db.flush()
        made[str(it.get("id", i))] = t
        ordered.append(t)
    for it in items:
        t = made.get(str(it.get("id", "")))
        dep = made.get(str(it.get("depende", ""))) if it.get("depende") else None
        if t is not None and dep is not None and dep.id != t.id:
            t.depends_on = dep.id
    db.commit()
    return ordered, refused


def create_from_chat(db: DB, cid: int, text: str) -> list[Mission]:
    """Lê os blocos ```missao``` da resposta da IA numa thread do chat. Fica aguardando a aprovação do owner."""
    from . import growth  # import tardio: growth importa este módulo

    created: list[Mission] = []
    for it in parse(text)[:2]:
        project = growth.get_project(db, str(it.get("projeto", "")).strip().lower())
        title = str(it.get("titulo", "")).strip()[:200]
        if project is None or not title:
            continue
        if db.query(Mission).filter(Mission.conversation_id == cid, Mission.title == title).count():
            continue  # a mesma resposta lida duas vezes não duplica a missão
        m = Mission(project_id=project.id, title=title, goal=str(it.get("objetivo", "")).strip()[:2000],
                    origin="chat", conversation_id=cid, status="awaiting_approval")
        db.add(m)
        db.commit()
        tasks, _ = _add_tasks(db, m, it.get("tarefas") if isinstance(it.get("tarefas"), list) else [], "proposed")
        if not tasks:  # sem plano: o Coordenador do projeto monta
            db.add(Task(
                project_id=m.project_id, agent_slug="coordenador", title=f"Planejar a missão: {title}"[:200],
                status="proposed", mission_id=m.id, risk="baixo", rationale=f"Missão: {title}"[:500],
                steps=("Quebre o objetivo em tarefas para os agentes do time e responda com um bloco ```missao "
                       'com {"tarefas": [{"id": "a", "agente": "slug", "titulo": "...", "detalhes": "...", '
                       '"depende": "id opcional"}]}. Objetivo: ' + m.goal)[:4000],
            ))
            db.commit()
        appr.emit(db, m.project_id, "mission.proposed", "mission", m.id, {"title": title})
        created.append(m)
    return created


def expand(db: DB, task: Task, text: str) -> list[int]:
    """O Coordenador respondeu com o plano: as tarefas entram na missão já aprovada (aprovação herdada)."""
    if task.mission_id is None or task.agent_slug != "coordenador":
        return []
    m = db.get(Mission, task.mission_id)
    if m is None or m.status != "active":
        return []
    items = [t for it in parse(text) for t in (it.get("tarefas") if isinstance(it.get("tarefas"), list) else [])]
    tasks, _ = _add_tasks(db, m, items, "proposed")
    return _release(db, tasks, "herdada")


def _release(db: DB, tasks: list[Task], via: str) -> list[int]:
    ids = []
    for t in tasks:
        t.decided_at = utcnow()
        if t.depends_on:
            dep = db.get(Task, t.depends_on)
            if dep is not None and dep.status == "done":
                t.status = "approved"
                ids.append(t.id)
            else:
                t.status, t.wait_reason = "waiting", "agente"
        else:
            t.status = "approved"
            ids.append(t.id)
        appr.record_inherited(db, t, via)
    db.commit()
    return ids


def activate(db: DB, m: Mission) -> list[int]:
    """Missão aprovada: tarefas sem dependência entram na fila; as outras esperam. Ids para a fila."""
    held = db.query(Task).filter(Task.mission_id == m.id, Task.status == "waiting", Task.wait_reason == "orcamento").all()
    if held:  # voltou de uma pausa por orçamento: o owner liberou mais
        m.budget_tasks += max(m.budget_tasks, 4)
        m.budget_steps += max(m.budget_steps, 40)
    ids = _release(db, db.query(Task).filter(Task.mission_id == m.id, Task.status == "proposed").order_by(Task.id).all(),
                   "herdada")
    for t in held:
        t.status, t.wait_reason = "approved", ""
        ids.append(t.id)
    db.commit()
    return ids


def cancel(db: DB, m: Mission) -> None:
    for t in db.query(Task).filter(Task.mission_id == m.id, Task.status.in_(("proposed", "waiting", "approved"))).all():
        t.status, t.wait_reason, t.finished_at = "rejected", "", utcnow()
    m.status, m.finished_at = "cancelled", utcnow()
    db.commit()


def hold_if_blocked(db: DB, task: Task) -> bool:
    """Antes de rodar uma tarefa de missão: cancelada não roda; orçamento estourado pausa e pede ao owner."""
    if task.mission_id is None:
        return False
    m = db.get(Mission, task.mission_id)
    if m is None:
        return False
    if m.status in ("cancelled", "failed", "done"):
        task.status, task.finished_at = "rejected", utcnow()
        db.commit()
        return True
    if m.used_tasks >= m.budget_tasks or m.used_steps >= m.budget_steps:
        task.status, task.wait_reason = "waiting", "orcamento"
        m.status = "awaiting_approval"
        note = "\n⚠ Orçamento da missão atingido: aprove para liberar mais tarefas e passos."
        if note not in m.goal:
            m.goal += note
        db.commit()
        appr.emit(db, m.project_id, "mission.budget", "mission", m.id, {"title": m.title})
        return True
    m.used_tasks += 1
    db.commit()
    return False


def _steps(db: DB, task: Task) -> int:
    if task.conversation_id is None:
        return 0
    return db.query(Message).filter(Message.conversation_id == task.conversation_id, Message.role == "assistant").count()


def settle(db: DB, task: Task) -> tuple[list[int], list[str]]:
    """Uma tarefa chegou ao fim (feita, falhou ou rejeitada): libera as que dependiam dela e fecha a missão se acabou.

    Retorna (ids para a fila, avisos para o owner)."""
    ids: list[int] = []
    notices: list[str] = []
    m = db.get(Mission, task.mission_id) if task.mission_id else None
    if m is not None:
        m.used_steps += _steps(db, task)
    for d in db.query(Task).filter(Task.depends_on == task.id, Task.status == "waiting", Task.wait_reason == "agente").all():
        if task.status == "done":
            d.status, d.wait_reason = "approved", ""
            ids.append(d.id)
        else:
            d.status, d.wait_reason, d.finished_at = "failed", "", utcnow()
            d.result = f"a tarefa anterior \"{task.title}\" não foi concluída"
            sub_ids, sub_notes = settle(db, d)
            ids += sub_ids
            notices += sub_notes
    db.commit()
    if m is not None:
        note = _close_if_done(db, m)
        if note:
            notices.append(note)
    return ids, notices


def _close_if_done(db: DB, m: Mission) -> str:
    if m.status != "active":
        return ""
    tasks = db.query(Task).filter(Task.mission_id == m.id).order_by(Task.id).all()
    if not tasks or any(t.status not in TERMINAL for t in tasks):
        return ""
    ok = sum(t.status == "done" for t in tasks)
    m.status, m.finished_at = ("done" if ok else "failed"), utcnow()
    db.commit()
    lines = []
    for t in tasks:
        res = re.sub(r"```.*?```", "", t.result or "", flags=re.S).strip().replace("\n", " ")[:300]
        mark = {"done": "✅", "failed": "⚠️", "rejected": "🚫"}.get(t.status, "•")
        lines.append(f"{mark} **{t.agent_slug}** · {t.title}" + (f"\n   {res}" if res and t.status == "done" else ""))
    head = "Missão concluída" if m.status == "done" else "Missão terminou sem resultado"
    summary = f"{head}: **{m.title}** ({ok}/{len(tasks)} tarefas)\n\n" + "\n".join(lines)
    if m.conversation_id and db.get(Conversation, m.conversation_id) is not None:
        db.add(Message(conversation_id=m.conversation_id, role="assistant", provider="growth-os", content=summary))
        db.commit()
    appr.emit(db, m.project_id, f"mission.{m.status}", "mission", m.id, {"title": m.title, "done": ok})
    return f"{'🏁' if m.status == 'done' else '⚠️'} {head}: {m.title} ({ok}/{len(tasks)})"


def tasks_of(db: DB, m: Mission) -> list[Task]:
    return db.query(Task).filter(Task.mission_id == m.id).order_by(Task.id).all()
