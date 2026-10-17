"""Módulo Core / Cadastros: usuários, departamentos, contatos, contas institucionais, atividades, API keys."""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.deps import Page, Principal, get_principal, pagination, require
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.events import emit
from app.core.serialize import account_out, contact_out, plain, row, user_out
from app.db import get_session, utcnow
from app.models import (
    Account, AccountContact, AccountType, Activity, ApiKey, Contact, Department, Organization, Ticket, User, UserDepartment,
)
from app.modules.core.defaults import DEFAULT_SETTINGS, deep_merge, effective_settings
from app.security.crypto import blind_index
from app.security.passwords import hash_password, validate_password_policy
from app.security.rbac import ALL_PERMISSIONS, Role, ROLE_PERMISSIONS
from app.security.sanitize import sanitize_text

router = APIRouter(prefix="/api/v1", tags=["Core / Cadastros"])


def normalize_wa_id(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) in (10, 11):  # número BR sem DDI
        d = "55" + d
    if len(d) < 10 or len(d) > 15:
        raise bad_request("Número de telefone inválido (use DDI+DDD+número)", code="invalid_phone")
    return d


def normalize_cnpj(raw: str | None) -> str | None:
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    if len(d) not in (11, 14):
        raise bad_request("CNPJ/CPF inválido", code="invalid_tax_id")
    return d


async def get_or_create_contact(session: AsyncSession, org_id: uuid.UUID, wa_id: str, name: str | None = None, source: str = "whatsapp") -> tuple[Contact, bool]:
    c = (await session.execute(sa.select(Contact).where(Contact.organization_id == org_id, Contact.wa_id == wa_id))).scalar_one_or_none()
    if c:
        if name and not c.name:
            c.name = sanitize_text(name, 150)
        return c, False
    c = Contact(organization_id=org_id, wa_id=wa_id, name=sanitize_text(name, 150) if name else None, source=source)
    session.add(c)
    await session.flush()
    return c, True


# ------------------------------------------------------------------ Organização
class OrgSettingsIn(BaseModel):
    settings: dict


@router.get("/org")
async def get_org(p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    org = await session.get(Organization, p.org_id)
    return {"id": str(org.id), "name": org.name, "slug": org.slug, "settings": effective_settings(org.settings),
            "defaults": DEFAULT_SETTINGS if p.can("org:manage") else None}


@router.put("/org/settings")
async def update_org_settings(body: OrgSettingsIn, request: Request, p: Principal = Depends(require("org:manage")), session: AsyncSession = Depends(get_session)):
    org = await session.get(Organization, p.org_id)
    old = dict(org.settings or {})
    org.settings = deep_merge(old, body.settings)
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_ORG_SETTINGS", resource_type="organizations", resource_id=org.id,
                           actor_user_id=p.user_id, request=request, old_state=old, new_state=org.settings)
    return {"settings": effective_settings(org.settings)}


@router.get("/roles")
async def roles(p: Principal = Depends(get_principal)):
    return {"roles": [{"role": r.value, "permissions": sorted(ROLE_PERMISSIONS[r])} for r in Role], "all_permissions": sorted(ALL_PERMISSIONS)}


# ------------------------------------------------------------------ Usuários
class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str | None = None
    role: Role = Role.AGENT
    skills: list[str] = []
    max_concurrent_chats: int = Field(5, ge=1, le=50)
    department_ids: list[uuid.UUID] = []
    is_active: bool = True


class UserPatch(BaseModel):
    name: str | None = None
    role: Role | None = None
    skills: list[str] | None = None
    max_concurrent_chats: int | None = Field(None, ge=1, le=50)
    department_ids: list[uuid.UUID] | None = None
    is_active: bool | None = None
    password: str | None = None


