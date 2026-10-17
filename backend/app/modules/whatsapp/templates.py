"""HSM template management (Marketing / Utility / Authentication): validation, submission and status."""
from __future__ import annotations

import re
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, require
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.events import emit
from app.core.serialize import row
from app.db import get_session, utcnow
from app.models import TemplateStatus, WaTemplate, WhatsAppAccount
from app.modules.whatsapp.meta_client import MetaClient, MetaError

router = APIRouter(prefix="/api/v1/templates", tags=["Templates HSM"])

CATEGORIES = {"MARKETING", "UTILITY", "AUTHENTICATION"}
VAR_RE = re.compile(r"\{\{(\d+)\}\}")
NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
LANG_RE = re.compile(r"^[a-z]{2}(_[A-Z]{2})?$")


def variables_of(text: str) -> list[int]:
    return [int(x) for x in VAR_RE.findall(text or "")]


def validate_template(name: str, language: str, category: str, components: list[dict], sample_values: dict | None = None) -> dict:
    """Validates Meta rules before submitting. Returns {"errors": [...], "warnings": [...]}."""
    errors: list[str] = []
    warnings: list[str] = []
    sample_values = sample_values or {}
    if not NAME_RE.match(name or ""):
        errors.append("Nome deve conter apenas letras minúsculas, números e underscore (ex.: boas_vindas_laudo).")
    if not LANG_RE.match(language or ""):
        errors.append("Idioma inválido (use o formato pt_BR, en_US...).")
    if category not in CATEGORIES:
        errors.append("Categoria deve ser MARKETING, UTILITY ou AUTHENTICATION.")
    by_type: dict[str, list[dict]] = {}
    for c in components:
        by_type.setdefault(str(c.get("type", "")).upper(), []).append(c)
    for t, items in by_type.items():
        if t not in ("HEADER", "BODY", "FOOTER", "BUTTONS"):
            errors.append(f"Componente desconhecido: {t}")
        elif len(items) > 1:
            errors.append(f"Componente {t} duplicado.")

    def check_vars(label: str, text: str, allow: bool = True, max_vars: int | None = None):
        vs = variables_of(text)
        if not allow and vs:
            errors.append(f"{label} não aceita variáveis.")
            return
        if vs and sorted(set(vs)) != list(range(1, len(set(vs)) + 1)):
            errors.append(f"{label}: variáveis devem ser sequenciais a partir de {{{{1}}}} (encontrado {sorted(set(vs))}).")
        if max_vars and len(set(vs)) > max_vars:
            errors.append(f"{label}: no máximo {max_vars} variável(is).")
        for v in set(vs):
            key = f"{label.lower()}_{v}"
            if not sample_values.get(key) and not sample_values.get(str(v)):
                warnings.append(f"{label}: informe um exemplo para a variável {{{{{v}}}}} (exigido pela Meta na aprovação).")

    body = (by_type.get("BODY") or [None])[0]
    if category == "AUTHENTICATION":
        if not any(b.get("type", "").upper() == "OTP" for btns in by_type.get("BUTTONS", []) for b in btns.get("buttons", [])):
            warnings.append("Templates de autenticação devem ter botão OTP (copiar código).")
    elif body is None or not (body.get("text") or "").strip():
        errors.append("O componente BODY é obrigatório.")
    else:
        text = body["text"]
        if len(text) > 1024:
            errors.append("BODY excede 1024 caracteres.")
        check_vars("Corpo", text)
        stripped = text.strip()
        if VAR_RE.match(stripped) or re.search(r"\{\{\d+\}\}$", stripped):
            errors.append("BODY não pode começar nem terminar com uma variável.")
        if re.search(r"\{\{\d+\}\}\s*\{\{\d+\}\}", text):
            errors.append("Variáveis não podem ser adjacentes.")
        if re.search(r"(\n\s*){4,}|\t| {5,}", text):
            errors.append("BODY não pode conter 4+ quebras de linha consecutivas, tabulações ou 5+ espaços.")
        n_vars = len(set(variables_of(text)))
        if n_vars and len(re.sub(r"\{\{\d+\}\}", "", text).split()) < n_vars * 2:
            warnings.append("Muitas variáveis para pouco texto — a Meta pode rejeitar. Acrescente contexto ao corpo.")
    header = (by_type.get("HEADER") or [None])[0]
    if header:
        fmt = str(header.get("format", "TEXT")).upper()
        if fmt not in ("TEXT", "IMAGE", "VIDEO", "DOCUMENT", "LOCATION"):
            errors.append("Formato de HEADER inválido.")
        if fmt == "TEXT":
            if len(header.get("text", "")) > 60:
                errors.append("HEADER de texto excede 60 caracteres.")
            check_vars("Cabeçalho", header.get("text", ""), max_vars=1)
    footer = (by_type.get("FOOTER") or [None])[0]
    if footer:
        if len(footer.get("text", "")) > 60:
            errors.append("FOOTER excede 60 caracteres.")
        if variables_of(footer.get("text", "")):
            errors.append("FOOTER não aceita variáveis.")
    btn_comp = (by_type.get("BUTTONS") or [None])[0]
    if btn_comp:
        buttons = btn_comp.get("buttons", [])
        if len(buttons) > 10:
            errors.append("No máximo 10 botões.")
        kinds = [b.get("type", "").upper() for b in buttons]
        if kinds.count("URL") > 2:
            errors.append("No máximo 2 botões de URL.")
        if kinds.count("PHONE_NUMBER") > 1:
            errors.append("No máximo 1 botão de telefone.")
        # Quick replies must be grouped (all before or all after the CTAs)
        if "QUICK_REPLY" in kinds:
            idx = [i for i, k in enumerate(kinds) if k == "QUICK_REPLY"]
            if idx != list(range(idx[0], idx[0] + len(idx))):
                errors.append("Botões de resposta rápida devem ser agrupados em sequência.")
        for b in buttons:
            k, text = b.get("type", "").upper(), b.get("text", "")
            if k in ("QUICK_REPLY", "URL", "PHONE_NUMBER") and (not text or len(text) > 25):
                errors.append(f"Texto do botão '{text}' deve ter 1–25 caracteres.")
            if k == "URL":
                url = b.get("url", "")
                if not url.startswith("https://"):
                    errors.append("URL do botão deve começar com https://")
                if len(url) > 2000:
                    errors.append("URL do botão excede 2000 caracteres.")
                if variables_of(url) and not url.endswith("{{1}}"):
                    errors.append("Variável na URL só é permitida no final ({{1}}).")
            if k == "PHONE_NUMBER" and not re.fullmatch(r"\+?\d{8,15}", re.sub(r"[\s()-]", "", b.get("phone_number", ""))):
                errors.append("Telefone do botão inválido (use formato internacional).")
            if k not in ("QUICK_REPLY", "URL", "PHONE_NUMBER", "OTP", "COPY_CODE"):
                errors.append(f"Tipo de botão desconhecido: {k}")
    if category == "MARKETING" and not any(b.get("type", "").upper() == "QUICK_REPLY" and re.search(r"parar|sair|n[ãa]o", b.get("text", ""), re.I) for b in (btn_comp or {}).get("buttons", [])):
        warnings.append("Recomendado: incluir botão de opt-out (ex.: 'Parar de receber') em templates de Marketing.")
    return {"errors": errors, "warnings": warnings, "variables": sorted(set(variables_of(body["text"] if body else "")))}


