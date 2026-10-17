"""Public Open API (authenticated by X-API-Key): lead capture from web forms/landing pages (e.g. WordPress/PHP)."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.deps import Principal, require
from app.core.errors import bad_request
from app.core.events import emit
from app.db import get_session
from app.models import Account, AccountContact, Activity, Deal, DealStage, DealStageHistory, LgpdConsent
from app.modules.core.router import get_or_create_contact, normalize_wa_id
from app.security.sanitize import sanitize_text

router = APIRouter(prefix="/api/v1/public", tags=["Open API"])


class LeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    phone: str
    email: EmailStr | None = None
    company: str | None = Field(None, max_length=200)
    interest: str | None = Field(None, max_length=200)
    message: str | None = Field(None, max_length=3000)
    source: str = Field("site", max_length=40)
    consent: bool = Field(description="O titular deve aceitar o tratamento de dados (LGPD, art. 7º, I)")


@router.post("/leads", status_code=201)
async def capture_lead(body: LeadIn, request: Request, p: Principal = Depends(require("contacts:write", "deals:write")), session: AsyncSession = Depends(get_session)):
    if not body.consent:
        raise bad_request("É necessário o consentimento do titular", code="consent_required")
    wa_id = normalize_wa_id(body.phone)
    contact, created = await get_or_create_contact(session, p.org_id, wa_id, body.name, source="web_form")
    if body.email:
        contact.email = body.email
    session.add(LgpdConsent(organization_id=p.org_id, contact_id=contact.id, legal_basis="Consentimento (Art. 7, I)", consent_source="formulario_web", ip_address=audit.client_ip(request),
                            purpose="Contato comercial e envio de propostas sobre serviços de citometria e consultoria"))
    link = (await session.execute(sa.select(AccountContact).where(AccountContact.contact_id == contact.id).limit(1))).scalar_one_or_none()
    if link:
        account = await session.get(Account, link.account_id)
    else:
        account = Account(organization_id=p.org_id, corporate_name=body.company or f"Lead — {body.name}", status="prospect", segment="Lead Web", tags=["lead-web"])
        session.add(account)
        await session.flush()
        session.add(AccountContact(account_id=account.id, contact_id=contact.id, is_primary=True, role_description="Contato do formulário"))
    deal = Deal(organization_id=p.org_id, account_id=account.id, lead_contact_id=contact.id, title=f"Lead {body.source} — {body.interest or body.name}"[:200], stage=DealStage.lead_inbound,
                source=body.source, probability=5, notes=sanitize_text(body.message, 3000))
    session.add(deal)
    await session.flush()
    session.add(DealStageHistory(deal_id=deal.id, from_stage=None, to_stage="lead_inbound"))
    session.add(Activity(organization_id=p.org_id, account_id=account.id, contact_id=contact.id, deal_id=deal.id, kind="lead_form", title="Lead recebido pelo formulário",
                         body=sanitize_text(body.message, 3000)))
    emit(session, "deal.created", p.org_id, {"deal_id": str(deal.id), "account_id": str(account.id), "title": deal.title, "source": body.source})
    return {"deal_id": str(deal.id), "account_id": str(account.id), "contact_id": str(contact.id)}
