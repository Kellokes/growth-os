"""Fase 5: missões, Coordenador, esperas (agente e ação externa), aprovação herdada, orçamento e fila com workers."""
import asyncio
import json

from app import db as dbmod
from app import growth, missions

from .test_growth import _run, _task, fake, make_projects, pid, submitted  # noqa: F401

PLAN = {
    "projeto": "zombie-party", "titulo": "Campanha de Natal", "objetivo": "post publicado com o link",
    "tarefas": [
        {"id": "a", "agente": "design", "titulo": "Criar a arte", "detalhes": "1080x1080"},
        {"id": "b", "agente": "social", "titulo": "Escrever o post", "depende": "a"},
        {"id": "x", "agente": "ninguem", "titulo": "agente que não existe"},
    ],
}


def block(obj) -> str:
    return "Proponho uma missão:\n```missao\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


def _conv() -> int:
    s = dbmod.new_session()
    c = dbmod.Conversation(title="Pedido do owner")
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def _mission_from_chat(plan=PLAN) -> tuple[int, int]:
    cid = _conv()
    s = dbmod.new_session()
    (m,) = missions.create_from_chat(s, cid, block(plan))
    mid = m.id
    s.close()
    return mid, cid


def _tasks(mid):
    s = dbmod.new_session()
    rows = s.query(dbmod.Task).filter_by(mission_id=mid).order_by(dbmod.Task.id).all()
    s.expunge_all()
    s.close()
    return rows


def _approve_mission(client, mid):
    return client.post("/api/approvals/decide", json={"items": [{"kind": "mission", "ref_id": mid}], "approve": True})


def test_mission_from_chat_is_one_card_and_ignores_invalid_agents(logged):
    make_projects("Zombie Party")
    mid, cid = _mission_from_chat()
    t = _tasks(mid)
    assert [(x.agent_slug, x.status) for x in t] == [("design", "proposed"), ("social", "proposed")]
    assert t[1].depends_on == t[0].id
    items = logged.get("/api/approvals").json()["items"]
    assert [i["kind"] for i in items] == ["mission"]  # as tarefas da missão não têm cartão próprio
    assert "1. design: Criar a arte" in items[0]["preview"] and "(depois da 1)" in items[0]["preview"]
    s = dbmod.new_session()
    assert missions.create_from_chat(s, cid, block(PLAN)) == []  # a mesma resposta não duplica
    assert missions.create_from_chat(s, cid, block({**PLAN, "projeto": "nao-existe"})) == []
    s.close()


def test_mission_task_cannot_be_decided_alone(logged):
    make_projects("Zombie Party")
    mid, _ = _mission_from_chat()
    r = logged.post("/api/approvals/decide", json={"items": [{"kind": "task", "ref_id": _tasks(mid)[0].id}], "approve": True})
    assert r.status_code == 409 and "missão" in r.json()["detail"]


def test_approving_mission_queues_first_task_and_chain_runs_to_summary(logged, fake, submitted):
    make_projects("Zombie Party")
    mid, cid = _mission_from_chat()
    assert _approve_mission(logged, mid).status_code == 200
    a, b = _tasks(mid)
    assert (a.status, b.status, b.wait_reason) == ("approved", "waiting", "agente")
    assert submitted == [a.id]

    fake(["Arte pronta."])
    _run(a.id)
    assert submitted == [a.id, b.id] and _tasks(mid)[1].status == "approved"  # terminou a anterior: libera a seguinte

    fake(["Post escrito."])
    _run(b.id)
    s = dbmod.new_session()
    m = s.get(dbmod.Mission, mid)
    assert m.status == "done" and m.used_tasks == 2 and m.used_steps >= 2
    summary = s.query(dbmod.Message).filter_by(conversation_id=cid, role="assistant").one()
    assert "Missão concluída" in summary.content and "Arte pronta." in summary.content
    assert s.query(dbmod.Approval).filter_by(kind="task", decided_via="herdada").count() == 2
    s.close()


def test_failed_dependency_fails_the_chain_and_closes_mission(logged, submitted):
    make_projects("Zombie Party")
    mid, cid = _mission_from_chat()
    _approve_mission(logged, mid)
    a, b = _tasks(mid)
    s = dbmod.new_session()
    t = s.get(dbmod.Task, a.id)
    t.status, t.result = "failed", "provider caiu"
    s.commit()
    growth.finish(s, t)
    s.close()
    a, b = _tasks(mid)
    assert b.status == "failed" and "não foi concluída" in b.result
    s = dbmod.new_session()
    assert s.get(dbmod.Mission, mid).status == "failed"
    assert "terminou sem resultado" in s.query(dbmod.Message).filter_by(conversation_id=cid).one().content
    s.close()


def test_reject_mission_cancels_its_tasks(logged):
    make_projects("Zombie Party")
    mid, _ = _mission_from_chat()
    r = logged.post("/api/approvals/decide", json={"items": [{"kind": "mission", "ref_id": mid}], "approve": False})
    assert r.status_code == 200
    assert {t.status for t in _tasks(mid)} == {"rejected"}
    assert logged.get("/api/approvals").json()["counts"]["total"] == 0