async def _set_user_departments(session: AsyncSession, user: User, org_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    valid = (await session.execute(sa.select(Department.id).where(Department.organization_id == org_id, Department.id.in_(ids)))).scalars().all() if ids else []
    await session.execute(sa.delete(UserDepartment).where(UserDepartment.user_id == user.id))
    for did in valid:
        session.add(UserDepartment(user_id=user.id, department_id=did))
    await session.flush()
    await session.refresh(user, ["departments"])


@router.get("/users")
async def list_users(role: str | None = None, p: Principal = Depends(require("users:read")), session: AsyncSession = Depends(get_session)):
    q = sa.select(User).where(User.organization_id == p.org_id).order_by(User.name)
    if role:
        q = q.where(User.role == role)
    return {"items": [user_out(u) for u in (await session.execute(q)).scalars().all()]}


@router.post("/users", status_code=201)
async def create_user(body: UserIn, request: Request, p: Principal = Depends(require("users:manage")), session: AsyncSession = Depends(get_session)):
    if (await session.execute(sa.select(User.id).where(sa.func.lower(User.email) == body.email.lower()))).first():
        raise conflict("E-mail já cadastrado", "email_taken")
    if body.password:
        problems = validate_password_policy(body.password)
        if problems:
            raise bad_request("Senha fraca: " + ", ".join(problems), code="weak_password")
    u = User(organization_id=p.org_id, name=body.name, email=body.email.lower(), role=body.role.value, skills=body.skills,
             max_concurrent_chats=body.max_concurrent_chats, is_active=body.is_active,
             password_hash=hash_password(body.password) if body.password else None, status="offline")
    session.add(u)
    await session.flush()
    await _set_user_departments(session, u, p.org_id, body.department_ids)
    await audit.log_action(session, org_id=p.org_id, action="CREATE_USER", resource_type="users", resource_id=u.id, actor_user_id=p.user_id,
                           request=request, new_state={"email": u.email, "role": u.role})
    return user_out(u)


@router.patch("/users/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserPatch, request: Request, p: Principal = Depends(require("users:manage")), session: AsyncSession = Depends(get_session)):
    u = await session.get(User, user_id)
    if not u or u.organization_id != p.org_id:
        raise not_found("Usuário")
    old = {"role": u.role, "is_active": u.is_active}
    data = body.model_dump(exclude_unset=True)
    if "password" in data and data["password"]:
        problems = validate_password_policy(data["password"])
        if problems:
            raise bad_request("Senha fraca: " + ", ".join(problems), code="weak_password")
        u.password_hash = hash_password(data["password"])
    for f in ("name", "skills", "max_concurrent_chats", "is_active"):
        if f in data and data[f] is not None:
            setattr(u, f, data[f])
    if data.get("role"):
        u.role = data["role"].value
    if "department_ids" in data and data["department_ids"] is not None:
        await _set_user_departments(session, u, p.org_id, data["department_ids"])
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_USER", resource_type="users", resource_id=u.id, actor_user_id=p.user_id,
                           request=request, old_state=old, new_state={"role": u.role, "is_active": u.is_active})
    return user_out(u)


# ------------------------------------------------------------------ Departamentos
class DepartmentIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    sla_first_response_seconds: int = Field(300, ge=10)
    sla_solution_seconds: int = Field(3600, ge=60)
    routing_strategy: str = Field("least_busy", pattern="^(round_robin|least_busy|skill_based)$")
    required_skills: list[str] = []
    business_hours: dict | None = None
    after_hours_message: str | None = None
    is_default: bool = False


DEPT_FIELDS = ["id", "name", "sla_first_response_seconds", "sla_solution_seconds", "routing_strategy", "required_skills", "business_hours", "after_hours_message", "is_default"]


@router.get("/departments")
async def list_departments(p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(sa.select(Department).where(Department.organization_id == p.org_id).order_by(Department.name))).scalars().all()
    return {"items": [row(d, DEPT_FIELDS) for d in rows]}


@router.post("/departments", status_code=201)
async def create_department(body: DepartmentIn, p: Principal = Depends(require("org:manage")), session: AsyncSession = Depends(get_session)):
    if body.is_default:
        await session.execute(sa.update(Department).where(Department.organization_id == p.org_id).values(is_default=False))
    d = Department(organization_id=p.org_id, **body.model_dump())
    session.add(d)
    await session.flush()
    return row(d, DEPT_FIELDS)


@router.put("/departments/{dept_id}")
async def update_department(dept_id: uuid.UUID, body: DepartmentIn, p: Principal = Depends(require("org:manage")), session: AsyncSession = Depends(get_session)):
    d = await session.get(Department, dept_id)
    if not d or d.organization_id != p.org_id:
        raise not_found("Departamento")
    if body.is_default:
        await session.execute(sa.update(Department).where(Department.organization_id == p.org_id).values(is_default=False))
    for k, v in body.model_dump().items():
        setattr(d, k, v)
    return row(d, DEPT_FIELDS)


@router.delete("/departments/{dept_id}", status_code=204)
async def delete_department(dept_id: uuid.UUID, p: Principal = Depends(require("org:manage")), session: AsyncSession = Depends(get_session)):
    d = await session.get(Department, dept_id)
    if not d or d.organization_id != p.org_id:
        raise not_found("Departamento")
    await session.delete(d)


# ------------------------------------------------------------------ Contatos
class ContactIn(BaseModel):
    wa_id: str
    name: str | None = Field(None, max_length=150)
    email: EmailStr | None = None
    job_title: str | None = Field(None, max_length=100)
    tags: list[str] = []
    notes: str | None = None


class ContactPatch(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    job_title: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


@router.get("/contacts")
async def list_contacts(q: str | None = None, page: Page = Depends(pagination), p: Principal = Depends(require("contacts:read")), session: AsyncSession = Depends(get_session)):
    stmt = sa.select(Contact).where(Contact.organization_id == p.org_id, Contact.anonymized_at.is_(None))
    if q:
        like = f"%{q.lower()}%"
        digits = re.sub(r"\D", "", q)
        cond = sa.or_(sa.func.lower(Contact.name).like(like), sa.func.lower(Contact.email).like(like))
        if digits:
            cond = sa.or_(cond, Contact.wa_id.like(f"%{digits}%"))
        stmt = stmt.where(cond)
    total = (await session.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Contact.created_at.desc()).limit(page.limit).offset(page.offset))).scalars().all()
    return {"total": total, "items": [contact_out(c, p) for c in rows]}


