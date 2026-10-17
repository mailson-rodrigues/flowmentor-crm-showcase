"""Sanitização de conteúdo vindo de terceiros (mensagens do WhatsApp, formulários)."""
from __future__ import annotations

import html
import re

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAGS = re.compile(r"<[^>]*>")
_SCRIPT_BLOCK = re.compile(r"<\s*(script|style|iframe|object|embed)[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_JS_URL = re.compile(r"(?i)\b(javascript|vbscript|data)\s*:")


def sanitize_text(value: str | None, max_len: int = 8000) -> str | None:
    """Remove caracteres de controle, blocos ativos e tags; o front-end ainda usa textContent."""
    if value is None:
        return None
    value = _CTRL.sub("", value)
    value = _SCRIPT_BLOCK.sub("", value)
    value = _TAGS.sub("", value)
    value = _JS_URL.sub("", value)
    return value[:max_len]


def escape_html(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name or "arquivo")
    return name.strip("._")[:120] or "arquivo"
