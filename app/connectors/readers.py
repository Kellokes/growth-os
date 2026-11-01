"""Conectores de leitura (só buscam números; nenhuma escrita)."""

import time
from urllib.parse import quote

from . import Connector, ConnectorError, Ctx, Op, get_json, register, safe_base_url


def _days(params: dict) -> int:
    return max(1, min(int(params.get("dias") or 7), 90))


class Umami(Connector):
    kind = "umami"
    ops = {"ler_visitas": Op("read", "visitantes, visitas, páginas vistas e rejeição dos últimos N dias", {"dias": (int, False)})}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        base, site = safe_base_url(ctx.config.get("url", "")), ctx.config.get("website_id", "")
        if not site:
            raise ConnectorError("falta o ID do site na configuração")
        days = _days(params)
        end = int(time.time() * 1000)
        data = await get_json(ctx, f"{base}/api/websites/{quote(site)}/stats",
                              {"startAt": end - days * 86_400_000, "endAt": end})
        g = lambda k: (data.get(k, {}).get("value") if isinstance(data.get(k), dict) else data.get(k)) or 0  # noqa: E731
        return {"dias": days, "visitantes": g("visitors"), "visitas": g("visits"),
                "paginas_vistas": g("pageviews"), "rejeicoes": g("bounces")}


class Plausible(Connector):
    kind = "plausible"
    ops = {"ler_visitas": Op("read", "visitantes, páginas vistas e rejeição dos últimos N dias", {"dias": (int, False)})}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        base, site = safe_base_url(ctx.config.get("url", "")), ctx.config.get("site_id", "")
        if not site:
            raise ConnectorError("falta o domínio do site na configuração")
        days = _days(params)
        data = await get_json(ctx, f"{base}/api/v1/stats/aggregate",
                              {"site_id": site, "period": f"{days}d", "metrics": "visitors,pageviews,bounce_rate,visit_duration"})
        r = data.get("results", {}) if isinstance(data, dict) else {}
        v = lambda k: (r.get(k) or {}).get("value", 0)  # noqa: E731
        return {"dias": days, "visitantes": v("visitors"), "paginas_vistas": v("pageviews"),
                "taxa_rejeicao": v("bounce_rate"), "duracao_visita_s": v("visit_duration")}


class UptimeKuma(Connector):
    kind = "uptime_kuma"
    ops = {"ler_status": Op("read", "estado atual e disponibilidade recente de cada monitor da página de status")}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        base, slug = safe_base_url(ctx.config.get("url", "")), ctx.config.get("status_page", "")
        if not slug:
            raise ConnectorError("falta o slug da página de status")
        data = await get_json(ctx, f"{base}/api/status-page/heartbeat/{quote(slug)}")
        beats = data.get("heartbeatList", {}) if isinstance(data, dict) else {}
        uptimes = data.get("uptimeList", {}) if isinstance(data, dict) else {}
        monitors = []
        for mid, rows in beats.items():
            last = rows[-1] if rows else {}
            monitors.append({"monitor": mid, "no_ar": last.get("status") == 1,
                             "uptime_24h": uptimes.get(f"{mid}_24"), "ultima_checagem": last.get("time")})
        down = [m["monitor"] for m in monitors if not m["no_ar"]]
        vals = [m["uptime_24h"] for m in monitors if isinstance(m["uptime_24h"], (int, float))]
        return {"monitores": monitors, "fora_do_ar": down,
                "uptime_24h_medio": round(sum(vals) / len(vals) * 100, 2) if vals else None}


class ProjectApi(Connector):
    kind = "project_api"
    ops = {"get": Op("read", "lê um endpoint (GET) da API do próprio projeto", {"caminho": (str, True)})}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        base = safe_base_url(ctx.config.get("base_url", ""))
        path = params["caminho"].strip()
        if not path.startswith("/") or ".." in path or "//" in path or "@" in path or "\\" in path:
            raise ConnectorError("o caminho precisa começar com / e ser relativo à URL base")
        data = await get_json(ctx, base + path)
        return {"caminho": path, "dados": data}


for _c in (Umami(), Plausible(), UptimeKuma(), ProjectApi()):
    register(_c)
