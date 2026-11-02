"""Conectores de leitura (só buscam números; nenhuma escrita)."""

import re
import time
from urllib.parse import quote, urlparse

import httpx

from ..config import get_settings
from . import Connector, ConnectorError, Ctx, Op, TransientError, get_json, register, safe_base_url

_GITHUB_BLOB = re.compile(r"^/([^/]+)/([^/]+)/blob/(.+)$")
DOCS_MAX_CHARS = 20000


def _github_token() -> str:
    s = get_settings()
    if s.github_token:
        return s.github_token.strip()
    try:
        return s.github_token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
    ops = {"get": Op("read", "lê um endpoint (GET) da API do próprio projeto", {"caminho": (str, True)}),
           "docs": Op("read", "lê a documentação cadastrada na integração: os endpoints que existem. Rode antes do get", {})}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        if op == "docs":
            return await self.docs(ctx)
        base = safe_base_url(ctx.config.get("base_url", ""))
        path = params["caminho"].strip()
        if not path.startswith("/") or ".." in path or "//" in path or "@" in path or "\\" in path:
            raise ConnectorError("o caminho precisa começar com / e ser relativo à URL base")
        data = await get_json(ctx, base + path)
        return {"caminho": path, "dados": data}

    async def docs(self, ctx: Ctx) -> dict:
        """Lê a documentação cadastrada na integração (campo "docs"): caminho da própria API ou link do GitHub."""
        base = safe_base_url(ctx.config.get("base_url", ""))
        raw = str(ctx.config.get("docs", "")).strip()
        if not raw:
            raise ConnectorError("a integração não tem a documentação cadastrada (campo 'Onde está a documentação')")
        headers: dict = {}
        github = False
        if raw.startswith("/"):
            if ".." in raw or "//" in raw or "@" in raw or "\\" in raw:
                raise ConnectorError("o caminho da documentação precisa ser relativo à URL base")
            url, headers = base + raw, ctx.auth()
        else:
            u = urlparse(raw)
            if u.scheme != "https" or not u.hostname:
                raise ConnectorError("o link da documentação precisa começar com https://")
            host = u.hostname.lower()
            if host == "github.com":
                m = _GITHUB_BLOB.match(u.path)
                if not m:
                    raise ConnectorError("use o link do arquivo no GitHub (…/blob/ramo/arquivo.md)")
                url, github = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}", True
            elif host == "raw.githubusercontent.com":
                url, github = f"https://{host}{u.path}", True
            elif host == urlparse(base).hostname:
                url, headers = f"https://{host}{u.path}" + (f"?{u.query}" if u.query else ""), ctx.auth()
            else:
                raise ConnectorError("a documentação só pode estar no GitHub ou no domínio da URL base")
            if github:
                token = _github_token()  # só vai para o GitHub, nunca a chave da API do projeto
                headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with ctx.client() as c:
                r = await c.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise TransientError(f"não consegui ler a documentação: {e.__class__.__name__}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"a documentação respondeu {r.status_code} (o token do GitHub alcança esse repositório?)")
        return {"documentacao": r.text[:DOCS_MAX_CHARS], "truncada": len(r.text) > DOCS_MAX_CHARS}


for _c in (Umami(), Plausible(), UptimeKuma(), ProjectApi()):
    register(_c)
