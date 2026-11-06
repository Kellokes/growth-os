"""Conectores que agem fora do OS (avisar, publicar, enviar). Operações `external` só rodam depois da aprovação."""

import asyncio
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import httpx

from . import Connector, ConnectorError, Ctx, Op, TransientError, register, request_json, safe_base_url

_EMAIL = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")

_IG_PROVIDERS = {"instagram", "instagram-standalone"}


def _no_newlines(*values: str) -> None:
    if any("\r" in v or "\n" in v for v in values):
        raise ConnectorError("quebra de linha não é permitida neste campo")


class Telegram(Connector):
    kind = "telegram"
    ops = {"avisar_owner": Op("external", "manda uma mensagem curta para o Telegram do owner", {"texto": (str, True)},
                              risk="baixo", daily_cap=20, preapprovable=True)}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        chat = ctx.config.get("chat_id", "")
        if not (chat and ctx.secret):
            raise ConnectorError("falta o chat_id ou o token do bot")
        data = await request_json(ctx, "POST", f"https://api.telegram.org/bot{ctx.secret}/sendMessage",
                                  body={"chat_id": chat, "text": params["texto"][:3500], "disable_web_page_preview": True},
                                  headers={})
        return {"enviado": True, "ref": str((data.get("result") or {}).get("message_id", "")) if isinstance(data, dict) else ""}


class Postiz(Connector):
    kind = "postiz"
    ops = {
        "listar_canais": Op("read", "lista os canais (redes) conectados no Postiz, com o id de cada um"),
        "criar_rascunho": Op("internal", "cria um rascunho no Postiz (não publica)",
                             {"canal_id": (str, True), "texto": (str, True), "midia_ids": (str, False), "tipo": (str, False)}),
        "agendar_post": Op("external", "agenda um post no Postiz para uma data/hora (ISO, UTC)",
                           {"canal_id": (str, True), "texto": (str, True), "quando": (str, True), "midia_ids": (str, False), "tipo": (str, False)},
                           risk="medio", daily_cap=10),
        "publicar_post": Op("external", "publica agora no canal escolhido", {"canal_id": (str, True), "texto": (str, True), "midia_ids": (str, False), "tipo": (str, False)},
                            risk="medio", daily_cap=10),
    }

    def _headers(self, ctx: Ctx) -> dict:
        return {"Authorization": ctx.secret}  # o Postiz usa a chave direto, sem "Bearer"

    async def _channels(self, ctx: Ctx, base: str) -> list[dict]:
        data = await request_json(ctx, "GET", f"{base}/api/public/v1/integrations", headers=self._headers(ctx))
        return [c for c in (data if isinstance(data, list) else []) if isinstance(c, dict)]

    async def _upload(self, ctx: Ctx, base: str, ids: list[int]) -> list[dict]:
        """Sobe as imagens/vídeos da peça para o Postiz (na ordem dada) e devolve {id, path} de cada um."""
        from .. import db as dbmod
        s = dbmod.new_session()
        try:
            files = []
            for i in ids:
                a = s.get(dbmod.LayoutAsset, i)
                if a is None or a.project_id != ctx.project_id:
                    raise ConnectorError(f"arquivo #{i} não existe neste projeto")
                files.append((a.mime or "image/png", bytes(a.data)))
        finally:
            s.close()
        out = []
        for n, (mime, data) in enumerate(files, 1):
            ext = (mime.split("/")[-1] or "png").replace("jpeg", "jpg")
            try:
                async with ctx.client() as c:
                    r = await c.post(f"{base}/api/public/v1/upload", headers=self._headers(ctx),
                                     files={"file": (f"midia-{n}.{ext}", data, mime)})
            except httpx.HTTPError as e:
                raise TransientError(f"não consegui subir a mídia no Postiz: {e.__class__.__name__}") from e
            if r.status_code in (429,) or r.status_code >= 500:
                raise TransientError(f"o Postiz respondeu {r.status_code} ao subir a mídia")
            if r.status_code >= 400:
                raise ConnectorError(f"o Postiz recusou a mídia ({r.status_code}): {r.text[:200]}")
            try:
                j = r.json()
            except ValueError as e:
                raise ConnectorError("o Postiz não devolveu JSON ao subir a mídia") from e
            if not (isinstance(j, dict) and j.get("id") and j.get("path")):
                raise ConnectorError("o Postiz não devolveu id/path da mídia enviada")
            out.append({"id": j["id"], "path": j["path"]})
        return out

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        base = safe_base_url(ctx.config.get("url", ""))
        channels = await self._channels(ctx, base)
        if op == "listar_canais":
            return {"canais": [{"id": c.get("id"), "nome": c.get("name"), "rede": c.get("identifier")} for c in channels]}
        ch = next((c for c in channels if c.get("id") == params["canal_id"]), None)
        if ch is None:
            raise ConnectorError("canal não encontrado no Postiz (use listar_canais para ver os ids)")
        kind = {"criar_rascunho": "draft", "agendar_post": "schedule", "publicar_post": "now"}[op]
        when = datetime.now(timezone.utc)
        if op == "agendar_post":
            try:
                when = datetime.fromisoformat(params["quando"].replace("Z", "+00:00"))
            except ValueError as e:
                raise ConnectorError("'quando' precisa ser data e hora ISO, ex.: 2026-11-05T13:00:00Z") from e
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= datetime.now(timezone.utc):
                raise ConnectorError("a data de agendamento já passou")
        settings = {"__type": ch.get("identifier", "")}
        if settings["__type"] in _IG_PROVIDERS:
            settings["post_type"] = "story" if params.get("tipo") == "story" else "post"  # Postiz: post|story; reel e carrossel são "post" (vídeo/várias imagens)
        ids = [int(x) for x in str(params.get("midia_ids", "")).replace(" ", "").split(",") if x.isdigit()]
        if settings["__type"] in _IG_PROVIDERS and not ids:
            raise ConnectorError("o Instagram exige ao menos uma imagem ou vídeo na peça")
        media = await self._upload(ctx, base, ids) if ids else []
        body = {"type": kind, "date": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "shortLink": False,
                "tags": [], "posts": [{"integration": {"id": ch["id"]}, "value": [{"content": params["texto"], "image": media}],
                                       "settings": settings}]}
        data = await request_json(ctx, "POST", f"{base}/api/public/v1/posts", body=body, headers=self._headers(ctx))
        ref = ""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            ref = str(data[0].get("postId") or data[0].get("id") or "")
        elif isinstance(data, dict):
            ref = str(data.get("postId") or data.get("id") or "")
        return {"tipo": kind, "canal": ch.get("name"), "ref": ref}


