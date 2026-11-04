"""Quanto as chamadas ao OpenRouter custam: por atividade, por modelo e por dia, a partir do log `ai_calls`.

Ordem de confiança para cada chamada:
1. `api`: o custo em dólar que o próprio OpenRouter devolveu na resposta (gravado desde que o registro de uso existe);
2. `estimado`: tokens registrados × preços publicados do modelo (prompt/completion), ou, para imagem, o preço por imagem;
3. `sem uso`: chamadas antigas, de antes do registro de uso, sem tokens nem custo. Entram na contagem, mas não no total em dólar.
Só o OpenRouter cobra por chamada: Claude e Codex rodam pelo login do CLI (assinatura) e aparecem só como contagem.
"""
from collections import defaultdict
from datetime import timedelta

from . import modelcfg
from .db import AiCall, utcnow

LABELS = {"piece_render": "Imagem da peça (template)", "video_render": "Renderização de vídeo"}


def price_of(call: AiCall, pricing: dict) -> tuple[float | None, str]:
    """(dólar, fonte) de uma chamada do OpenRouter."""
    if call.cost_usd is not None:
        return call.cost_usd, "api"
    if call.input_tokens is not None or call.output_tokens is not None:
        p_in, p_out = pricing.get("prompt"), pricing.get("completion")
        if p_in is None and p_out is None and not pricing:
            return None, "sem uso"  # sem preços conhecidos para o modelo
        return (call.input_tokens or 0) * (p_in or 0) + (call.output_tokens or 0) * (p_out or 0), "estimado"
    if call.activity == "image" and call.status == "ok" and pricing.get("image"):
        return pricing["image"] + (pricing.get("request") or 0), "estimado"  # preço por imagem × 1 imagem
    return None, "sem uso"


def _bucket() -> dict:
    return {"calls": 0, "ok": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "priced": 0, "unpriced": 0}


def _add(b: dict, call: AiCall, cost: float | None) -> None:
    b["calls"] += 1
    b["ok"] += call.status == "ok"
    b["errors"] += call.status == "error"
    b["input_tokens"] += call.input_tokens or 0
    b["output_tokens"] += call.output_tokens or 0
    if cost is None:
        b["unpriced"] += 1
    else:
        b["priced"] += 1
        b["cost_usd"] += cost


def _rows(groups: dict, name_key: str, labels: dict | None = None) -> list[dict]:
    out = [{name_key: k, "label": (labels or {}).get(k, k), **v, "cost_usd": round(v["cost_usd"], 6)} for k, v in groups.items()]
    return sorted(out, key=lambda r: (-r["cost_usd"], -r["calls"]))


def summarize(db, models: list[dict], days: int = 30) -> dict:
    """Resumo dos últimos `days` dias. `models` = lista do OpenRouter com preços (modelcfg.openrouter_models)."""
    prices = {m["id"]: {k: v for k, v in (m.get("pricing") or {}).items() if v is not None} for m in models}
    since = utcnow() - timedelta(days=days)
    rows = db.query(AiCall).filter(AiCall.at >= since, AiCall.status != "running").order_by(AiCall.at).all()
    labels = {a["key"]: a["label"] for a in modelcfg.ACTIVITIES} | LABELS
    total, other = _bucket(), defaultdict(int)
    by_act, by_model, by_day = defaultdict(_bucket), defaultdict(_bucket), defaultdict(_bucket)
    sources = defaultdict(int)
    for c in rows:
        if c.provider != "openrouter":
            if c.provider in ("claude", "codex"):
                other[c.provider] += 1
            continue
        cost, source = price_of(c, prices.get(c.model, {}))
        sources[source] += 1
        for b in (total, by_act[c.activity], by_model[c.model or "—"], by_day[c.at.date().isoformat()]):
            _add(b, c, cost)
    total["cost_usd"] = round(total["cost_usd"], 6)
    return {
        "days": days, "since": since.isoformat(), "prices_live": bool(prices) and any(prices.values()),
        "total": total, "sources": dict(sources), "subscription_calls": dict(other),
        "by_activity": _rows(by_act, "activity", labels), "by_model": _rows(by_model, "model"),
        "by_day": [{"day": k, **v, "cost_usd": round(v["cost_usd"], 6)} for k, v in sorted(by_day.items())],
    }
