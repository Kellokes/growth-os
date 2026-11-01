"""Gateway mínimo para o Docker do VPS.

É o ÚNICO processo com o socket do Docker. O Growth OS (e os comandos que a IA roda) só falam
com este gateway pela rede interna do compose, e ele só oferece:

    GET  /health
    GET  /containers                    lista sem variáveis de ambiente, mounts ou labels
    GET  /containers/{nome}/logs?tail=N últimas linhas de log (máx. 2000)
    POST /containers/{nome}/restart     só com o token que o app guarda (X-Gateway-Token)

Não existe exec, create, rm, inspect completo, cópia de arquivos nem acesso a imagens/volumes.
Só usa a biblioteca padrão do Python para ter o mínimo de código e dependências.
"""

import hmac
import http.client
import json
import os
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
TOKEN = os.environ.get("GATEWAY_TOKEN", "")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_TAIL = 2000
MAX_LOG_BYTES = 400_000


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def docker(method: str, path: str, timeout: float = 30) -> tuple[int, bytes]:
    conn = UnixHTTPConnection(SOCKET, timeout=timeout)
    try:
        conn.request(method, path, headers={"Host": "docker"})
        resp = conn.getresponse()
        return resp.status, resp.read(MAX_LOG_BYTES * 2)
    finally:
        conn.close()


def _health(status: str) -> str:
    m = re.search(r"\((healthy|unhealthy|health: starting)\)", status or "")
    return {"health: starting": "starting"}.get(m.group(1), m.group(1)) if m else ""


def sanitize(raw: dict) -> dict:
    """Só o que a tela de projetos e a IA precisam; nada de Env, Mounts ou Labels completos."""
    labels = raw.get("Labels") or {}
    names = [n.lstrip("/") for n in raw.get("Names") or []]
    ports = sorted({
        f"{p.get('IP', '')}:{p['PublicPort']}->{p['PrivatePort']}/{p.get('Type', 'tcp')}" if p.get("PublicPort")
        else f"{p['PrivatePort']}/{p.get('Type', 'tcp')}"
        for p in raw.get("Ports") or [] if p.get("PrivatePort")
    })
    return {
        "id": (raw.get("Id") or "")[:12],
        "name": names[0] if names else "",
        "image": raw.get("Image", ""),
        "state": raw.get("State", ""),
        "status": raw.get("Status", ""),
        "health": _health(raw.get("Status", "")),
        "created": raw.get("Created", 0),
        "project": labels.get("com.docker.compose.project", ""),
        "service": labels.get("com.docker.compose.service", ""),
        "networks": sorted((raw.get("NetworkSettings") or {}).get("Networks", {}).keys()),
        "ports": ports,
    }


def demux(data: bytes) -> str:
    """Logs de containers sem TTY vêm em quadros de 8 bytes de cabeçalho + conteúdo."""
    out = bytearray()
    i = 0
    while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1:i + 4] == b"\x00\x00\x00":
        size = int.from_bytes(data[i + 4:i + 8], "big")
        out += data[i + 8:i + 8 + size]
        i += 8 + size
    if i == 0:  # container com TTY: o log já é texto puro
        out = bytearray(data)
    return bytes(out[-MAX_LOG_BYTES:]).decode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    server_version = "gos-docker-gateway"
    sys_version = ""

    def log_message(self, fmt, *args):  # um log por linha, sem query string
        sys.stderr.write("%s %s\n" % (self.command, urlparse(self.path).path))

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        )
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _name(self, raw: str) -> str | None:
        return raw if NAME_RE.match(raw) else None

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if parts == ["health"]:
                return self._send(200, {"ok": True})
            if parts == ["containers"]:
                code, body = docker("GET", "/containers/json?all=1")
                if code != 200:
                    return self._send(502, {"error": "docker", "status": code})
                return self._send(200, [sanitize(c) for c in json.loads(body)])
            if len(parts) == 3 and parts[0] == "containers" and parts[2] == "logs":
                name = self._name(parts[1])
                if not name:
                    return self._send(400, {"error": "nome inválido"})
                try:
                    tail = max(1, min(MAX_TAIL, int(parse_qs(url.query).get("tail", ["200"])[0])))
                except ValueError:
                    tail = 200
                code, body = docker(
                    "GET", f"/containers/{quote(name)}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}"
                )
                if code == 404:
                    return self._send(404, {"error": "container não encontrado"})
                if code != 200:
                    return self._send(502, {"error": "docker", "status": code})
                return self._send(200, demux(body), "text/plain")
        except (OSError, http.client.HTTPException) as e:
            return self._send(502, {"error": f"docker indisponível: {type(e).__name__}"})
        return self._send(404, {"error": "não existe"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if not (len(parts) == 3 and parts[0] == "containers" and parts[2] == "restart"):
            return self._send(404, {"error": "não existe"})
        given = self.headers.get("X-Gateway-Token", "")
        if not TOKEN or not hmac.compare_digest(given.encode(), TOKEN.encode()):
            return self._send(401, {"error": "token inválido"})
        name = self._name(parts[1])
        if not name:
            return self._send(400, {"error": "nome inválido"})
        try:
            code, _ = docker("POST", f"/containers/{quote(name)}/restart?t=10", timeout=60)
        except (OSError, http.client.HTTPException) as e:
            return self._send(502, {"error": f"docker indisponível: {type(e).__name__}"})
        if code == 204:
            return self._send(200, {"ok": True})
        if code == 404:
            return self._send(404, {"error": "container não encontrado"})
        return self._send(502, {"error": "docker", "status": code})

    do_PUT = do_DELETE = do_PATCH = lambda self: self._send(405, {"error": "método não permitido"})


def main() -> None:
    if not TOKEN:
        sys.stderr.write("aviso: GATEWAY_TOKEN vazio, restart fica desligado\n")
    port = int(os.environ.get("PORT", "2375"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
