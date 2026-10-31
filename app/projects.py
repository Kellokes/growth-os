"""Etapa 2: visão dos projetos do VPS (containers pelo gateway do Docker e domínios do Caddy)."""

import asyncio
import re
import shlex
from pathlib import Path

import httpx

from .config import get_settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class GatewayError(RuntimeError):
    pass


def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(base_url=s.gateway_url, timeout=30, transport=transport or _transport)


# Os testes trocam isto por um httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


async def list_containers() -> list[dict]:
    try:
        async with _client() as c:
            r = await c.get("/containers")
    except httpx.HTTPError as e:
        raise GatewayError(f"gateway do Docker indisponível ({type(e).__name__})") from e
    if r.status_code != 200:
        raise GatewayError(f"gateway respondeu {r.status_code}")
    return r.json()


async def container_logs(name: str, tail: int = 200) -> str:
    if not NAME_RE.match(name):
        raise GatewayError("nome de container inválido")
    try:
        async with _client() as c:
            r = await c.get(f"/containers/{name}/logs", params={"tail": max(1, min(tail, 2000))})
    except httpx.HTTPError as e:
        raise GatewayError(f"gateway do Docker indisponível ({type(e).__name__})") from e
    if r.status_code == 404:
        raise GatewayError("container não encontrado")
    if r.status_code != 200:
        raise GatewayError(f"gateway respondeu {r.status_code}")
    return r.text


async def restart_container(name: str) -> None:
    """Só deve ser chamado depois do 2FA do owner."""
    if not NAME_RE.match(name):
        raise GatewayError("nome de container inválido")
    s = get_settings()
    if not s.gateway_token:
        raise GatewayError("GOS_GATEWAY_TOKEN não configurado")
    try:
        async with _client() as c:
            r = await c.post(f"/containers/{name}/restart", headers={"X-Gateway-Token": s.gateway_token}, timeout=90)
    except httpx.HTTPError as e:
        raise GatewayError(f"gateway do Docker indisponível ({type(e).__name__})") from e
    if r.status_code != 200:
        detail = r.json().get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        raise GatewayError(f"restart falhou ({r.status_code}) {detail}".strip())


def restart_target(command: str) -> str | None:
    """`gos-docker restart NOME` exato: o app executa pelo gateway (o shell não tem o token)."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return None
    if len(parts) == 3 and parts[0] == "gos-docker" and parts[1] == "restart" and NAME_RE.match(parts[2]):
        return parts[2]
    return None


# ---------- Caddy ----------

_VAR = re.compile(r"\{\$([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def parse_caddy_vars(raw: str) -> dict[str, str]:
    out = {}
    for item in re.split(r"[,\s]+", raw.strip()):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _subst(text: str, env: dict[str, str]) -> str:
    return _VAR.sub(lambda m: env.get(m.group(1), m.group(2) if m.group(2) is not None else m.group(0)), text)


def parse_caddyfile(text: str, env: dict[str, str] | None = None) -> list[dict]:
    """Blocos de site do nível superior: endereços e o upstream do reverse_proxy (se houver)."""
    env = env or {}
    sites: list[dict] = []
    depth = 0
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip() if not raw_line.strip().startswith("#") else ""
        if not line:
            continue
        opens, closes = line.count("{") - line.count("{$"), line.count("}") - line.count("{$")
        if depth == 0 and line.endswith("{") and line != "{" and not line.startswith("("):
            addrs = [a.strip() for a in re.split(r"[,\s]+", _subst(line[:-1], env)) if a.strip()]
            hosts = []
            for a in addrs:
                a = re.sub(r"^https?://", "", a).split("/")[0]
                if a and not a.startswith(":"):
                    hosts.append(a.split(":")[0] if a.count(":") == 1 else a)
            current = {"hosts": hosts, "upstream": "", "redirect": ""}
            sites.append(current)
        elif current is not None and depth == 1:
            m = re.match(r"reverse_proxy\s+(\S+)", line)
            if m and not current["upstream"]:
                current["upstream"] = _subst(m.group(1), env)
            r = re.match(r"redir\s+(\S+)", line)
            if r and not current["redirect"]:
                current["redirect"] = _subst(r.group(1), env)
        depth += opens - closes
        if depth <= 0:
            depth = 0
            current = None
    return [s for s in sites if s["hosts"]]


def read_caddy_sites() -> tuple[list[dict], str]:
    s = get_settings()
    path = Path(s.caddyfile_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], f"Caddyfile não encontrado em {path}"
    return parse_caddyfile(text, parse_caddy_vars(s.caddy_vars)), ""


async def check_hosts(hosts: list[str], transport: httpx.AsyncBaseTransport | None = None) -> dict[str, dict]:
    """HEAD em https://host sem seguir redirect. 403 em storage sem login é normal."""
    async def one(c: httpx.AsyncClient, host: str):
        try:
            r = await c.head(f"https://{host}/")
            return host, {"code": r.status_code, "ok": r.status_code < 500}
        except httpx.HTTPError as e:
            return host, {"code": 0, "ok": False, "error": type(e).__name__}

    async with httpx.AsyncClient(timeout=8, follow_redirects=False, transport=transport or _transport) as c:
        results = await asyncio.gather(*(one(c, h) for h in hosts))
    return dict(results)


def group_projects(containers: list[dict], sites: list[dict]) -> list[dict]:
    by_name = {c["name"]: c for c in containers}
    # O Caddy também alcança pelo nome do serviço do compose (ex.: "web"); vale quando é único.
    services: dict[str, list[dict]] = {}
    for c in containers:
        if c.get("service"):
            services.setdefault(c["service"], []).append(c)
    for svc, items in services.items():
        if len(items) == 1 and svc not in by_name:
            by_name[svc] = items[0]
    projects: dict[str, dict] = {}
    for c in containers:
        key = c.get("project") or "avulsos"
        projects.setdefault(key, {"name": key, "containers": [], "sites": []})["containers"].append(c)
    for site in sites:
        up = site["upstream"].split(":")[0]
        target = by_name.get(up)
        key = (target.get("project") or "avulsos") if target else "_caddy"
        projects.setdefault(key, {"name": key, "containers": [], "sites": []})["sites"].append(
            {**site, "container": target["name"] if target else ""}
        )
    out = list(projects.values())
    for p in out:
        p["containers"].sort(key=lambda c: c["name"])
        states = [c["state"] for c in p["containers"]]
        unhealthy = any(c.get("health") == "unhealthy" for c in p["containers"])
        p["status"] = (
            "down" if states and all(st != "running" for st in states)
            else "warn" if unhealthy or any(st != "running" for st in states)
            else "ok"
        )
    out.sort(key=lambda p: (p["name"] in ("avulsos", "_caddy"), p["name"]))
    return out
