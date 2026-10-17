"""WhatsApp ↔ CRM bridge: links the number to the Account/Lead, feeds the unified timeline and closes the sales loop."""
from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.core.events import Event, EventBus, emit
from app.db import session_scope
from app.models import Account, AccountContact, Activity, Contact, CrmDealTicket, Deal, DealStage, DealStageHistory, SatisfactionSurvey, Ticket
from app.models.crm import STAGE_PROBABILITY
from app.modules.whatsapp import service as svc
from app.security.masking import mask_phone

OPEN_STAGES = [s for s in DealStage if not s.value.startswith("fechado")]


async def on_ticket_created(event: Event) -> None:
    org_id, tid = uuid.UUID(event.org_id), uuid.UUID(event.payload["ticket_id"])
    async with session_scope() as s:
        t = await s.get(Ticket, tid)
        if t is None:
            return
        await s.refresh(t, ["contact"])
        c: Contact = t.contact
        links = (await s.execute(sa.select(AccountContact).where(AccountContact.contact_id == c.id))).scalars().all()
        cfg = await svc.org_cfg(s, org_id)
        account: Account | None = None
        if links:
            primary = next((l for l in links if l.is_primary), links[0])
            account = await s.get(Account, primary.account_id)
        elif cfg.get("crm", {}).get("auto_create_leads", True):
            # unknown number → creates a Lead (prospect account + deal in lead_inbound)
            label = c.name or f"Contato {mask_phone(c.wa_id)}"
            account = Account(organization_id=org_id, corporate_name=f"Lead — {label}", status="prospect", segment="Lead WhatsApp", tags=["lead-whatsapp"])
            s.add(account)
            await s.flush()
            s.add(AccountContact(account_id=account.id, contact_id=c.id, role_description="Contato inicial (WhatsApp)", is_primary=True))
            deal = Deal(organization_id=org_id, account_id=account.id, lead_contact_id=c.id, title=f"Lead WhatsApp — {label}", stage=DealStage.lead_inbound, source="whatsapp",
                        probability=STAGE_PROBABILITY["lead_inbound"])
            s.add(deal)
            await s.flush()
            s.add(DealStageHistory(deal_id=deal.id, from_stage=None, to_stage="lead_inbound"))
            emit(s, "deal.created", org_id, {"deal_id": str(deal.id), "account_id": str(account.id), "title": deal.title, "source": "whatsapp"})
            emit(s, "account.created", org_id, {"account_id": str(account.id), "name": account.corporate_name, "source": "whatsapp"})
        if account is None:
            return
        deal = (await s.execute(sa.select(Deal).where(Deal.account_id == account.id, Deal.stage.in_(OPEN_STAGES)).order_by(Deal.updated_at.desc()).limit(1))).scalar_one_or_none()
        if deal and not (await s.execute(sa.select(CrmDealTicket.id).where(CrmDealTicket.deal_id == deal.id, CrmDealTicket.ticket_id == tid))).first():
            s.add(CrmDealTicket(deal_id=deal.id, ticket_id=tid))
        s.add(Activity(organization_id=org_id, account_id=account.id, contact_id=c.id, deal_id=deal.id if deal else None, ticket_id=tid, kind="whatsapp_ticket",
                       title="Atendimento WhatsApp iniciado", body=None, data={"ticket_id": str(tid)}))


async def on_ticket_closed(event: Event) -> None:
    """LLM summary on closing + entry in the account timeline."""
    from app.modules.whatsapp import llm

    org_id, tid = uuid.UUID(event.org_id), uuid.UUID(event.payload["ticket_id"])
    async with session_scope() as s:
        t = await s.get(Ticket, tid)
        if t is None:
            return
        await s.refresh(t, ["contact"])
        t.summary = await llm.summarize_conversation(await svc.load_transcript(s, tid))
        link = (await s.execute(sa.select(AccountContact.account_id).where(AccountContact.contact_id == t.contact_id).limit(1))).scalar_one_or_none()
        deal_id = (await s.execute(sa.select(CrmDealTicket.deal_id).where(CrmDealTicket.ticket_id == tid).limit(1))).scalar_one_or_none()
        s.add(Activity(organization_id=org_id, account_id=link, contact_id=t.contact_id, deal_id=deal_id, ticket_id=tid, kind="whatsapp_closed", title="Atendimento WhatsApp encerrado",
                       body=t.summary, data={"handling_seconds": t.handling_time_seconds, "frt_seconds": t.first_response_time_seconds, "fcr": t.is_fcr}))
        emit(s, "ticket.summary_ready", org_id, {"ticket_id": str(tid), "summary": t.summary, "agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None,
                                                  "department_id": str(t.department_id) if t.department_id else None})


async def on_survey_answered(event: Event) -> None:
    org_id, sid = uuid.UUID(event.org_id), uuid.UUID(event.payload["survey_id"])
    async with session_scope() as s:
        sv = await s.get(SatisfactionSurvey, sid)
        if sv is None:
            return
        link = (await s.execute(sa.select(AccountContact.account_id).where(AccountContact.contact_id == sv.contact_id).limit(1))).scalar_one_or_none()
        s.add(Activity(organization_id=org_id, account_id=link, contact_id=sv.contact_id, ticket_id=sv.ticket_id, kind="survey", title=f"Avaliação {sv.survey_type.upper()}: {sv.rating}",
                       data={"rating": sv.rating, "type": sv.survey_type}))


async def on_contact_linked(event: Event) -> None:
    """Contact manually linked to an account → re-associates the automatic lead (avoids duplicates)."""
    ticket_id = event.payload.get("ticket_id")
    if not ticket_id:
        return
    async with session_scope() as s:
        t = await s.get(Ticket, uuid.UUID(ticket_id))
        if t is None:
            return
        acc_id = uuid.UUID(event.payload["account_id"])
        s.add(Activity(organization_id=t.organization_id, account_id=acc_id, contact_id=t.contact_id, ticket_id=t.id, kind="whatsapp_ticket", title="Atendimento WhatsApp associado à conta"))


def register(bus: EventBus) -> None:
    bus.subscribe("ticket.created", on_ticket_created, "bridge.ticket_created")
    bus.subscribe("ticket.closed", on_ticket_closed, "bridge.ticket_closed")
    bus.subscribe("survey.answered", on_survey_answered, "bridge.survey_answered")
    bus.subscribe("contact.linked", on_contact_linked, "bridge.contact_linked")
