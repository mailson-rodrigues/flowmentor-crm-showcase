"""Mascaramento de dados pessoais (LGPD) para telas, logs e exportações."""
from __future__ import annotations

import re

_DIGITS = re.compile(r"\D+")


def mask_phone(value: str | None) -> str | None:
    """5562999999999 → +55 (62) 9****-**99"""
    if not value:
        return value
    d = _DIGITS.sub("", value)
    if len(d) < 8:
        return "*" * len(d)
    if d.startswith("55") and len(d) in (12, 13):
        ddd, rest = d[2:4], d[4:]
        first = rest[0]
        return f"+55 ({ddd}) {first}****-**{rest[-2:]}"
    return f"+{d[:2]}{'*' * (len(d) - 4)}{d[-2:]}"


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return value
    user, _, domain = value.partition("@")
    return f"{user[:1]}{'*' * max(len(user) - 1, 2)}@{domain}"


def mask_tax_id(value: str | None) -> str | None:
    """CNPJ 12.345.678/0001-90 → **.***.678/****-90"""
    if not value:
        return value
    d = _DIGITS.sub("", value)
    if len(d) == 14:
        return f"**.***.{d[5:8]}/****-{d[12:]}"
    if len(d) == 11:
        return f"***.{d[3:6]}.***-{d[9:]}"
    return "*" * max(len(d) - 2, 0) + d[-2:]


_RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_PHONE = re.compile(r"(?<!\d)(?:\+?55)?\s?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}(?!\d)")


def scrub_text(text: str | None) -> str | None:
    """Remove PII evidente de textos livres (usado em logs e na anonimização)."""
    if not text:
        return text
    text = _RE_CNPJ.sub("[CNPJ]", text)
    text = _RE_CPF.sub("[CPF]", text)
    text = _RE_EMAIL.sub("[EMAIL]", text)
    text = _RE_PHONE.sub("[TELEFONE]", text)
    return text
