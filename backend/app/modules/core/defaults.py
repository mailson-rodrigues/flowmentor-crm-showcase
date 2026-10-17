"""Default organization settings (deep merge with org.settings)."""
from __future__ import annotations

import copy
from datetime import datetime, time
from zoneinfo import ZoneInfo

DEFAULT_SETTINGS: dict = {
    "timezone": "America/Sao_Paulo",
    # 0 = segunda ... 6 = domingo
    "business_hours": {
        "0": [["08:00", "18:00"]], "1": [["08:00", "18:00"]], "2": [["08:00", "18:00"]],
        "3": [["08:00", "18:00"]], "4": [["08:00", "18:00"]], "5": [], "6": [],
    },
    "routing": {"default_strategy": "least_busy", "assign_on_available": True, "sticky_agent_hours": 72},
    "survey": {
        "enabled": True, "type": "csat", "grace_minutes": 3, "suppress_days": 7, "ttl_hours": 48,
        "ask_comment": True,
        "intro": "Seu atendimento foi finalizado. Poderia avaliar como foi?",
    },
    "bot": {
        "enabled": True,
        "greeting": "Olá! Sou o assistente virtual da Flowmentor. Como posso ajudar? Você pode falar sobre consultoria, laudos, canal médico ou treinamentos.",
        "handoff_keywords": ["atendente", "humano", "falar com alguém", "pessoa", "suporte", "reclamação"],
        "min_confidence": 0.55,
        "after_hours_message": "Nosso atendimento humano funciona em horário comercial. Registramos sua mensagem e retornaremos assim que possível.",
    },
    "discount": {"auto_approve_max_pct": 10.0, "institutional_tiers": [
        {"min_value": 0, "pct": 0}, {"min_value": 20000, "pct": 5}, {"min_value": 50000, "pct": 8}, {"min_value": 100000, "pct": 12},
    ]},
    "crm": {"auto_create_leads": True},
    "billing": {"pix_key": "", "merchant_name": "Flowmentor", "merchant_city": "Goiania"},
    "branding": {"company": "Flowmentor", "tagline": "Inteligência em Citometria de Fluxo"},
}


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("business_hours",):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def effective_settings(org_settings: dict | None) -> dict:
    return deep_merge(DEFAULT_SETTINGS, org_settings)


def is_within_business_hours(hours: dict, tz_name: str, at: datetime | None = None) -> bool:
    tz = ZoneInfo(tz_name)
    now = (at or datetime.now(tz)).astimezone(tz)
    windows = hours.get(str(now.weekday()), [])
    for start, end in windows:
        h1, m1 = map(int, start.split(":"))
        h2, m2 = map(int, end.split(":"))
        if time(h1, m1) <= now.time() < time(h2, m2):
            return True
    return False