@router.post("/contacts", status_code=201)
async def create_contact(body: ContactIn, request: Request, p: Principal = Depends(require("contacts:write")), session: AsyncSession = Depends(get_session)):
    wa = normalize_wa_id(body.wa_id)
    if (await session.execute(sa.select(Contact.id).where(Contact.organization_id == p.org_id, Contact.wa_id == wa))).first():
        raise conflict("Já existe contato com este número", "contact_exists")
    c = Contact(organization_id=p.org_id, wa_id=wa, name=sanitize_text(body.name, 150), email=body.email, job_title=body.job_title,
                tags=body.tags, notes=sanitize_text(body.notes, 4000), source="manual")
    session.add(c)
    await session.flush()
    await audit.log_action(session, org_id=p.org_id, action="CREATE_CONTACT", resource_type="contacts", resource_id=c.id, actor_user_id=p.user_id, request=request,
                           new_state={"name": c.name, "wa_id": c.wa_id})
    emit(session, "contact.created", p.org_id, {"contact_id": str(c.id)}, p.user_id)
    return contact_out(c, p)


async def _contact_or_404(session: AsyncSession, org_id: uuid.UUID, cid: uuid.UUID) -> Contact:
    c = await session.get(Contact, cid)
    if not c or c.organization_id != org_id:
        raise not_found("Contato")
    return c


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: uuid.UUID, p: Principal = Depends(require("contacts:read")), session: AsyncSession = Depends(get_session)):
    c = await _contact_or_404(session, p.org_id, contact_id)
    links = (await session.execute(sa.select(AccountContact, Account).join(Account, Account.id == AccountContact.account_id).where(AccountContact.contact_id == c.id))).all()
    out = contact_out(c, p)
    out["accounts"] = [{"id": str(a.id), "name": a.trade_name or a.corporate_name, "role_description": ac.role_description,
                        "council_registration": ac.council_registration} for ac, a in links]
    return out


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: uuid.UUID, body: ContactPatch, request: Request, p: Principal = Depends(require("contacts:write")), session: AsyncSession = Depends(get_session)):
    c = await _contact_or_404(session, p.org_id, contact_id)
    old = {"name": c.name, "email": c.email, "job_title": c.job_title}
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, sanitize_text(v, 4000) if isinstance(v, str) else v)
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_CONTACT", resource_type="contacts", resource_id=c.id, actor_user_id=p.user_id, request=request,
                           old_state=old, new_state={"name": c.name, "email": c.email, "job_title": c.job_title})
    return contact_out(c, p)