def render_template_text(tpl: WaTemplate, values: dict[str, str]) -> str:
    """Final text shown in the chat (body with variables substituted)."""
    body = next((c for c in tpl.components if c.get("type", "").upper() == "BODY"), {"text": ""})
    text = body.get("text", "")
    header = next((c for c in tpl.components if c.get("type", "").upper() == "HEADER" and c.get("format", "TEXT").upper() == "TEXT"), None)
    out = VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)
    if header:
        out = "*" + VAR_RE.sub(lambda m: str(values.get(f"h{m.group(1)}", values.get(m.group(1), m.group(0)))), header.get("text", "")) + "*\n" + out
    return out


def render_template_components(tpl: WaTemplate, values: dict[str, str]) -> list[dict]:
    """Builds the send ``components`` (positional parameters) according to the Cloud API."""
    comps: list[dict] = []
    body = next((c for c in tpl.components if c.get("type", "").upper() == "BODY"), None)
    if body:
        nums = sorted(set(variables_of(body.get("text", ""))))
        missing = [n for n in nums if not str(values.get(str(n), "")).strip()]
        if missing:
            raise bad_request(f"Preencha as variáveis do template: {missing}", code="missing_variables")
        if nums:
            comps.append({"type": "body", "parameters": [{"type": "text", "text": str(values[str(n)])[:1024]} for n in nums]})
    header = next((c for c in tpl.components if c.get("type", "").upper() == "HEADER" and c.get("format", "TEXT").upper() == "TEXT"), None)
    if header and variables_of(header.get("text", "")):
        comps.append({"type": "header", "parameters": [{"type": "text", "text": str(values.get("h1", values.get("1", "")))}]})
    return comps