class Email(Connector):
    kind = "email"
    ops = {"enviar_email": Op("external", "envia UM e-mail para UMA pessoa pelo SMTP do projeto",
                              {"para": (str, True), "assunto": (str, True), "texto": (str, True)}, risk="alto", daily_cap=5)}

    async def run(self, ctx: Ctx, op: str, params: dict) -> dict:
        host, sender = ctx.config.get("host", ""), ctx.config.get("from", "")
        port = int(ctx.config.get("port") or 587)
        if not (host and sender and ctx.secret):
            raise ConnectorError("falta host, remetente ou a senha do SMTP")
        to, subject = params["para"].strip(), params["assunto"].strip()
        _no_newlines(to, subject, sender)
        if not _EMAIL.match(to):
            raise ConnectorError("endereço de e-mail inválido (envie para uma pessoa por vez)")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, to, subject[:200]
        msg["Date"], msg["Message-ID"] = formatdate(localtime=False), make_msgid()
        msg.set_content(params["texto"])
        user = ctx.config.get("user") or sender
        await asyncio.to_thread(self._send, host, port, user, ctx.secret, msg)
        return {"enviado": True, "para": to, "ref": msg["Message-ID"]}

    @staticmethod
    def _send(host: str, port: int, user: str, password: str, msg: EmailMessage) -> None:
        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as s:
                    s.login(user, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=20) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(user, password)
                    s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise ConnectorError("o SMTP recusou o usuário ou a senha") from e
        except smtplib.SMTPRecipientsRefused as e:
            raise ConnectorError("o SMTP recusou o destinatário") from e
        except (smtplib.SMTPException, OSError) as e:
            raise TransientError(f"falha no SMTP: {e.__class__.__name__}") from e


for _c in (Telegram(), Postiz(), Email()):
    register(_c)
