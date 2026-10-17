"""RBAC (perfis → permissões) e helpers ABAC (atributos do recurso)."""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"      # Supervisor Comercial / Operacional
    CONSULTOR = "consultor"        # Consultor Técnico
    AGENT = "agent"                # Atendente / Operador
    AUDITOR = "auditor"            # Auditor / DPO
    BOT = "bot"


ALL_PERMISSIONS: set[str] = {
    "org:manage", "users:manage", "users:read",
    "accounts:read", "accounts:write", "contacts:read", "contacts:write",
    "pii:view", "pii:reveal",
    "products:read", "products:manage",
    "deals:read", "deals:write", "deals:all", "deals:discount_high",
    "cases:read", "cases:write", "cases:all",
    "contracts:read", "contracts:write",
    "inbox:use", "inbox:all", "inbox:transfer", "inbox:close",
    "supervisor:live", "templates:read", "templates:manage",
    "surveys:manage", "kpi:view", "webhooks:manage", "whatsapp:manage",
    "lgpd:manage", "audit:view", "export:data",
}

_AGENT = {
    "users:read", "accounts:read", "accounts:write", "contacts:read", "contacts:write", "pii:reveal",
    "products:read", "deals:read", "deals:write", "cases:read", "cases:write", "contracts:read",
    "inbox:use", "inbox:transfer", "inbox:close", "templates:read",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN: set(ALL_PERMISSIONS),
    Role.SUPERVISOR: _AGENT | {
        "pii:view", "deals:all", "deals:discount_high", "cases:all", "contracts:write", "products:manage",
        "inbox:all", "supervisor:live", "templates:manage", "surveys:manage", "kpi:view", "export:data",
    },
    Role.CONSULTOR: {
        "users:read", "accounts:read", "accounts:write", "contacts:read", "contacts:write", "pii:reveal",
        "products:read", "deals:read", "cases:read", "cases:write", "cases:all", "contracts:read",
        "inbox:use", "inbox:transfer", "inbox:close", "templates:read", "kpi:view",
    },
    Role.AGENT: set(_AGENT),
    Role.AUDITOR: {
        "users:read", "accounts:read", "contacts:read", "pii:view", "deals:read", "deals:all", "cases:read",
        "cases:all", "contracts:read", "products:read", "kpi:view", "lgpd:manage", "audit:view", "export:data",
    },
    Role.BOT: {"inbox:use", "templates:read"},
}


def permissions_for(role: str) -> set[str]:
    return ROLE_PERMISSIONS.get(role, set())


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


# ---- ABAC -------------------------------------------------------------------

def can_access_ticket(role: str, user_id, user_department_ids: set, ticket) -> bool:
    """Atributos: ticket.assigned_agent_id, ticket.department_id e papel."""
    if has_permission(role, "inbox:all"):
        return True
    if not has_permission(role, "inbox:use"):
        return False
    if ticket.assigned_agent_id == user_id:
        return True
    # tickets na fila dos departamentos do usuário (ou sem departamento) ficam visíveis p/ assumir
    if ticket.assigned_agent_id is None and (ticket.department_id is None or ticket.department_id in user_department_ids):
        return True
    return False