@router.post("/contacts/{contact_id}/reveal")
async def reveal_contact(contact_id: uuid.UUID, request: Request, p: Principal = Depends(require("pii:reveal")), session: AsyncSession = Depends(get_session)):
    """Revela o telefone/e-mail completos — cada acesso é gravado na trilha de auditoria (VIEW_PHONE)."""
    c = await _contact_or_404(session, p.org_id, contact_id)
    await audit.log_action(session, org_id=p.org_id, action="VIEW_PHONE", resource_type="contacts", resource_id=c.id, actor_user_id=p.user_id, request=request)
    return {"wa_id": c.wa_id, "email": c.email}


# ------------------------------------------------------------------ Contas institucionais
class AccountIn(BaseModel):
    corporate_name: str = Field(min_length=2, max_length=200)
    trade_name: str | None = Field(None, max_length=150)
    tax_id: str | None = None
    account_type: AccountType = AccountType.laboratorio
    website: str | None = Field(None, max_length=150)
    status: str = Field("prospect", pattern="^(prospect|customer|inactive)$")
    city: str | None = None
    state: str | None = Field(None, max_length=2)
    segment: str | None = None
    institutional_discount_pct: float = Field(0, ge=0, le=100)
    owner_user_id: uuid.UUID | None = None
    tags: list[str] = []
    notes: str | None = None


@router.get("/accounts")
async def list_accounts(q: str | None = None, account_type: str | None = None, status: str | None = None, page: Page = Depends(pagination),
                        p: Principal = Depends(require("accounts:read")), session: AsyncSession = Depends(get_session)):
    stmt = sa.select(Account).where(Account.organization_id == p.org_id)
    if q:
        like = f"%{q.lower()}%"
        cond = sa.or_(sa.func.lower(Account.corporate_name).like(like), sa.func.lower(Account.trade_name).like(like))
        digits = re.sub(r"\D", "", q)
        if len(digits) in (11, 14):
            cond = sa.or_(cond, Account.tax_id_hash == blind_index(digits, "tax_id"))
        stmt = stmt.where(cond)
    if account_type:
        stmt = stmt.where(Account.account_type == account_type)
    if status:
        stmt = stmt.where(Account.status == status)
    total = (await session.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Account.corporate_name).limit(page.limit).offset(page.offset))).scalars().all()
    return {"total": total, "items": [account_out(a, p) for a in rows]}


@router.post("/accounts", status_code=201)
async def create_account(body: AccountIn, request: Request, p: Principal = Depends(require("accounts:write")), session: AsyncSession = Depends(get_session)):
    tax = normalize_cnpj(body.tax_id)
    if tax and (await session.execute(sa.select(Account.id).where(Account.organization_id == p.org_id, Account.tax_id_hash == blind_index(tax, "tax_id")))).first():
        raise conflict("Já existe conta com este CNPJ", "account_exists")
    data = body.model_dump(exclude={"tax_id"})
    a = Account(organization_id=p.org_id, tax_id=tax, tax_id_hash=blind_index(tax, "tax_id"), owner_user_id=body.owner_user_id or p.user_id, **{k: v for k, v in data.items() if k != "owner_user_id"})
    session.add(a)
    await session.flush()
    await session.refresh(a, ["contacts"])
    await audit.log_action(session, org_id=p.org_id, action="CREATE_ACCOUNT", resource_type="accounts", resource_id=a.id, actor_user_id=p.user_id, request=request,
                           new_state={"corporate_name": a.corporate_name, "tax_id": "***"})
    emit(session, "account.created", p.org_id, {"account_id": str(a.id), "name": a.corporate_name}, p.user_id)
    return account_out(a, p, with_contacts=True)


async def _account_or_404(session: AsyncSession, org_id: uuid.UUID, aid: uuid.UUID) -> Account:
    a = await session.get(Account, aid)
    if not a or a.organization_id != org_id:
        raise not_found("Conta")
    return a


