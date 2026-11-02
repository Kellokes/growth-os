"""Catálogo único de eventos de analytics dos projetos e leitura do envelope padronizado do ``/daily``.

Os três projetos expõem ``GET /daily?from=YYYY-MM-DD&to=YYYY-MM-DD`` com o mesmo envelope:
``periodo``, ``include_internal``, ``totals`` (``{evento: {events, actors}}``) e ``series``
(um item por dia: ``{date, events: {evento: n}, actors: {evento: n}}``). O nome do evento é a chave
do KPI no Growth OS, então a integração não precisa de mapeamento escrito à mão.

Tudo em código, sem LLM.
"""

from datetime import date

# Nomes ainda não confirmados pelo Felipe (2026-11-02). Se mudarem, ajuste SÓ aqui.
SIGNUP_STARTED = "signup_started"
SIGNUP_COMPLETED = "signup_completed"
LOGIN = "login"
SESSION_START = "session_start"

# evento (= chave do KPI), nome no dashboard, área (agente dono do KPI)
CATALOG: list[tuple[str, str, str]] = [
    ("landing_view", "Landing: visualizações", "growth"),
    ("landing_section_view", "Landing: seções vistas", "growth"),
    (SIGNUP_STARTED, "Cadastros iniciados", "growth"),
    (SIGNUP_COMPLETED, "Cadastros concluídos", "growth"),
    (LOGIN, "Logins", "produto"),
    (SESSION_START, "Sessões iniciadas", "produto"),
    ("app_ready", "App carregado", "produto"),
    ("screen_view", "Telas vistas", "produto"),
    ("page_view", "Páginas vistas", "produto"),
    ("pwa_prompt", "Convites de instalação (PWA)", "produto"),
]
EVENTS = [name for name, *_ in CATALOG]

# KPIs padrão do dashboard que saem direto de um evento do catálogo: kpi -> (evento, "events" | "actors")
DERIVED = {
    "cadastros": (SIGNUP_COMPLETED, "events"),
    "usuarios_ativos": (SESSION_START, "actors"),
}

DAILY_PATH = "/daily?from={ontem}&to={ontem}"


def is_envelope(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("totals"), dict) and isinstance(data.get("series"), list)


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _day_counts(data: dict, day: date) -> tuple[dict, dict, bool]:
    """(events, actors, achou_o_dia) do dia pedido; sem o dia na série, usa totals se o período for só esse dia."""
    for item in data["series"]:
        if isinstance(item, dict) and item.get("date") == day.isoformat():
            ev, ac = item.get("events"), item.get("actors")
            return (ev if isinstance(ev, dict) else {}), (ac if isinstance(ac, dict) else {}), True
    per = data.get("periodo") if isinstance(data.get("periodo"), dict) else {}
    if per.get("from") == per.get("to") == day.isoformat():
        tot = data["totals"]
        ev = {k: v.get("events") for k, v in tot.items() if isinstance(v, dict)}
        ac = {k: v.get("actors") for k, v in tot.items() if isinstance(v, dict)}
        return ev, ac, True
    return {}, {}, False


def match(data: dict, day: date) -> tuple[dict[str, float], list[str]]:
    """Valores do dia para cada KPI do catálogo (0 quando o evento não veio) e avisos.

    Nome fora do catálogo é ignorado e vira aviso. Os KPIs de ``DERIVED`` entram junto.
    """
    events, actors, found = _day_counts(data, day)
    avisos = []
    if not found:
        avisos.append(f"o dia {day.isoformat()} não veio na série do /daily; gravei 0")
    names = set(events) | set(actors) | {k for k in data["totals"] if isinstance(k, str)}
    extra = sorted(n for n in names if n not in EVENTS)
    if extra:
        avisos.append("fora do catálogo, ignorado: " + ", ".join(extra))
    out: dict[str, float] = {}
    for name in EVENTS:
        v = _num(events.get(name))
        if v is None and events.get(name) is not None:
            avisos.append(f"{name}: valor não numérico, gravei 0")
        out[name] = v or 0.0
    for kpi, (name, kind) in DERIVED.items():
        out[kpi] = _num((actors if kind == "actors" else events).get(name)) or 0.0
    return out, avisos