class TemplateIn(BaseModel):
    whatsapp_account_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=512)
    language: str = "pt_BR"
    category: str
    components: list[dict]
    sample_values: dict = {}
    submit: bool = False


FIELDS = ["id", "name", "language", "category", "components", "sample_values", "status", "meta_template_id", "rejection_reason", "submitted_at", "whatsapp_account_id", "created_at", "updated_at"]


async def _account(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID | None) -> WhatsAppAccount:
    q = sa.select(WhatsAppAccount).where(WhatsAppAccount.organization_id == org_id, WhatsAppAccount.status == "active")
    if account_id:
        q = q.where(WhatsAppAccount.id == account_id)
    wa = (await session.execute(q.limit(1))).scalar_one_or_none()
    if not wa:
        raise bad_request("Conecte um número do WhatsApp antes de gerenciar templates", code="no_whatsapp_account")
    return wa


async def submit_to_meta(session: AsyncSession, tpl: WaTemplate, wa: WhatsAppAccount) -> None:
    client = MetaClient(wa.access_token, wa.phone_number_id, wa.waba_id)
    try:
        res = await client.create_template(tpl.name, tpl.language, tpl.category, tpl.components)
    except MetaError as exc:
        tpl.status, tpl.rejection_reason = TemplateStatus.rejected, str(exc)[:500]
        raise AppError(422, "meta_rejected", f"A Meta recusou o template: {exc}")
    tpl.meta_template_id = str(res.get("id"))
    tpl.status = TemplateStatus.pending
    tpl.submitted_at = utcnow()
    emit(session, "template.submitted", tpl.organization_id, {"template_id": str(tpl.id), "name": tpl.name})


@router.post("/validate")
async def validate(body: TemplateIn, p: Principal = Depends(require("templates:read"))):
    return validate_template(body.name, body.language, body.category, body.components, body.sample_values)


@router.get("")
async def list_templates(status: str | None = None, p: Principal = Depends(require("templates:read")), session: AsyncSession = Depends(get_session)):
    q = sa.select(WaTemplate).where(WaTemplate.organization_id == p.org_id).order_by(WaTemplate.created_at.desc())
    if status:
        q = q.where(WaTemplate.status == status)
    return {"items": [row(t, FIELDS) for t in (await session.execute(q)).scalars().all()]}


@router.post("", status_code=201)
async def create_template(body: TemplateIn, p: Principal = Depends(require("templates:manage")), session: AsyncSession = Depends(get_session)):
    result = validate_template(body.name, body.language, body.category, body.components, body.sample_values)
    if result["errors"]:
        raise AppError(422, "template_invalid", "Template não atende às regras da Meta", result)
    wa = await _account(session, p.org_id, body.whatsapp_account_id)
    if (await session.execute(sa.select(WaTemplate.id).where(WaTemplate.whatsapp_account_id == wa.id, WaTemplate.name == body.name, WaTemplate.language == body.language))).first():
        raise conflict("Já existe template com este nome/idioma", "template_exists")
    tpl = WaTemplate(organization_id=p.org_id, whatsapp_account_id=wa.id, name=body.name, language=body.language, category=body.category, components=body.components,
                     sample_values=body.sample_values, status=TemplateStatus.draft, created_by=p.user_id)
    session.add(tpl)
    await session.flush()
    if body.submit:
        await submit_to_meta(session, tpl, wa)
    return {**row(tpl, FIELDS), "warnings": result["warnings"]}


