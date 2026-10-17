"""Pipeline module (sales CRM): catalog, deals, items, history, activities, proposals."""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, enum_col, utcnow


class BillingModality(str, enum.Enum):
    hourly = "hourly"
    unit_volume = "unit_volume"
    recurring_subscription = "recurring_subscription"
    fixed_project = "fixed_project"


class ProductService(UUIDPk, Base):
    __tablename__ = "products_services"
    __table_args__ = (sa.UniqueConstraint("organization_id", "sku", name="uq_product_org_sku"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    module_category: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    billing_modality: Mapped[BillingModality] = mapped_column(enum_col(BillingModality, "billing_modality"))
    unit_label: Mapped[str | None] = mapped_column(sa.String(30))  # hour, exam, month, year
    base_unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DealStage(str, enum.Enum):
    lead_inbound = "lead_inbound"
    prospeccao = "prospeccao"
    qualificacao_tecnica = "qualificacao_tecnica"
    reuniao_alinhamento = "reuniao_alinhamento"
    proposta_apresentada = "proposta_apresentada"
    negociacao = "negociacao"
    fechado_ganho = "fechado_ganho"
    fechado_perdido = "fechado_perdido"


STAGE_ORDER = [s.value for s in DealStage]
STAGE_PROBABILITY = {
    "lead_inbound": 5, "prospeccao": 10, "qualificacao_tecnica": 25, "reuniao_alinhamento": 40,
    "proposta_apresentada": 60, "negociacao": 80, "fechado_ganho": 100, "fechado_perdido": 0,
}


class Deal(UUIDPk, Timestamps, Base):
    __tablename__ = "deals"
    __table_args__ = (sa.Index("idx_deals_org_stage", "organization_id", "stage"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    lead_contact_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("contacts.id", ondelete="SET NULL"))
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    stage: Mapped[DealStage] = mapped_column(enum_col(DealStage, "deal_stage"), default=DealStage.lead_inbound)
    source: Mapped[str | None] = mapped_column(sa.String(40))  # whatsapp|indicacao|evento|site|outbound
    position: Mapped[float] = mapped_column(sa.Float, default=0)  # kanban ordering
    probability: Mapped[int] = mapped_column(default=5)
    expected_value: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), default=0)  # sum of the items, without discount
    discount_percentage: Mapped[Decimal] = mapped_column(sa.Numeric(5, 2), default=0)
    discount_approved_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    final_contracted_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 2))
    expected_close_date: Mapped[date | None] = mapped_column(sa.Date)
    loss_reason: Mapped[str | None] = mapped_column(sa.Text)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(sa.Text)

    account: Mapped["Account"] = relationship(lazy="joined")  # noqa: F821
    items: Mapped[list["DealItem"]] = relationship(back_populates="deal", cascade="all, delete-orphan", lazy="selectin")


class DealItem(UUIDPk, Base):
    __tablename__ = "deal_items"

    deal_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    product_service_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("products_services.id"))
    description: Mapped[str | None] = mapped_column(sa.String(250))
    quantity: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), default=1)
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2))
    subtotal: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2))

    deal: Mapped[Deal] = relationship(back_populates="items")
    product: Mapped[ProductService] = relationship(lazy="joined")


class DealStageHistory(UUIDPk, Base):
    __tablename__ = "deal_stage_history"

    deal_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    from_stage: Mapped[str | None] = mapped_column(sa.String(40))
    to_stage: Mapped[str] = mapped_column(sa.String(40))
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CrmDealTicket(UUIDPk, Base):
    __tablename__ = "crm_deal_tickets"
    __table_args__ = (sa.UniqueConstraint("deal_id", "ticket_id", name="uq_deal_ticket"),)

    deal_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    linked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Activity(UUIDPk, Base):
    """Unified timeline of the account/contact/deal (WhatsApp, notes, meetings, stage changes)."""

    __tablename__ = "activities"
    __table_args__ = (
        sa.Index("idx_activities_account", "account_id", "created_at"),
        sa.Index("idx_activities_contact", "contact_id", "created_at"),
        sa.Index("idx_activities_deal", "deal_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("accounts.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("contacts.id", ondelete="CASCADE"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("deals.id", ondelete="CASCADE"))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    case_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    kind: Mapped[str] = mapped_column(sa.String(40))  # whatsapp_ticket|whatsapp_message|note|meeting|call|stage_change|deal_won|case|...
    title: Mapped[str] = mapped_column(sa.String(250))
    body: Mapped[str | None] = mapped_column(sa.Text)
    data: Mapped[dict | None] = mapped_column(JSONType)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # tarefas/lembretes
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Proposal(UUIDPk, Timestamps, Base):
    __tablename__ = "proposals"
    __table_args__ = (sa.UniqueConstraint("organization_id", "number", name="uq_proposal_number"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"))
    deal_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(sa.String(30))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(sa.String(20), default="draft")  # draft|sent|accepted|rejected|expired
    valid_until: Mapped[date | None] = mapped_column(sa.Date)
    payment_terms: Mapped[str | None] = mapped_column(sa.Text)
    scope_text: Mapped[str | None] = mapped_column(sa.Text)
    clauses: Mapped[list] = mapped_column(JSONType, default=list)
    snapshot: Mapped[dict] = mapped_column(JSONType, default=dict)  # itens/valores congelados no envio
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
