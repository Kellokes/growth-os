"""KPIs lidos da API do próprio projeto e relatório semanal: tudo em código, sem LLM.

- A integração "API do próprio projeto" guarda dois campos públicos: o caminho da leitura diária
  (``kpi_path``, com ``{ontem}`` e ``{hoje}``) e quais campos do JSON viram quais KPIs (``kpi_fields``,
  ``kpi=caminho.no.json`` separados por vírgula).
- O relatório semanal compara os últimos 7 dias com os 7 anteriores a partir dos pontos já gravados.
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session as DB

from .db import Metric, MetricPoint


def parse_fields(raw: str) -> dict[str, str]:
    """'cadastros=kpis.logins, visitantes=kpis.web' -> {'cadastros': 'kpis.logins', 'visitantes': 'kpis.web'}."""
    out = {}
    for part in (raw or "").split(","):
        key, sep, path = part.partition("=")
        if sep and key.strip() and path.strip():
            out[key.strip()] = path.strip()
    return out


def dig(data, path: str):
    """Valor em 'a.b.c' de um JSON (dicionários; segmentos numéricos entram em listas). None se não existir."""
    cur = data
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return None
    return cur


def render_path(template: str, today: date) -> str:
    return template.replace("{ontem}", (today - timedelta(days=1)).isoformat()).replace("{hoje}", today.isoformat())


def _agg(unit: str, values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values) if unit == "percent" else sum(values)


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    text = f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    text = text.replace(",", " ")
    return f"{text}%" if unit == "percent" else text


def weekly_report(db: DB, project_id: int, today: date) -> str:
    """Tabela da semana (últimos 7 dias até ontem) contra a anterior, calculada dos pontos de KPI gravados."""
    end = today - timedelta(days=1)
    cur_from, prev_to, prev_from = end - timedelta(days=6), end - timedelta(days=7), end - timedelta(days=13)
    rows, missing = [], []
    for m in db.query(Metric).filter(Metric.project_id == project_id).order_by(Metric.id).all():
        pts = db.query(MetricPoint).filter(MetricPoint.metric_id == m.id, MetricPoint.day >= prev_from,
                                           MetricPoint.day <= end).all()
        cur = [p.value for p in pts if p.day >= cur_from]
        prev = [p.value for p in pts if p.day <= prev_to]
        if not cur and not prev:
            missing.append(m.name)
            continue
        a, b = _agg(m.unit, cur), _agg(m.unit, prev)
        change = "—" if a is None or not b else f"{(a - b) / abs(b) * 100:+.1f}%".replace(".", ",")
        rows.append(f"| {m.name} | {_fmt(a, m.unit)} | {_fmt(b, m.unit)} | {change} | {len(cur)}/7 | "
                    f"{_fmt(m.target, m.unit) if m.target is not None else '—'} |")
    head = (f"## Relatório semanal de KPIs\n\nSemana atual: {cur_from:%d/%m} a {end:%d/%m}. "
            f"Anterior: {prev_from:%d/%m} a {prev_to:%d/%m}. Calculado em código a partir dos pontos gravados, sem IA.\n\n")
    if not rows:
        return head + "Nenhum KPI tem dado nestas duas semanas. Confira a coleta diária em Automações > Integrações.\n"
    table = ("| KPI | Semana atual | Anterior | Variação | Dias com dado | Meta |\n|---|---|---|---|---|---|\n"
             + "\n".join(rows) + "\n")
    notes = "\nContagens somam os dias; KPIs em % usam a média. "
    notes += f"Sem dado: {', '.join(missing)}.\n" if missing else "\n"
    return head + table + notes