@router.post("/{template_id}/submit")
async def submit(template_id: uuid.UUID, p: Principal = Depends(require("templates:manage")), session: AsyncSession = Depends(get_session)):
    tpl = await session.get(WaTemplate, template_id)
    if not tpl or tpl.organization_id != p.org_id:
        raise not_found("Template")
    if tpl.status not in (TemplateStatus.draft, TemplateStatus.rejected):
        raise bad_request("Somente rascunhos ou rejeitados podem ser (re)submetidos", code="invalid_status")
    result = validate_template(tpl.name, tpl.language, tpl.category, tpl.components, tpl.sample_values)
    if result["errors"]:
        raise AppError(422, "template_invalid", "Template não atende às regras da Meta", result)
    await submit_to_meta(session, tpl, await _account(session, p.org_id, tpl.whatsapp_account_id))
    return row(tpl, FIELDS)


@router.post("/sync")
async def sync_status(p: Principal = Depends(require("templates:manage")), session: AsyncSession = Depends(get_session)):
    """Imports/updates template statuses directly from Meta (live mode)."""
    wa = await _account(session, p.org_id, None)
    client = MetaClient(wa.access_token, wa.phone_number_id, wa.waba_id)
    remote = await client.list_templates()
    updated = created = 0
    mapping = {"APPROVED": TemplateStatus.approved, "REJECTED": TemplateStatus.rejected, "PENDING": TemplateStatus.pending, "PAUSED": TemplateStatus.paused, "DISABLED": TemplateStatus.disabled}
    for r in remote:
        tpl = (await session.execute(sa.select(WaTemplate).where(WaTemplate.whatsapp_account_id == wa.id, WaTemplate.name == r["name"], WaTemplate.language == r["language"]))).scalar_one_or_none()
        status = mapping.get(r.get("status", "").upper(), TemplateStatus.pending)
        if tpl is None:
            session.add(WaTemplate(organization_id=p.org_id, whatsapp_account_id=wa.id, name=r["name"], language=r["language"], category=r.get("category", "UTILITY"),
                                   components=r.get("components", []), status=status, meta_template_id=str(r.get("id"))))
            created += 1
        elif tpl.status != status:
            tpl.status, tpl.meta_template_id = status, str(r.get("id"))
            tpl.rejection_reason = r.get("rejected_reason") if status == TemplateStatus.rejected else None
            updated += 1
    return {"remote": len(remote), "created": created, "updated": updated, "mode": "live" if client.live else "sandbox"}


@router.post("/{template_id}/sandbox-approve")
async def sandbox_approve(template_id: uuid.UUID, approve: bool = True, p: Principal = Depends(require("templates:manage")), session: AsyncSession = Depends(get_session)):
    """Sandbox only: simulates Meta's decision (message_template_status_update webhook)."""
    from app.config import settings

    if settings.meta_mode != "sandbox":
        raise AppError(403, "not_sandbox", "Disponível apenas em modo sandbox")
    tpl = await session.get(WaTemplate, template_id)
    if not tpl or tpl.organization_id != p.org_id:
        raise not_found("Template")
    tpl.status = TemplateStatus.approved if approve else TemplateStatus.rejected
    tpl.rejection_reason = None if approve else "Rejeitado no sandbox (simulação)"
    emit(session, "template.status_changed", p.org_id, {"template_id": str(tpl.id), "name": tpl.name, "status": tpl.status.value})
    return row(tpl, FIELDS)


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: uuid.UUID, p: Principal = Depends(require("templates:manage")), session: AsyncSession = Depends(get_session)):
    tpl = await session.get(WaTemplate, template_id)
    if not tpl or tpl.organization_id != p.org_id:
        raise not_found("Template")
    if tpl.meta_template_id:
        wa = await _account(session, p.org_id, tpl.whatsapp_account_id)
        try:
            await MetaClient(wa.access_token, wa.phone_number_id, wa.waba_id).delete_template(tpl.name)
        except MetaError as exc:
            raise AppError(422, "meta_error", str(exc))
    await session.delete(tpl)
