"""Central de conteúdo: tudo que os agentes de um projeto produzem para sair para fora.

Posts de redes sociais, e-mails de contato e de parceria, artigos... Cada peça:
  1. nasce de um bloco ```conteudo``` na resposta de um agente (ou o owner cria na tela);
  2. espera a aprovação do owner (sempre: nem rotina com auto-aprovação libera conteúdo);
  3. aprovada, pode ganhar data e hora (agendada) ou ficar pronta para sair;
  4. o owner publica/envia e marca como enviada. O envio automático entra com as integrações (etapa 4).
"""

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session as DB

from .config import get_settings
from .db import ContentItem, Task, utcnow

MAX_CONTENT_PER_TASK = 10

# chave: (nome, grupo, ícone)
CHANNELS: dict[str, tuple[str, str, str]] = {
    "instagram": ("Instagram", "social", "📸"),
    "tiktok": ("TikTok", "social", "🎵"),
    "linkedin": ("LinkedIn", "social", "💼"),
    "x": ("X / Twitter", "social", "𝕏"),
    "facebook": ("Facebook", "social", "📘"),
    "youtube": ("YouTube", "social", "▶"),
    "threads": ("Threads", "social", "@"),
    "email": ("E-mail", "email", "✉"),
    "whatsapp": ("WhatsApp", "mensagem", "💬"),
    "blog": ("Blog / artigo", "site", "📝"),
    "outro": ("Outro", "outro", "◆"),
}
_ALIASES = {"twitter": "x", "ig": "instagram", "insta": "instagram", "e-mail": "email", "mail": "email",
            "newsletter": "email", "artigo": "blog", "site": "blog", "fb": "facebook", "yt": "youtube",
            "zap": "whatsapp", "wpp": "whatsapp"}
GROUPS = {"social": "Redes sociais", "email": "E-mails", "mensagem": "Mensagens", "site": "Site", "outro": "Outros"}

STATUSES = ("pending", "approved", "sent", "rejected", "failed")
EDITABLE = ("pending", "approved", "failed")

_BLOCK = re.compile(r"```(conteudo|conteúdo|content)[ \t]*\n(.*?)```", re.S)


def local_now() -> datetime:
    """Agora no fuso do owner, sem tzinfo (é assim que scheduled_at é guardado)."""
    return datetime.now(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)


def norm_channel(raw: str) -> str:
    c = str(raw or "").strip().lower()
    c = _ALIASES.get(c, c)
    return c if c in CHANNELS else "outro"


def parse_when(raw) -> datetime | None:
    """Aceita '2026-11-03 10:00', '2026-11-03T10:00' ou só a data (vira 9h). Hora local do owner."""
    s = str(raw or "").strip().replace("T", " ")[:16]
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(hour=9) if fmt == "%Y-%m-%d" else d
        except ValueError:
            continue
    return None


def stage(c: ContentItem, now: datetime | None = None) -> str:
    """Status que a tela mostra. 'approved' se divide em agendado (data no futuro) e pronto para sair."""
    if c.status != "approved":
        return c.status
    now = now or local_now()
    if c.scheduled_at and c.scheduled_at > now:
        return "scheduled"
    return "ready"


def guide() -> str:
    """Instrução para o prompt do agente: como entregar conteúdo para a central."""
    channels = ", ".join(CHANNELS)
    return f"""CONTEÚDO PARA SAIR (posts, e-mails de contato/parceria, artigos)
Nunca publique nem envie nada por conta própria. Entregue cada peça pronta num bloco assim (um por peça,
ou uma lista), e ela vai para a Central de Conteúdo esperar a aprovação do owner:
```conteudo
{{"canal": "instagram", "titulo": "título ou assunto do e-mail", "texto": "o texto final, pronto para publicar",
 "para": "destinatário (e-mail) ou público", "objetivo": "parceria|contato|lançamento|...",
 "midia": "descrição da imagem/vídeo, se tiver", "quando": "AAAA-MM-DD HH:MM (sugestão, hora de Brasília)"}}
```
Canais: {channels}. Só "canal" e "texto" são obrigatórios."""


def _items(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    return [i for i in items if isinstance(i, dict)]


def ingest_blocks(db: DB, task: Task, text: str) -> list[ContentItem]:
    """Cria as peças dos blocos ```conteudo``` de uma tarefa, sempre no projeto da tarefa e aguardando aprovação.

    Não duplica: o mesmo canal + texto na mesma tarefa entra uma vez só (a conversa pode repetir o bloco).
    """
    seen = {
        (c.channel, c.body.strip())
        for c in db.query(ContentItem).filter(ContentItem.task_id == task.id).all()
    }
    created: list[ContentItem] = []
    for _, raw in _BLOCK.findall(text or ""):
        for it in _items(raw):
            body = str(it.get("texto") or it.get("text") or "").strip()[:20000]
            if not body:
                continue
            channel = norm_channel(it.get("canal") or it.get("channel"))
            if (channel, body) in seen or len(seen) >= MAX_CONTENT_PER_TASK:
                continue
            seen.add((channel, body))
            c = ContentItem(
                project_id=task.project_id, agent_slug=task.agent_slug, task_id=task.id, channel=channel,
                title=str(it.get("titulo") or it.get("assunto") or "").strip()[:200],
                body=body,
                recipient=str(it.get("para") or "").strip()[:300],
                purpose=str(it.get("objetivo") or "").strip()[:60],
                media=str(it.get("midia") or it.get("mídia") or "").strip()[:4000],
                scheduled_at=parse_when(it.get("quando")),
                status="pending",
            )
            db.add(c)
            created.append(c)
    if created:
        db.commit()
    return created


def item_json(c: ContentItem, project_slug: str = "", now: datetime | None = None) -> dict:
    name, group, icon = CHANNELS.get(c.channel, CHANNELS["outro"])
    return {
        "id": c.id, "project": project_slug, "agent": c.agent_slug, "task_id": c.task_id,
        "channel": c.channel, "channel_name": name, "channel_icon": icon, "group": group,
        "title": c.title, "body": c.body, "recipient": c.recipient, "purpose": c.purpose, "media": c.media,
        "status": c.status, "stage": stage(c, now), "link": c.link, "note": c.note,
        "scheduled_at": c.scheduled_at.isoformat(timespec="minutes") if c.scheduled_at else None,
        "sent_at": c.sent_at.isoformat() if c.sent_at else None,
        "created_at": c.created_at.isoformat(),
        "decided_at": c.decided_at.isoformat() if c.decided_at else None,
    }


def channels_json() -> list[dict]:
    return [{"key": k, "name": n, "group": g, "group_name": GROUPS[g], "icon": i} for k, (n, g, i) in CHANNELS.items()]


def touch(c: ContentItem) -> None:
    c.updated_at = utcnow()


def agenda_window(days: int) -> tuple[datetime, datetime]:
    now = local_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=days)