def test_mission_without_plan_asks_the_coordinator_who_expands_it(logged, fake, submitted):
    make_projects("Zombie Party")
    mid, _ = _mission_from_chat({"projeto": "zombie-party", "titulo": "Lançar o app", "objetivo": "app no ar"})
    (c,) = _tasks(mid)
    assert c.agent_slug == "coordenador" and "bloco ```missao" in c.steps
    _approve_mission(logged, mid)
    plan = {"tarefas": [{"id": "1", "agente": "tech", "titulo": "Preparar o deploy"},
                        {"id": "2", "agente": "growth", "titulo": "Medir o lançamento", "depende": "1"}]}
    fake([block(plan)])
    _run(c.id)
    rows = _tasks(mid)
    assert [r.agent_slug for r in rows] == ["coordenador", "tech", "growth"]
    assert rows[1].status == "approved" and rows[2].status == "waiting" and rows[2].depends_on == rows[1].id
    assert submitted == [c.id, rows[1].id]
    assert rows[0].status == "done"  # a missão segue aberta: ainda há tarefas
    s = dbmod.new_session()
    assert s.get(dbmod.Mission, mid).status == "active"
    s.close()


def test_coordinator_is_not_asked_for_a_daily_plan(logged, fake):
    (slug,) = make_projects("Zombie Party")
    p = fake([json.dumps({"tarefas": []})] * 12)
    asyncio.run(growth.generate_plan())
    assert len(p.seen) == 9  # os 9 agentes de área; o Coordenador não propõe tarefas no plano


def test_handoff_with_wait_blocks_requester_then_resumes_with_the_answer(logged, fake, submitted):
    make_projects("Zombie Party")
    tid = _task("growth", "Montar a oferta")
    fake(['```pedido\n{"para": "design", "titulo": "Arte da oferta", "detalhes": "banner", "esperar": true}\n```'])
    _run(tid)
    s = dbmod.new_session()
    parent = s.get(dbmod.Task, tid)
    assert (parent.status, parent.wait_reason, parent.finished_at) == ("waiting", "agente", None)
    link = s.query(dbmod.TaskLink).filter_by(parent_id=tid).one()
    assert link.wait == 1
    child = link.task_id
    s.close()
    assert logged.get("/api/board").status_code == 200  # a espera aparece no Kanban sem quebrar

    logged.post("/api/tasks/decide", json={"ids": [child], "approve": True})
    p = fake(["Arte feita: banner.png"])
    _run(child)
    assert submitted[-1] == tid  # a última resposta que faltava: quem pediu volta para a fila

    p2 = fake(["Oferta montada com a arte."])
    _run(tid)
    resume = p2.seen[0][-1][1]
    assert resume.startswith(growth.RESUME_MARK) and "Arte feita: banner.png" in resume
    s = dbmod.new_session()
    parent = s.get(dbmod.Task, tid)
    assert parent.status == "done" and "Oferta montada" in parent.result
    s.close()


def test_rejecting_awaited_handoff_resumes_requester(logged, fake, submitted):
    make_projects("Zombie Party")
    tid = _task("growth", "Montar a oferta")
    fake(['```pedido\n{"para": "design", "titulo": "Arte", "esperar": true}\n```'])
    _run(tid)
    s = dbmod.new_session()
    child = s.query(dbmod.TaskLink).filter_by(parent_id=tid).one().task_id
    s.close()
    logged.post("/api/tasks/decide", json={"ids": [child], "approve": False})
    assert submitted == [tid]
    p = fake(["Sigo sem a arte."])
    _run(tid)
    assert "rejected" in p.seen[0][-1][1]


def test_low_risk_handoff_inside_approved_mission_is_inherited(logged, fake, submitted):
    make_projects("Zombie Party")
    mid, _ = _mission_from_chat()
    _approve_mission(logged, mid)
    a, _b = _tasks(mid)
    submitted.clear()
    fake(['```pedido\n{"para": "growth", "titulo": "Link com UTM", "risco": "baixo"}\n```\n'
          '```pedido\n{"para": "crm", "titulo": "Disparo para a base", "risco": "medio"}\n```'])
    _run(a.id)
    s = dbmod.new_session()
    kids = {t.agent_slug: t for t in s.query(dbmod.Task).filter(dbmod.Task.id.in_(
        [lk.task_id for lk in s.query(dbmod.TaskLink).filter_by(parent_id=a.id)])).all()}
    assert (kids["growth"].status, kids["growth"].mission_id) == ("approved", mid)
    assert kids["crm"].status == "proposed" and kids["crm"].mission_id == mid  # risco médio continua pedindo
    assert submitted == [kids["growth"].id, _b.id]  # o herdado e a tarefa seguinte da missão; o de risco médio não
    assert s.query(dbmod.Approval).filter_by(ref_id=kids["growth"].id, decided_via="herdada").count() == 1
    s.close()
    cards = logged.get("/api/approvals").json()["items"]
    assert [(c["kind"], c["agent"]) for c in cards] == [("task", "crm")]


