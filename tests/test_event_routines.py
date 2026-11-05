"""Fase 6: rotinas disparadas por evento do projeto."""
import json
from datetime import timedelta

from app import actions as acts
from app import approvals, growth, routines
from app import db as dbmod

from .test_growth import make_projects, pid  # noqa: F401


def _auto(slug, event, agent="tech", flt="", minimum=20, auto=False, enabled=True) -> int:
    s = dbmod.new_session()
    a = dbmod.Automation(project_id=pid(slug), agent_slug=agent, title=f"Reagir a {event}", steps="olhe e proponha",
                         frequency="event", event_type=event, event_filter=flt, event_min=minimum,
                         auto_approve=auto, enabled=enabled)
    s.add(a)
    s.commit()
    aid = a.id
    s.close()
    return aid


def _emit(slug, type_, payload=None, age_hours=0):
    s = dbmod.new_session()
    ev = approvals.emit(s, pid(slug), type_, "x", 1, payload or {})
    if age_hours:
        ev.created_at = dbmod.utcnow() - timedelta(hours=age_hours)
        s.commit()
    s.close()


def _dispatch():
    s = dbmod.new_session()
    out = routines.dispatch_events(s)
    ids = [(t.automation_id, t.status, t.steps) for t in out]
    s.close()
    return ids


def test_site_down_event_creates_proposed_task_with_context(logged):
    (slug,) = make_projects("Zombie Party")
    aid = _auto(slug, "site.down")
    _emit(slug, "site.down", {"site": slug, "uptime": 90.5})
    [(a, status, steps)] = _dispatch()
    assert (a, status) == (aid, "proposed") and "site.down" in steps and "90.5" in steps
    assert _dispatch() == []  # o evento só dispara uma vez


def test_auto_approve_goes_straight_to_queue_and_disabled_or_other_project_is_ignored(logged):
    a, b = make_projects("Zombie Party", "A Ilha")
    _auto(a, "site.down", auto=True)
    _auto(a, "site.down", agent="growth", enabled=False)
    _auto(b, "site.down")
    _emit(a, "site.down")
    out = _dispatch()
    assert [(x[1]) for x in out] == ["approved"] and len(out) == 1  # a rotina do outro projeto não dispara


def test_never_stacks_open_runs(logged):
    (slug,) = make_projects("Zombie Party")
    _auto(slug, "site.down")
    _emit(slug, "site.down")
    _emit(slug, "site.down")
    assert len(_dispatch()) == 1


def test_kpi_drop_threshold_and_direction(logged):
    (slug,) = make_projects("Zombie Party")
    _auto(slug, "kpi.updated", agent="growth", flt="visitantes", minimum=20)
    s = dbmod.new_session()
    day = growth.local_today()
    growth.record_point(s, pid(slug), "visitantes", 100, day - timedelta(days=1))
    growth.record_point(s, pid(slug), "visitantes", 90, day)  # -10%: não basta
    growth.record_point(s, pid(slug), "cac", 10, day - timedelta(days=1))
    growth.record_point(s, pid(slug), "cac", 20, day)  # outro KPI: filtro
    s.close()
    assert _dispatch() == []
    s = dbmod.new_session()
    growth.record_point(s, pid(slug), "visitantes", 70, day)  # -30%: dispara
    s.close()
    [(_, status, steps)] = _dispatch()
    assert status == "proposed" and "visitantes" in steps and "-30.0" in steps


def test_kpi_that_should_go_down_worsens_when_it_rises(logged):
    (slug,) = make_projects("Zombie Party")
    _auto(slug, "kpi.updated", agent="marketing", flt="cac", minimum=50)
    s = dbmod.new_session()
    day = growth.local_today()
    growth.record_point(s, pid(slug), "cac", 10, day - timedelta(days=1))
    growth.record_point(s, pid(slug), "cac", 20, day)  # CAC dobrou = piorou 100%
    s.close()
    assert len(_dispatch()) == 1


def test_old_events_do_not_fire(logged):
    (slug,) = make_projects("Zombie Party")
    _auto(slug, "site.down")
    _emit(slug, "site.down", age_hours=30)
    assert _dispatch() == []


def test_action_failed_filter_by_integration(logged):
    (slug,) = make_projects("Zombie Party")
    _auto(slug, "action.failed", agent="coordenador", flt="postiz")
    _emit(slug, "action.failed", {"integration": "email"})
    assert _dispatch() == []
    _emit(slug, "action.failed", {"integration": "postiz"})
    assert len(_dispatch()) == 1


def test_uptime_collection_below_99_emits_site_down_and_fires_routine(logged, monkeypatch):
    import httpx

    from .test_integration_exec import connect, mock

    (slug,) = make_projects("Zombie Party")
    connect(slug, "uptime_kuma", {"url": "https://status.exemplo.com", "status_page": "app"}, monkeypatch)
    _auto(slug, "site.down")

    def handler(req: httpx.Request):
        return httpx.Response(200, json={"heartbeatList": {"1": [{"status": 0}]}, "uptimeList": {"1_24": 0.8}})

    import asyncio

    s = dbmod.new_session()
    asyncio.run(acts.collect_kpis(s, mock(handler)))
    assert s.query(dbmod.Event).filter_by(type="site.down").count() == 1
    s.close()
    [(_, status, steps)] = _dispatch()
    assert status == "proposed" and "uptime_kuma" in steps


def test_api_creates_event_routine_and_validates(logged):
    make_projects("Zombie Party")
    cat = logged.get("/api/routines/catalog").json()
    assert {e["key"] for e in cat["events"]} == set(routines.EVENTS)
    base = {"agent": "tech", "title": "Investigar queda", "steps": "olhe os logs", "frequency": "event"}
    assert logged.post("/api/gprojects/zombie-party/automations", json=base).status_code == 422  # falta o evento
    r = logged.post("/api/gprojects/zombie-party/automations", json={**base, "event_type": "nao.existe"})
    assert r.status_code == 422
    r = logged.post("/api/gprojects/zombie-party/automations", json={**base, "event_type": "site.down"})
    assert r.status_code == 200
    j = r.json()
    assert j["event_type"] == "site.down" and j["next_run"] == "" and j["schedule"].startswith("Quando:")


def test_marking_content_sent_emits_event(logged):
    (slug,) = make_projects("Zombie Party")
    _auto(slug, "content.sent", agent="social")
    s = dbmod.new_session()
    c = dbmod.ContentItem(project_id=pid(slug), agent_slug="social", channel="instagram", title="Post",
                          body="oi", status="approved")
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    assert logged.post(f"/api/content/{cid}/sent", json={"link": "https://i.example/p/1"}).status_code == 200
    [(_, status, steps)] = _dispatch()
    assert status == "proposed" and "instagram" in steps
