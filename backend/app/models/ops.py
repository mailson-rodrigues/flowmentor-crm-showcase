"""Módulo Operacional (Projetos/Casos) e Contratos & Faturamento."""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, enum_col, utcnow


class Contract(UUIDPk, Timestamps, Base):
    __tablename__ = "contracts"
    __table_args__ = (sa.UniqueConstraint("organization_id", "number", name="uq_contract_number"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    deal_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("deals.id", ondelete="SET NULL"))
    number: Mapped[str] = mapped_column(sa.String(30))
    title: Mapped[str] = mapped_column(sa.String(200))
    status: Mapped[str] = mapped_column(sa.String(20), default="active")  # draft|active|suspended|ended|cancelled
    modality: Mapped[str] = mapped_column(sa.String(30), default="fixed_project")  # billing_modality
    start_date: Mapped[date] = mapped_column(sa.Date)
    end_date: Mapped[date | None] = mapped_column(sa.Date)
    total_value: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), default=0)
    recurring_amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 2))
    billing_day: Mapped[int] = mapped_column(default=5)
    billing_period: Mapped[str] = mapped_column(sa.String(10), default="monthly")  # monthly|yearly|once
    next_billing_date: Mapped[date | None] = mapped_column(sa.Date)
    hours_contracted: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), default=0)  # banco de horas
    hourly_rate: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), default=300)  # hora técnica excedente/avulsa
    exams_contracted: Mapped[int] = mapped_column(default=0)  # volume de exames
    auto_renew: Mapped[bool] = mapped_column(default=False)
    terms: Mapped[str | None] = mapped_column(sa.Text)

    account: Mapped["Account"] = relationship(lazy="joined")  # noqa: F821


class Invoice(UUIDPk, Timestamps, Base):
    __tablename__ = "invoices"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("contracts.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(sa.String(30))
    description: Mapped[str | None] = mapped_column(sa.String(250))
    reference_period: Mapped[str | None] = mapped_column(sa.String(20))  # 2026-10
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2))
    due_date: Mapped[date] = mapped_column(sa.Date)
    status: Mapped[str] = mapped_column(sa.String(20), default="open")  # open|paid|overdue|cancelled
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payment_method: Mapped[str | None] = mapped_column(sa.String(20))  # pix|boleto|transfer
    pix_payload: Mapped[str | None] = mapped_column(sa.Text)  # copia-e-cola PIX (gateway adapter)
    gateway_ref: Mapped[str | None] = mapped_column(sa.String(80))
    nfse_number: Mapped[str | None] = mapped_column(sa.String(40))
    nfse_status: Mapped[str | None] = mapped_column(sa.String(20))  # pending|issued|error

    account: Mapped["Account"] = relationship(lazy="joined")  # noqa: F821


class CaseType(str, enum.Enum):
    consultoria = "consultoria"          # Consultoria técnica de processos e qualidade
    apoio_diagnostico = "apoio_diagnostico"  # Apoio diagnóstico remoto & laudos
    canal_medico = "canal_medico"        # Canal médico & discussão multidisciplinar
    capacitacao = "capacitacao"          # Capacitação técnica & treinamentos


class CaseStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    waiting_client = "waiting_client"
    review = "review"
    done = "done"
    cancelled = "cancelled"


class Case(UUIDPk, Timestamps, Base):
    __tablename__ = "cases"
    __table_args__ = (
        sa.UniqueConstraint("organization_id", "number", name="uq_case_number"),
        sa.Index("idx_cases_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("contracts.id", ondelete="SET NULL"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("deals.id", ondelete="SET NULL"))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    number: Mapped[str] = mapped_column(sa.String(30))
    title: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str | None] = mapped_column(sa.Text)
    case_type: Mapped[CaseType] = mapped_column(enum_col(CaseType, "case_type"), default=CaseType.consultoria)
    status: Mapped[CaseStatus] = mapped_column(enum_col(CaseStatus, "case_status"), default=CaseStatus.open)
    priority: Mapped[str] = mapped_column(sa.String(10), default="normal")  # low|normal|high|urgent
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    hours_contracted: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), default=0)
    hours_consumed: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), default=0)  # denormalizado dos lançamentos
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_breached_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tags: Mapped[list] = mapped_column(JSONType, default=list)

    account: Mapped["Account"] = relationship(lazy="joined")  # noqa: F821


class CaseTimeEntry(UUIDPk, Base):
    """Horas técnicas consumidas (base para cobrança por hora e controle de banco de horas)."""

    __tablename__ = "case_time_entries"

    case_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    work_date: Mapped[date] = mapped_column(sa.Date)
    hours: Mapped[Decimal] = mapped_column(sa.Numeric(6, 2))
    description: Mapped[str | None] = mapped_column(sa.String(300))
    billable: Mapped[bool] = mapped_column(default=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)  # preenchido ao faturar por consumo
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseReport(UUIDPk, Timestamps, Base):
    """Laudos / entregas técnicas (processamento de .FCS e emissão de laudo de alta complexidade)."""

    __tablename__ = "case_reports"
    __table_args__ = (sa.Index("idx_reports_org_status", "organization_id", "status"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    exam_code: Mapped[str] = mapped_column(sa.String(60))  # identificador externo (LIS)
    panel: Mapped[str | None] = mapped_column(sa.String(120))  # painel imunofenotípico
    urgency: Mapped[str] = mapped_column(sa.String(10), default="routine")  # routine|urgent
    status: Mapped[str] = mapped_column(sa.String(20), default="received")  # received|processing|in_review|issued|rejected
    fcs_media_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    analyst_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_breached_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    conclusion: Mapped[str | None] = mapped_column(sa.Text)