@router.get("/accounts/{account_id}")
async def get_account(account_id: uuid.UUID, request: Request, p: Principal = Depends(require("accounts:read")), session: AsyncSession = Depends(get_session)):
    from app.models import Deal

    a = await _account_or_404(session, p.org_id, account_id)
    out = account_out(a, p, with_contacts=True)
    deals = (await session.execute(sa.select(Deal).where(Deal.account_id == a.id).order_by(Deal.created_at.desc()).limit(50))).scalars().all()
    out["deals"] = [row(d, ["id", "title", "stage", "expected_value", "final_contracted_value", "discount_percentage"]) for d in deals]
    if p.can("pii:view"):
        await audit.log_action(session, org_id=p.org_id, action="VIEW_ACCOUNT", resource_type="accounts", resource_id=a.id, actor_user_id=p.user_id, request=request)
    return out


@router.put("/accounts/{account_id}")
async def update_account(account_id: uuid.UUID, body: AccountIn, request: Request, p: Principal = Depends(require("accounts:write")), session: AsyncSession = Depends(get_session)):
    a = await _account_or_404(session, p.org_id, account_id)
    old = {"corporate_name": a.corporate_name, "status": a.status}
    tax = normalize_cnpj(body.tax_id) if body.tax_id and "*" not in body.tax_id else None
    for k, v in body.model_dump(exclude={"tax_id"}, exclude_unset=True).items():
        setattr(a, k, v)
    if tax:
        a.tax_id, a.tax_id_hash = tax, blind_index(tax, "tax_id")
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_ACCOUNT", resource_type="accounts", resource_id=a.id, actor_user_id=p.user_id, request=request,
                           old_state=old, new_state={"corporate_name": a.corporate_name, "status": a.status})
    await session.refresh(a, ["contacts"])
    return account_out(a, p, with_contacts=True)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID, request: Request, p: Principal = Depends(require("org:manage")), session: AsyncSession = Depends(get_session)):
    a = await _account_or_404(session, p.org_id, account_id)
    await audit.log_action(session, org_id=p.org_id, action="DELETE_ACCOUNT", resource_type="accounts", resource_id=a.id, actor_user_id=p.user_id, request=request,
                           old_state={"corporate_name": a.corporate_name})
    await session.delete(a)


class AccountContactIn(BaseModel):
    contact_id: uuid.UUID | None = None
    new_contact: ContactIn | None = None
    role_description: str | None = Field(None, max_length=100)
    council_registration: str | None = Field(None, max_length=50)
    is_primary: bool = False


@router.post("/accounts/{account_id}/contacts", status_code=201)
async def link_contact(account_id: uuid.UUID, body: AccountContactIn, p: Principal = Depends(require("accounts:write")), session: AsyncSession = Depends(get_session)):
    a = await _account_or_404(session, p.org_id, account_id)
    if body.contact_id:
        c = await _contact_or_404(session, p.org_id, body.contact_id)
    elif body.new_contact:
        c, _ = await get_or_create_contact(session, p.org_id, normalize_wa_id(body.new_contact.wa_id), body.new_contact.name, "manual")
        c.email = body.new_contact.email or c.email
        c.job_title = body.new_contact.job_title or c.job_title
    else:
        raise bad_request("Informe contact_id ou new_contact")
    if (await session.execute(sa.select(AccountContact.id).where(AccountContact.account_id == a.id, AccountContact.contact_id == c.id))).first():
        raise conflict("Contato já vinculado à conta")
    if body.is_primary:
        await session.execute(sa.update(AccountContact).where(AccountContact.account_id == a.id).values(is_primary=False))
    link = AccountContact(account_id=a.id, contact_id=c.id, role_description=body.role_description, council_registration=body.council_registration, is_primary=body.is_primary)
    session.add(link)
    await session.flush()
    emit(session, "contact.linked", p.org_id, {"account_id": str(a.id), "contact_id": str(c.id)}, p.user_id)
    return {"link_id": str(link.id), "contact": contact_out(c, p)}


@router.delete("/accounts/{account_id}/contacts/{link_id}", status_code=204)
async def unlink_contact(account_id: uuid.UUID, link_id: uuid.UUID, p: Principal = Depends(require("accounts:write")), session: AsyncSession = Depends(get_session)):
    await _account_or_404(session, p.org_id, account_id)
    link = await session.get(AccountContact, link_id)
    if not link or link.account_id != account_id:
        raise not_found("Vínculo")
    await session.delete(link)


