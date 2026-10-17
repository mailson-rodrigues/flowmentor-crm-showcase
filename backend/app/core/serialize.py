"""Serialization helpers with PII masking according to the permissions of the principal."""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from app.security.masking import mask_email, mask_phone


def plain(v: Any) -> Any:
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def row(obj: Any, fields: Iterable[str], **extra: Any) -> dict[str, Any]:
    d = {f: plain(getattr(obj, f)) for f in fields}
    d.update({k: plain(v) for k, v in extra.items()})
    return d


def user_out(u) -> dict:
    return row(u, ["id", "name", "email", "role", "status", "is_active", "skills", "max_concurrent_chats", "last_login_at"],
               department_ids=[str(i) for i in u.department_ids])


def contact_out(c, principal=None, reveal: bool = False) -> dict:
    can_view = reveal or (principal is not None and principal.can("pii:view"))
    d = row(c, ["id", "name", "job_title", "tags", "source", "created_at", "last_interaction_at", "opted_out_at", "anonymized_at", "notes"])
    d["wa_id"] = c.wa_id if can_view else mask_phone(c.wa_id)
    d["email"] = c.email if can_view else mask_email(c.email)
    d["masked"] = not can_view
    return d


def account_out(a, principal=None, with_contacts: bool = False) -> dict:
    from app.security.masking import mask_tax_id

    can_view = principal is not None and principal.can("pii:view")
    d = row(a, ["id", "corporate_name", "trade_name", "account_type", "website", "status", "city", "state", "segment",
                "institutional_discount_pct", "owner_user_id", "tags", "notes", "created_at"])
    d["tax_id"] = a.tax_id if can_view else mask_tax_id(a.tax_id)
    if with_contacts:
        d["contacts"] = [
            {**contact_out(ac.contact, principal), "link_id": str(ac.id), "role_description": ac.role_description,
             "council_registration": ac.council_registration, "is_primary": ac.is_primary}
            for ac in a.contacts
        ]
    return d
