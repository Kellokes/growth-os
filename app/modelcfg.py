"""Configuração de modelos: qual provider, modelo e nível de raciocínio cada atividade usa.

Ordem de decisão para uma atividade (ex.: "threads"):
  1. a escolha explícita (a thread ou o agente escolheu na hora)
  2. a regra salva para a atividade em /settings
  3. a regra "Padrão" (vale para tudo que não tem regra própria)
  4. o que já existia antes: GOS_AGENT_PROVIDER / primeiro provider disponível, modelo padrão da conta/CLI
Modelo vazio = o padrão do provider (o da conta no Claude/Codex, GOS_OPENROUTER_MODEL no OpenRouter).
Raciocínio vazio = o padrão do modelo.
"""

import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session as DB

from .db import ModelSetting, utcnow

# Ids de modelo vão como argumento separado para a CLI (nunca por shell), mas validamos mesmo assim.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]]{0,119}$")

TEXT, IMAGE, VIDEO, AUDIO = "text", "image", "video", "audio"

ACTIVITIES: list[dict] = [
    {"key": "default", "label": "Padrão", "kind": TEXT,
     "help": "Vale para tudo que não tiver uma regra própria abaixo."},
    {"key": "threads", "label": "Threads", "kind": TEXT,
     "help": "Ponto de partida de cada thread nova. Dá para trocar na própria thread."},
    {"key": "agents_plan", "label": "Agentes · plano do dia", "kind": TEXT,
     "help": "Quando os agentes propõem as tarefas da manhã."},
    {"key": "agents_run", "label": "Agentes · execução", "kind": TEXT,
     "help": "Quando um agente executa uma tarefa aprovada."},
    {"key": "image", "label": "Imagens", "kind": IMAGE,
     "help": "Geração de imagens (posts, criativos). Precisa de um modelo que gere imagem."},
    {"key": "video", "label": "Vídeos", "kind": VIDEO,
     "help": "Geração de vídeo. Precisa de um modelo que gere vídeo."},
    {"key": "audio", "label": "Áudio", "kind": AUDIO,
     "help": "Voz, narração e transcrição."},
]
ACTIVITY_KEYS = {a["key"] for a in ACTIVITIES}

# O que cada provider sabe fazer e como ele chama o "nível de raciocínio".
# Os modelos são sugestões: o campo aceita qualquer id que a sua conta tenha.
CATALOG: dict[str, dict] = {
    "claude": {
        "kinds": [TEXT],
        "reasoning": [
            {"id": "low", "label": "Baixo"}, {"id": "medium", "label": "Médio"}, {"id": "high", "label": "Alto"},
            {"id": "xhigh", "label": "Muito alto"}, {"id": "max", "label": "Máximo"},
        ],
        "models": [
            {"id": "opus", "label": "Opus (mais recente)"},
            {"id": "sonnet", "label": "Sonnet (mais recente)"},
            {"id": "haiku", "label": "Haiku (mais recente)"},
            {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
            {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
            {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
            {"id": "claude-fable-5-1", "label": "Claude Fable 5.1"},
        ],
    },
    "codex": {
        "kinds": [TEXT],
        "reasoning": [
            {"id": "minimal", "label": "Mínimo"}, {"id": "low", "label": "Baixo"}, {"id": "medium", "label": "Médio"},
            {"id": "high", "label": "Alto"}, {"id": "xhigh", "label": "Muito alto"},
        ],
        "models": [
            {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
            {"id": "gpt-5.1-codex", "label": "GPT-5.1 Codex"},
            {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max"},
            {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini"},
        ],
    },
    "openrouter": {
        "kinds": [TEXT, IMAGE, VIDEO, AUDIO],
        "reasoning": [
            {"id": "none", "label": "Desligado"}, {"id": "minimal", "label": "Mínimo"}, {"id": "low", "label": "Baixo"},
            {"id": "medium", "label": "Médio"}, {"id": "high", "label": "Alto"}, {"id": "xhigh", "label": "Muito alto"},
        ],
        "models": [
            {"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5", "kinds": [TEXT]},
            {"id": "openai/gpt-5", "label": "GPT-5", "kinds": [TEXT]},
            {"id": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "kinds": [TEXT]},
            {"id": "deepseek/deepseek-chat-v3.1", "label": "DeepSeek V3.1", "kinds": [TEXT]},
            {"id": "google/gemini-2.5-flash-image", "label": "Gemini 2.5 Flash Image", "kinds": [IMAGE]},
            {"id": "openai/gpt-5-image", "label": "GPT-5 Image", "kinds": [IMAGE]},
            {"id": "openai/gpt-4o-audio-preview", "label": "GPT-4o Audio", "kinds": [AUDIO]},
        ],
    },
}


@dataclass
class Choice:
    provider: str = ""
    model: str = ""
    reasoning: str = ""
    source: str = ""  # explicit | activity key | "" (nada configurado)


def check(provider: str, model: str, reasoning: str, kind: str = TEXT) -> str | None:
    """Devolve a mensagem de erro, ou None se a combinação é válida."""
    if not provider:
        return None if not (model or reasoning) else "escolha o provider antes do modelo"
    cat = CATALOG.get(provider)
    if cat is None:
        return "provider_desconhecido"
    if kind not in cat["kinds"]:
        return f"{provider} não faz {kind} pelo Growth OS"
    if model and not MODEL_RE.match(model):
        return "id de modelo inválido"
    if reasoning and reasoning not in {r["id"] for r in cat["reasoning"]}:
        return f"nível de raciocínio inválido para {provider}"
    return None


def kind_of(activity: str) -> str:
    return next((a["kind"] for a in ACTIVITIES if a["key"] == activity), TEXT)


def saved(db: DB) -> dict[str, ModelSetting]:
    return {r.activity: r for r in db.query(ModelSetting).all()}


def resolve(db: DB, activity: str) -> Choice:
    rows = saved(db)
    keys = [activity] if activity == "default" or kind_of(activity) != TEXT else [activity, "default"]
    for key in keys:
        r = rows.get(key)
        if r and r.provider:
            return Choice(r.provider, r.model or "", r.reasoning or "", key)
    return Choice()


def save(db: DB, activity: str, provider: str, model: str, reasoning: str) -> ModelSetting:
    r = db.get(ModelSetting, activity) or ModelSetting(activity=activity)
    r.provider, r.model, r.reasoning, r.updated_at = provider, model, reasoning, utcnow()
    db.add(r)
    db.commit()
    return r


# ---------- lista viva do OpenRouter (pública, sem chave) ----------

_OR_CACHE: dict = {"at": 0.0, "models": None}
_OR_TTL = 6 * 3600


async def openrouter_models() -> list[dict]:
    """Modelos do OpenRouter com o que cada um gera. Cai na lista fixa se a API não responder."""
    if _OR_CACHE["models"] is not None and time.time() - _OR_CACHE["at"] < _OR_TTL:
        return _OR_CACHE["models"]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://openrouter.ai/api/v1/models")
        resp.raise_for_status()
        out = []
        for m in resp.json().get("data", []):
            mid = str(m.get("id", ""))
            if not MODEL_RE.match(mid):
                continue
            outs = (m.get("architecture") or {}).get("output_modalities") or ["text"]
            kinds = [k for k in (TEXT, IMAGE, VIDEO, AUDIO) if k in outs]
            out.append({"id": mid, "label": str(m.get("name") or mid)[:80], "kinds": kinds or [TEXT],
                        "reasoning": "reasoning" in (m.get("supported_parameters") or [])})
        if out:
            _OR_CACHE.update(at=time.time(), models=out)
            return out
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return CATALOG["openrouter"]["models"]
