"""Resetar dados: apaga atividades e threads, mantém a configuração, e só com RESETAR + 2FA."""
from datetime import date

from app import db as dbmod
from app import growth
from app import reset as rst

from .conftest import code


def _seed():
    """Uma linha em cada tabela, ligadas como no uso real."""
    s = dbmod.new_session()
    p = growth.create_project(s, "Loja")
    conv = dbmod.Conversation(title="t")
    s.add(conv)
    s.flush()
    s.add_all([
        dbmod.Message(conversation_id=conv.id, role="user", content="oi"),
        dbmod.CommandRun(conversation_id=conv.id, command="ls", risk="safe", status="done"),
    ])
    plan = dbmod.Plan(day=date(2026, 11, 2), status="ready")
    s.add(plan)
    s.flush()
    mission = dbmod.Mission(project_id=p.id, title="m", conversation_id=conv.id)
    s.add(mission)
    s.flush()
    t1 = dbmod.Task(plan_id=plan.id, project_id=p.id, agent_slug="growth", title="a", status="done",
                    conversation_id=conv.id, mission_id=mission.id)
    t2 = dbmod.Task(project_id=p.id, agent_slug="social", title="b", status="proposed")
    s.add_all([t1, t2])
    s.flush()
    s.add_all([
        dbmod.TaskLink(task_id=t2.id, parent_id=t1.id, requested_by="growth"),
        dbmod.AgentNote(project_id=p.id, from_agent="growth", text="x", task_id=t1.id),
        dbmod.ContentItem(project_id=p.id, task_id=t1.id, body="post"),
        dbmod.Action(project_id=p.id, task_id=t1.id, integration="telegram", operation="send", idempotency_key="k1"),
        dbmod.Approval(project_id=p.id, kind="task", ref_id=t1.id),
        dbmod.Event(project_id=p.id, type="task.done"),
        dbmod.Skill(project_id=p.id, name="skill", instructions="faça"),
        dbmod.Integration(project_id=p.id, kind="umami", config='{"url": "https://x"}'),
        dbmod.Automation(project_id=p.id, agent_slug="growth", title="rotina", last_run_on=date(2026, 11, 1)),
        dbmod.ModelSetting(activity="default", provider="claude"),
    ])
    m = dbmod.Metric(project_id=p.id, key="teste_reset", name="Teste")
    s.add(m)
    s.flush()
    s.add(dbmod.MetricPoint(metric_id=m.id, day=date(2026, 11, 1), value=3))
    s.commit()
    s.close()


def _snapshot(tables):
    """Conteúdo completo das tabelas mantidas, para provar que nada mudou."""
    s = dbmod.new_session()
    out = {}
    for t in dbmod.Base.metadata.sorted_tables:
        if t.name in tables:
            out[t.name] = sorted(tuple(r) for r in s.execute(t.select()).all())
    s.close()
    return out


KEEP = {t.name for t in dbmod.Base.metadata.sorted_tables} - rst.CLEAR_TABLES
KEEP_NO_AUDIT = KEEP - {"audit_log", "users"}  # o reset registra na auditoria e consome a janela 2FA


def _count(name):
    s = dbmod.new_session()
    t = dbmod.Base.metadata.tables[name]
    n = s.execute(t.select()).all()
    s.close()
    return len(n)


def test_lists_cover_every_table_and_config_is_kept():
    names = {t.name for t in dbmod.Base.metadata.sorted_tables}
    assert rst.CLEAR_TABLES <= names
    assert set(rst.KEEP_LABELS) == KEEP  # toda tabela nova precisa entrar numa das listas de propósito
    for keep in ("skills", "integrations", "automations", "agents", "project_agents", "growth_projects", "kpis"):
        assert keep not in rst.CLEAR_TABLES
    # Nenhuma tabela mantida aponta para uma apagada (senão o reset quebraria ou deixaria órfãos).
    for t in dbmod.Base.metadata.sorted_tables:
        if t.name in KEEP:
            assert not {fk.column.table.name for fk in t.foreign_keys} & rst.CLEAR_TABLES, t.name


def test_preview_counts_and_deletes_nothing(logged):
    _seed()
    before = _snapshot({t.name for t in dbmod.Base.metadata.sorted_tables})
    assert logged.get("/config").status_code == 200
    p = logged.get("/api/reset/preview").json()
    assert p["word"] == "RESETAR"
    clear = {r["table"]: r["count"] for r in p["clear"]}
    assert clear["conversations"] == 1 and clear["agent_tasks"] == 2 and clear["messages"] == 1
    keep = {r["table"]: r["count"] for r in p["keep"]}
    assert keep["skills"] >= 1 and keep["integrations"] >= 1 and keep["automations"] >= 1 and keep["kpi_points"] >= 1
    assert set(keep) == KEEP
    assert _snapshot(set(before)) == before


def test_reset_clears_activity_and_keeps_config(logged):
    _seed()
    kept = _snapshot(KEEP_NO_AUDIT)
    r = logged.post("/api/reset", json={"confirm": "RESETAR", "totp": code()})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["conversations"] == 1
    for name in rst.CLEAR_TABLES:
        assert _count(name) == 0, name
    assert _snapshot(KEEP_NO_AUDIT) == kept
    actions = [a["action"] for a in logged.get("/api/audit").json()]
    assert "data_reset" in actions
    # O sistema segue funcionando depois do reset.
    assert logged.post("/api/conversations").status_code == 200


def test_reset_refused_without_confirmation(logged):
    _seed()
    before = _snapshot({t.name for t in dbmod.Base.metadata.sorted_tables} - {"audit_log", "users"})
    for word in ("", "resetar", "RESET", "SIM"):
        r = logged.post("/api/reset", json={"confirm": word, "totp": code()})
        assert r.status_code == 400, word
    r = logged.post("/api/reset", json={"confirm": "RESETAR", "totp": "000000"})
    assert r.status_code == 403
    r = logged.post("/api/reset", json={"confirm": "RESETAR"})
    assert r.status_code == 422
    assert _snapshot(set(before)) == before


def test_reset_needs_login_and_csrf(client):
    assert client.post("/api/reset", json={"confirm": "RESETAR", "totp": code()}).status_code in (401, 403)


def test_reset_function_refuses_bad_word_and_running_work(client):
    _seed()
    s = dbmod.new_session()
    try:
        rst.reset_data(s, "nao", "owner")
        raise AssertionError("devia recusar")
    except rst.ResetError as e:
        assert e.status == 400
    t = s.query(dbmod.Task).first()
    t.status = "running"
    s.commit()
    try:
        rst.reset_data(s, "RESETAR", "owner")
        raise AssertionError("devia recusar")
    except rst.ResetError as e:
        assert e.status == 409
    s.close()
    assert _count("agent_tasks") == 2


def test_reset_is_one_transaction(client, monkeypatch):
    """Se uma exclusão falha no meio, nada é apagado."""
    _seed()
    before = _snapshot(rst.CLEAR_TABLES)
    real = rst.CLEAR[:]
    boom = type("Boom", (), {"__tablename__": "boom"})
    monkeypatch.setattr(rst, "CLEAR", real[:6] + [(boom, "x")] + real[6:])
    s = dbmod.new_session()
    try:
        rst.reset_data(s, "RESETAR", "owner")
        raise AssertionError("devia falhar")
    except Exception as e:  # noqa: BLE001
        assert not isinstance(e, AssertionError)
    s.close()
    assert _snapshot(rst.CLEAR_TABLES) == before


def test_sidebar_has_settings_icon(logged):
    side = logged.get("/dashboard").text
    assert 'href="/config"' in side and 'aria-label="Configurações"' in side
