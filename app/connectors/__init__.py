"""Conectores: quem de fato chama as APIs das integrações.

Só o processo do app lê as chaves (no .env do VPS). A IA vê o esquema das operações e o resultado resumido,
nunca a chave. Cada operação declara o seu efeito:

  read      lê dados; roda na hora
  internal  grava só dentro do OS ou da própria ferramenta (rascunho); roda na hora
  external  muda algo fora do OS (publicar, enviar); só roda depois da aprovação do owner
"""

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

EFFECTS = ("read", "internal", "external")


class ConnectorError(Exception):
    """Falha esperada de uma operação (config faltando, API fora do ar, parâmetro inválido)."""


class TransientError(ConnectorError):
    """Falha que pode passar sozinha (rede, 429, 5xx): só esta é repetida automaticamente."""


@dataclass
class Op:
    effect: str
    about: str
    params: dict[str, tuple[type, bool]] = field(default_factory=dict)  # nome -> (tipo, obrigatório)
    risk: str = "baixo"  # só vale para external: baixo | medio | alto (alto exige 2FA)
    daily_cap: int = 10  # máximo de pedidos externos por dia, por projeto e integração
    preapprovable: bool = False  # o owner pode liberar esta operação sem aprovação por item (config "preaprovar")

    def clean(self, raw: dict) -> dict:
        raw = raw or {}
        extra = sorted(set(raw) - set(self.params))
        if extra:
            raise ConnectorError(f"parâmetros desconhecidos: {', '.join(extra)}")
        out = {}
        for name, (typ, required) in self.params.items():
            if raw.get(name) in (None, ""):
                if required:
                    raise ConnectorError(f"falta o parâmetro '{name}'")
                continue
            try:
                out[name] = typ(raw[name])
            except (TypeError, ValueError) as e:
                raise ConnectorError(f"'{name}' precisa ser {typ.__name__}") from e
        return out

    def signature(self) -> str:
        return ", ".join(f"{n}{'' if req else '?'}: {t.__name__}" for n, (t, req) in self.params.items())


@dataclass
class Ctx:
    config: dict
    secret: str = ""
    transport: httpx.AsyncBaseTransport | None = None  # os testes trocam por um transporte falso

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=self.transport, follow_redirects=False)

    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}


class Connector:
    kind = ""
    ops: dict[str, Op] = {}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


_BLOCKED_HOSTS = {"localhost", "docker-gateway", "postgres", "metadata.google.internal"}


def safe_base_url(raw: str, resolve: bool = True) -> str:
    """URL configurada pelo owner. Bloqueia o que levaria o OS a falar com ele mesmo ou com a rede de metadados."""
    u = urlparse((raw or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ConnectorError("a URL precisa começar com http:// ou https://")
    if u.username or u.password:
        raise ConnectorError("não coloque usuário e senha na URL")
    host = u.hostname.lower()
    if host in _BLOCKED_HOSTS:
        raise ConnectorError(f"host não permitido: {host}")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        addrs = []
        if resolve:
            try:
                addrs = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)]
            except OSError:
                pass  # sem DNS aqui: a chamada falha sozinha com um erro claro
    for a in addrs:
        if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
            raise ConnectorError("host aponta para o próprio servidor ou para a rede de metadados")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


async def request_json(ctx: Ctx, method: str, url: str, params: dict | None = None, body: dict | None = None,
                       headers: dict | None = None) -> dict | list:
    try:
        async with ctx.client() as c:
            r = await c.request(method, url, params=params, json=body, headers=headers if headers is not None else ctx.auth())
    except httpx.HTTPError as e:
        raise TransientError(f"não consegui falar com {urlparse(url).netloc}: {e.__class__.__name__}") from e
    if r.status_code in (401, 403):
        raise ConnectorError("a ferramenta recusou a chave (confira a variável no .env do VPS)")
    if r.status_code == 429 or r.status_code >= 500:
        raise TransientError(f"a ferramenta respondeu {r.status_code}")
    if r.status_code >= 400:
        raise ConnectorError(f"a ferramenta respondeu {r.status_code}: {r.text[:200]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError as e:
        raise ConnectorError("a resposta não veio em JSON") from e


async def get_json(ctx: Ctx, url: str, params: dict | None = None) -> dict | list:
    return await request_json(ctx, "GET", url, params=params)


REGISTRY: dict[str, Connector] = {}


def register(conn: Connector) -> Connector:
    REGISTRY[conn.kind] = conn
    return conn


from . import readers  # noqa: E402,F401 - registra os conectores de leitura
from . import senders  # noqa: E402,F401 - registra os conectores que agem fora do OS
