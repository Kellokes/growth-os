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
# Formato visual padrão por canal quando o agente não diz (vazio = o canal não exige imagem).
DEFAULT_VISUAL = {"instagram": "carrossel", "facebook": "carrossel", "tiktok": "reel", "youtube": "reel"}
VISUAL_FORMATS = ("story", "carrossel", "reel")
IMAGE_MODES = ("template", "reference", "prompt")  # template = padrão de sempre
_MODE_ALIASES = {"referencia": "reference", "referência": "reference", "ref": "reference", "livre": "prompt", "sem template": "prompt"}


def norm_mode(raw) -> str:
    m = str(raw or "").strip().lower()
    m = _MODE_ALIASES.get(m, m)
    return m if m in IMAGE_MODES else "template"
_FORMAT_ALIASES = {"stories": "story", "carousel": "carrossel", "carrosel": "carrossel", "reels": "reel", "video": "reel"}
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


def norm_visual(raw, channel: str) -> str:
    """Formato visual da peça: o que o agente pediu ("formato") ou o padrão do canal."""
    f = str(raw or "").strip().lower()
    f = _FORMAT_ALIASES.get(f, f)
    if f in VISUAL_FORMATS:
        return f
    if f in ("nenhum", "texto", "sem imagem", "none"):
        return ""
    return DEFAULT_VISUAL.get(channel, "")


def needs_image(c: ContentItem) -> bool:
    """Peça visual (story, carrossel, reel) ainda sem imagem: não pode ser aprovada nem agendada assim."""
    return bool(c.visual_format) and not c.image_id


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
 "midia": "descrição da imagem/vídeo (o que deve aparecer nela)", "formato": "story|carrossel|reel (só para posts visuais)",
 "modo_imagem": "template (padrão) | reference (só as referências do projeto) | prompt (só o prompt abaixo)",
 "prompt_imagem": "prompt completo da imagem, em inglês, só se o modo for prompt ou reference",
 "titulo_imagem": "título curto (até 6 palavras) escrito NA imagem", "texto_imagem": "frase de apoio curta escrita NA imagem", "quando": "AAAA-MM-DD HH:MM (sugestão, hora de Brasília)"}}
```
Canais: {channels}. Só "canal" e "texto" são obrigatórios.
Posts visuais ganham a imagem pronta do sistema antes de você aprovar: o fundo é gerado a partir de "midia" (descreva só a
cena, sem texto) e o texto da imagem ("titulo_imagem", "texto_imagem") entra no template fixo do projeto. Se a peça pedir uma imagem livre (sem o template), use "modo_imagem": "prompt" e
descreva a imagem inteira em "prompt_imagem"; o owner pode trocar o modo e editar o prompt antes de aprovar. Não gere nem peça arquivos."""


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
                img_title=str(it.get("titulo_imagem") or "").strip()[:200],
                img_text=str(it.get("texto_imagem") or "").strip()[:400],
                image_mode=norm_mode(it.get("modo_imagem")),
                image_prompt=str(it.get("prompt_imagem") or "").strip()[:4000],
                visual_format=norm_visual(it.get("formato") or it.get("format"), channel),
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
        "img_title": c.img_title, "img_text": c.img_text,
        "image_mode": c.image_mode or "template", "template_id": c.template_id, "ref_id": c.ref_id,
        "image_prompt": c.image_prompt,
        "visual_format": c.visual_format, "needs_image": needs_image(c), "image_error": c.image_error,
        "image_url": f"/api/layouts/assets/{c.image_id}" if c.image_id else "",
        "video_url": f"/api/layouts/assets/{c.video_id}" if c.video_id else "",
        "can_video": bool(c.bg_id and c.visual_format),
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