def test_handoff_outside_a_mission_still_needs_approval(logged, fake, submitted):
    make_projects("Zombie Party")
    tid = _task("growth", "Solta")
    fake(['```pedido\n{"para": "design", "titulo": "Arte", "risco": "baixo"}\n```'])
    _run(tid)
    assert submitted == []
    assert [c["kind"] for c in logged.get("/api/approvals").json()["items"]] == ["task"]


def test_budget_pauses_the_mission_until_owner_releases_more(logged, fake, submitted):
    make_projects("Zombie Party")
    mid, _ = _mission_from_chat()
    s = dbmod.new_session()
    s.get(dbmod.Mission, mid).budget_tasks = 1
    s.commit()
    s.close()
    _approve_mission(logged, mid)
    a, b = _tasks(mid)
    fake(["Arte pronta."])
    _run(a.id)
    _run(b.id)  # a segunda tarefa estoura o orçamento
    s = dbmod.new_session()
    assert (s.get(dbmod.Task, b.id).status, s.get(dbmod.Task, b.id).wait_reason) == ("waiting", "orcamento")
    m = s.get(dbmod.Mission, mid)
    assert m.status == "awaiting_approval" and "Orçamento" in m.goal
    s.close()
    card = logged.get("/api/approvals").json()["items"][0]
    assert card["kind"] == "mission"
    submitted.clear()
    assert _approve_mission(logged, mid).status_code == 200
    assert submitted == [b.id]
    s = dbmod.new_session()
    assert s.get(dbmod.Mission, mid).budget_tasks > 1 and s.get(dbmod.Task, b.id).status == "approved"
    s.close()


def test_task_waits_for_external_action_and_resumes_after_decision(logged, fake, submitted, monkeypatch):
    (slug,) = make_projects("Zombie Party")
    tid = _task("social", "Publicar o post")
    s = dbmod.new_session()
    act = dbmod.Action(project_id=pid(slug), task_id=tid, integration="postiz", operation="publicar_post",
                       effect="external", risk="medio", status="awaiting_approval", params="{}", idempotency_key="k-wait")
    s.add(act)
    s.commit()
    aid = act.id
    s.close()
    fake(["Pedi para publicar; aguardo a aprovação."])
    _run(tid)
    s = dbmod.new_session()
    t = s.get(dbmod.Task, tid)
    assert (t.status, t.wait_reason, t.finished_at) == ("waiting", "acao", None)
    s.close()

    r = logged.post("/api/approvals/decide", json={"items": [{"kind": "action", "ref_id": aid}], "approve": False})
    assert r.status_code == 200 and submitted == [tid]
    p = fake(["A publicação foi rejeitada; deixei o rascunho."])
    _run(tid)
    assert "postiz.publicar_post: rejected" in p.seen[0][-1][1]
    s = dbmod.new_session()
    assert s.get(dbmod.Task, tid).status == "done"
    s.close()


def test_action_wait_survives_a_chat_in_the_task_conversation(logged, fake):
    (slug,) = make_projects("Zombie Party")
    tid = _task("social", "Publicar")
    s = dbmod.new_session()
    s.add(dbmod.Action(project_id=pid(slug), task_id=tid, integration="postiz", operation="publicar_post",
                       effect="external", risk="medio", status="awaiting_approval", params="{}", idempotency_key="k-w2"))
    s.commit()
    s.close()
    fake(["ok"])
    _run(tid)
    s = dbmod.new_session()
    cid = s.get(dbmod.Task, tid).conversation_id
    growth.sync_task_for_conversation(s, cid)  # não pode marcar como feita
    assert s.get(dbmod.Task, tid).status == "waiting"
    s.close()


def test_runner_runs_two_at_once_but_one_per_agent_and_project(logged, monkeypatch):
    make_projects("Zombie Party")
    t1, t2, t3 = _task("growth", "g1"), _task("growth", "g2"), _task("design", "d1")
    log: list[tuple[str, int]] = []
    active: set[str] = set()
    peak = []

    async def fake_exec(tid):
        s = dbmod.new_session()
        slug = s.get(dbmod.Task, tid).agent_slug
        s.close()
        assert slug not in active, "dois do mesmo agente ao mesmo tempo"
        active.add(slug)
        peak.append(len(active))
        log.append(("start", tid))
        await asyncio.sleep(0.4)
        active.discard(slug)
        log.append(("end", tid))

    monkeypatch.setattr(growth, "execute_task", fake_exec)

    async def go():
        r = growth.TaskRunner()
        r.bind(asyncio.get_running_loop())
        for tid in (t1, t2, t3):
            r.submit(tid)
        for _ in range(60):
            await asyncio.sleep(0.1)
            if sum(1 for k, _ in log if k == "end") == 3:
                break

    asyncio.run(go())
    assert max(peak) == 2 and sum(1 for k, _ in log if k == "end") == 3
    assert log.index(("end", t1)) < log.index(("start", t2))
