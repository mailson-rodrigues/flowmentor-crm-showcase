"""Camada de IA: resumos de atendimento, classificação de sentimento (NLP) e intenção.

Usa a API da Anthropic quando ``ANTHROPIC_API_KEY`` está configurada; caso contrário, aplica heurísticas
determinísticas em português (léxico de sentimento e extração de frases-chave) para que o sistema funcione offline.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata

import httpx

from app.config import settings

log = logging.getLogger("flowmentor.llm")

API_URL = "https://api.anthropic.com/v1/messages"


async def complete(system: str, user: str, max_tokens: int = 400) -> str | None:
    if not settings.anthropic_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(API_URL, headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             json={"model": settings.llm_model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception:  # noqa: BLE001
        log.warning("falha ao chamar LLM; usando fallback heurístico", exc_info=True)
        return None


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()


POSITIVE = {"otimo", "excelente", "perfeito", "obrigado", "obrigada", "agradeco", "parabens", "rapido", "atencioso", "atenciosa", "resolveu", "resolvido", "satisfeito",
            "satisfeita", "maravilhoso", "bom", "boa", "top", "show", "ajudou", "eficiente", "claro", "gostei", "amei", "recomendo", "nota 10"}
NEGATIVE = {"pessimo", "horrivel", "ruim", "demora", "demorou", "lento", "lenta", "nao resolveu", "insatisfeito", "insatisfeita", "descaso", "reclamacao", "decepcionado",
            "decepcionada", "inadmissivel", "absurdo", "problema", "erro", "falha", "pior", "mal atendido", "ninguem", "nunca", "cancelar", "processo"}
NEGATORS = {"nao", "nunca", "jamais", "sem"}


def heuristic_sentiment(text: str) -> str:
    t = _norm(text)
    tokens = re.findall(r"[a-z0-9]+", t)
    score = 0
    for i, tok in enumerate(tokens):
        neg = i > 0 and tokens[i - 1] in NEGATORS
        if tok in POSITIVE:
            score += -1 if neg else 1
        elif tok in NEGATIVE:
            score += 1 if neg and tok in {"problema", "erro", "falha"} else -1
    for phrase in ("nao resolveu", "mal atendido", "nota 10"):
        if phrase in t:
            score += (-2 if phrase != "nota 10" else 2)
    return "positive" if score > 0 else "negative" if score < 0 else "neutral"


async def classify_sentiment(text: str) -> str:
    if not text or not text.strip():
        return "neutral"
    out = await complete(
        "Classifique o sentimento do comentário de um cliente em português. Responda APENAS com uma palavra: positive, neutral ou negative.", text[:1500], 5)
    if out:
        w = out.strip().lower()
        for label in ("positive", "negative", "neutral"):
            if label in w:
                return label
    return heuristic_sentiment(text)


def _fmt_transcript(messages: list[dict], limit: int = 40) -> str:
    lines = []
    for m in messages[-limit:]:
        who = "Cliente" if m["direction"] == "inbound" else ("Bot" if m.get("is_bot") else "Atendente")
        lines.append(f"{who}: {m.get('body') or '[' + m.get('type', 'mídia') + ']'}")
    return "\n".join(lines)


async def summarize_conversation(messages: list[dict]) -> str:
    """Resumo curto do atendimento (no handoff bot→humano, transferência ou fechamento)."""
    if not messages:
        return ""
    transcript = _fmt_transcript(messages)
    out = await complete(
        "Você resume atendimentos de WhatsApp de uma empresa de citometria de fluxo e consultoria laboratorial. Em português, escreva um resumo objetivo de "
        "no máximo 4 linhas: motivo do contato, o que foi feito/decidido e pendências. Não inclua dados pessoais sensíveis (telefones, CPF, CNPJ).", transcript, 300)
    if out:
        return out
    inbound = [m for m in messages if m["direction"] == "inbound" and m.get("body")]
    outbound = [m for m in messages if m["direction"] == "outbound" and not m.get("is_bot") and m.get("body")]
    parts = []
    if inbound:
        parts.append(f"Motivo: {inbound[0]['body'][:160]}")
    if outbound:
        parts.append(f"Última resposta: {outbound[-1]['body'][:160]}")
    parts.append(f"{len(messages)} mensagens trocadas ({len(inbound)} do cliente).")
    return " | ".join(parts)


# Intenções do bot: palavras-chave → (intent, peso)
INTENTS: dict[str, list[str]] = {
    "consultoria": ["consultoria", "pop", "procedimento operacional", "padronizacao", "euroflow", "painel", "validacao", "qualidade", "auditoria", "acreditacao", "pncq", "iso 15189", "oms"],
    "apoio_diagnostico": ["laudo", "fcs", "citometria", "imunofenotipagem", "exame", "resultado", "urgente", "amostra", "leucemia", "linfoma", "doenca residual", "dmr", "analise"],
    "canal_medico": ["hematologista", "medico", "residente", "discussao", "caso clinico", "interconsulta", "reuniao clinica", "multidisciplinar"],
    "capacitacao": ["curso", "treinamento", "capacitacao", "escola", "assinatura", "aula", "comunidade", "workshop", "certificado"],
    "comercial": ["preco", "valor", "orcamento", "proposta", "contrato", "plano", "quanto custa", "desconto", "contratar"],
    "financeiro": ["boleto", "nota fiscal", "nfs", "fatura", "pagamento", "pix", "cobranca"],
}


def heuristic_intent(text: str) -> tuple[str, float]:
    t = _norm(text)
    best, best_score = "desconhecido", 0
    for intent, kws in INTENTS.items():
        score = sum(1 for k in kws if k in t)
        if score > best_score:
            best, best_score = intent, score
    if best_score == 0:
        return "desconhecido", 0.2
    return best, min(0.5 + 0.2 * best_score, 0.95)


async def classify_intent(text: str) -> tuple[str, float]:
    out = await complete(
        "Classifique a mensagem de um cliente em UMA intenção entre: consultoria, apoio_diagnostico, canal_medico, capacitacao, comercial, financeiro, desconhecido. "
        'Responda apenas JSON: {"intent": "...", "confidence": 0.0-1.0}', text[:800], 40)
    if out:
        try:
            data = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            if data.get("intent") in INTENTS or data.get("intent") == "desconhecido":
                return data["intent"], float(data.get("confidence", 0.5))
        except Exception:  # noqa: BLE001
            pass
    return heuristic_intent(text)