# ------------------------------------------------------------------ Timeline / atividades
class ActivityIn(BaseModel):
    kind: str = Field(pattern="^(note|meeting|call|task|email)$")
    title: str = Field(min_length=1, max_length=250)
    body: str | None = None
    account_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    due_at: datetime | None = None


def activity_out(a: Activity) -> dict:
    return row(a, ["id", "kind", "title", "body", "data", "account_id", "contact_id", "deal_id", "ticket_id", "case_id", "user_id", "due_at", "done_at", "created_at"])


@router.post("/activities", status_code=201)
async def create_activity(body: ActivityIn, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    if not (body.account_id or body.contact_id or body.deal_id):
        raise bad_request("Vincule a atividade a uma conta, contato ou oportunidade")
    a = Activity(organization_id=p.org_id, user_id=p.user_id, kind=body.kind, title=sanitize_text(body.title, 250), body=sanitize_text(body.body, 8000),
                 account_id=body.account_id, contact_id=body.contact_id, deal_id=body.deal_id, due_at=body.due_at)
    session.add(a)
    await session.flush()
    return activity_out(a)


@router.post("/activities/{activity_id}/done")
async def complete_activity(activity_id: uuid.UUID, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    a = await session.get(Activity, activity_id)
    if not a or a.organization_id != p.org_id:
        raise not_found("Atividade")
    a.done_at = utcnow()
    return activity_out(a)


@router.get("/timeline")
async def timeline(account_id: uuid.UUID | None = None, contact_id: uuid.UUID | None = None, deal_id: uuid.UUID | None = None,
                   limit: int = 100, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    """Timeline unificada: WhatsApp (tickets/mensagens), notas, reuniões, mudanças de estágio e casos."""
    stmt = sa.select(Activity).where(Activity.organization_id == p.org_id)
    if account_id:
        stmt = stmt.where(Activity.account_id == account_id)
    if contact_id:
        stmt = stmt.where(Activity.contact_id == contact_id)
    if deal_id:
        stmt = stmt.where(Activity.deal_id == deal_id)
    rows = (await session.execute(stmt.order_by(Activity.created_at.desc()).limit(min(limit, 300)))).scalars().all()
    return {"items": [activity_out(a) for a in rows]}


@router.get("/tasks")
async def my_tasks(p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    stmt = sa.select(Activity).where(Activity.organization_id == p.org_id, Activity.kind == "task", Activity.done_at.is_(None))
    if not p.can("deals:all") and p.user_id:
        stmt = stmt.where(Activity.user_id == p.user_id)
    rows = (await session.execute(stmt.order_by(Activity.due_at.asc().nulls_last()).limit(100))).scalars().all()
    return {"items": [activity_out(a) for a in rows]}


# ------------------------------------------------------------------ API Keys (Open API)
class ApiKeyIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    scopes: list[str] = ["accounts:read", "contacts:read", "contacts:write", "deals:read", "deals:write", "cases:read"]


@router.get("/api-keys")
async def list_api_keys(p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(sa.select(ApiKey).where(ApiKey.organization_id == p.org_id).order_by(ApiKey.created_at.desc()))).scalars().all()
    return {"items": [row(k, ["id", "name", "prefix", "scopes", "last_used_at", "revoked_at", "created_at"]) for k in rows]}


@router.post("/api-keys", status_code=201)
async def create_api_key(body: ApiKeyIn, request: Request, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    bad = [s for s in body.scopes if s not in ALL_PERMISSIONS]
    if bad:
        raise bad_request(f"Escopos inválidos: {bad}")
    raw = "fm_" + secrets.token_urlsafe(32)
    k = ApiKey(organization_id=p.org_id, name=body.name, prefix=raw[:10], key_hash=hashlib.sha256(raw.encode()).hexdigest(), scopes=body.scopes, created_by=p.user_id)
    session.add(k)
    await session.flush()
    await audit.log_action(session, org_id=p.org_id, action="CREATE_API_KEY", resource_type="api_keys", resource_id=k.id, actor_user_id=p.user_id, request=request)
    return {"id": str(k.id), "key": raw, "note": "Guarde esta chave: ela não será exibida novamente."}


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: uuid.UUID, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    k = await session.get(ApiKey, key_id)
    if not k or k.organization_id != p.org_id:
        raise not_found("Chave")
    k.revoked_at = utcnow()
