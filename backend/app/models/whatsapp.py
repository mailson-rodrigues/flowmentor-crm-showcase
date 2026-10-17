"""Módulo BSP / WhatsApp: contas WABA, templates, tickets, mensagens, eventos, notas, pesquisas, presença."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, enum_col, utcnow
from app.security.crypto import EncryptedString


class WhatsAppAccount(UUIDPk, Timestamps, Base):
    """Número/WABA conectado via Embedded Signup. Tokens cifrados com envelope encryption."""

    __tablename__ = "whatsapp_accounts"
    __table_args__ = (sa.UniqueConstraint("phone_number_id", name="uq_wa_phone_number_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(sa.String(100))
    waba_id: Mapped[str] = mapped_column(sa.String(40))
    phone_number_id: Mapped[str] = mapped_column(sa.String(40))
    display_phone_number: Mapped[str | None] = mapped_column(sa.String(30))
    verified_name: Mapped[str | None] = mapped_column(sa.String(150))
    access_token: Mapped[str | None] = mapped_column(EncryptedString)  # System User Access Token
    token_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    token_rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(sa.String(20), default="active")  # active|disconnected|error
    quality_rating: Mapped[str | None] = mapped_column(sa.String(20))
    messaging_limit: Mapped[str | None] = mapped_column(sa.String(30))
    default_department_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("departments.id", ondelete="SET NULL"))
    bot_enabled: Mapped[bool] = mapped_column(default=True)
    payment_method_attached: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class TemplateStatus(str, enum.Enum):
    draft = "draft"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    paused = "paused"
    disabled = "disabled"


class WaTemplate(UUIDPk, Timestamps, Base):
    """Template HSM (Marketing / Utility / Authentication)."""

    __tablename__ = "wa_templates"
    __table_args__ = (sa.UniqueConstraint("whatsapp_account_id", "name", "language", name="uq_template_name_lang"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    whatsapp_account_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(sa.String(512))
    language: Mapped[str] = mapped_column(sa.String(10), default="pt_BR")
    category: Mapped[str] = mapped_column(sa.String(20))  # MARKETING|UTILITY|AUTHENTICATION
    components: Mapped[list] = mapped_column(JSONType, default=list)  # HEADER/BODY/FOOTER/BUTTONS
    sample_values: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[TemplateStatus] = mapped_column(enum_col(TemplateStatus, "template_status"), default=TemplateStatus.draft)
    meta_template_id: Mapped[str | None] = mapped_column(sa.String(40))
    rejection_reason: Mapped[str | None] = mapped_column(sa.Text)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))


class TicketStatus(str, enum.Enum):
    pending_bot = "pending_bot"
    queued = "queued"
    in_progress = "in_progress"
    pending_client = "pending_client"
    resolved = "resolved"
    closed = "closed"


OPEN_STATUSES = (TicketStatus.pending_bot, TicketStatus.queued, TicketStatus.in_progress, TicketStatus.pending_client)


class Ticket(UUIDPk, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        sa.Index("idx_tickets_org_status", "organization_id", "status"),
        sa.Index("idx_tickets_agent", "assigned_agent_id"),
        sa.Index("idx_tickets_created_at", "created_at"),
        sa.Index("idx_tickets_contact", "contact_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("contacts.id", ondelete="CASCADE"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("departments.id", ondelete="SET NULL"))
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    whatsapp_account_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("whatsapp_accounts.id", ondelete="SET NULL"))
    status: Mapped[TicketStatus] = mapped_column(enum_col(TicketStatus, "ticket_status"), default=TicketStatus.queued)
    priority: Mapped[str] = mapped_column(sa.String(10), default="normal")  # low|normal|high|urgent
    subject: Mapped[str | None] = mapped_column(sa.String(200))
    summary: Mapped[str | None] = mapped_column(sa.Text)  # resumo gerado por LLM
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    required_skills: Mapped[list] = mapped_column(JSONType, default=list)
    bot_active: Mapped[bool] = mapped_column(default=False)
    bot_context: Mapped[dict] = mapped_column(JSONType, default=dict)
    survey_state: Mapped[str | None] = mapped_column(sa.String(20))
    last_message_preview: Mapped[str | None] = mapped_column(sa.String(200))
    after_hours: Mapped[bool] = mapped_column(default=False)
    unread_count: Mapped[int] = mapped_column(default=0)
    reopen_count: Mapped[int] = mapped_column(default=0)
    reopened_from_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)

    # Timestamps operacionais críticos
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_response_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_human_outbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_first_response_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_solution_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_flags: Mapped[dict] = mapped_column(JSONType, default=dict)  # {frt_at_risk, frt_breached, sol_at_risk, sol_breached}

    # Snapshots pré-calculados (segundos)
    first_response_time_seconds: Mapped[int | None] = mapped_column()
    handling_time_seconds: Mapped[int | None] = mapped_column()
    wait_time_seconds: Mapped[int | None] = mapped_column()  # tempo em fila
    is_fcr: Mapped[bool] = mapped_column(default=False)

    contact: Mapped["Contact"] = relationship(lazy="joined")  # noqa: F821


class MessageDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class MessageType(str, enum.Enum):
    text = "text"
    image = "image"
    audio = "audio"
    document = "document"
    video = "video"
    interactive = "interactive"
    template = "template"
    location = "location"
    system = "system"


class MessageStatus(str, enum.Enum):
    received = "received"
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    read = "read"
    failed = "failed"


class Message(UUIDPk, Base):
    """Particionada por mês (created_at) em PostgreSQL via migração; PK física = (id, created_at)."""

    __tablename__ = "messages"
    __table_args__ = (
        sa.Index("idx_messages_ticket_id", "ticket_id", "created_at"),
        sa.Index("idx_messages_meta_id", "meta_message_id"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)  # FK lógica (tabela particionada)
    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    meta_message_id: Mapped[str | None] = mapped_column(sa.String(100))  # wamid
    direction: Mapped[MessageDirection] = mapped_column(enum_col(MessageDirection, "message_direction"))
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    is_bot: Mapped[bool] = mapped_column(default=False)
    type: Mapped[MessageType] = mapped_column(enum_col(MessageType, "message_type"), default=MessageType.text)
    body: Mapped[str | None] = mapped_column(sa.Text)
    media_asset_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    media_url: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[MessageStatus] = mapped_column(enum_col(MessageStatus, "message_status"), default=MessageStatus.received)
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class MediaAsset(UUIDPk, Base):
    """Metadados de mídia; o binário vive em S3/GCS/FS (nunca no banco relacional)."""

    __tablename__ = "media_assets"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    storage_key: Mapped[str] = mapped_column(sa.String(300), unique=True)
    filename: Mapped[str | None] = mapped_column(sa.String(200))
    mime_type: Mapped[str | None] = mapped_column(sa.String(100))
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    meta_media_id: Mapped[str | None] = mapped_column(sa.String(60))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TicketEvent(UUIDPk, Base):
    """Histórico cronológico (event sourcing simplificado) para SLA e auditoria operacional."""

    __tablename__ = "ticket_events"
    __table_args__ = (sa.Index("idx_ticket_events_ticket", "ticket_id", "created_at"),)

    ticket_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tickets.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(sa.String(50))  # status_change|agent_assigned|transferred|sla_breached|...
    previous_value: Mapped[str | None] = mapped_column(sa.String(100))
    new_value: Mapped[str | None] = mapped_column(sa.String(100))
    meta: Mapped[dict | None] = mapped_column("metadata", JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TicketNote(UUIDPk, Base):
    """Notas internas (invisíveis ao cliente) e sussurros (whisper) de supervisores."""

    __tablename__ = "ticket_notes"

    ticket_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    author_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(sa.String(10), default="note")  # note|whisper
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)  # whisper: só este usuário enxerga
    body: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SatisfactionSurvey(UUIDPk, Base):
    __tablename__ = "satisfaction_surveys"
    __table_args__ = (
        sa.UniqueConstraint("ticket_id", name="uq_survey_ticket"),
        sa.Index("idx_surveys_agent_rating", "agent_id", "rating"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tickets.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("contacts.id", ondelete="CASCADE"))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    survey_type: Mapped[str] = mapped_column(sa.String(10), default="csat")  # csat|nps|ces
    state: Mapped[str] = mapped_column(sa.String(20), default="awaiting_rating")  # awaiting_rating|awaiting_nps_value|awaiting_comment|completed|expired
    rating: Mapped[int | None] = mapped_column(sa.SmallInteger)
    comment: Mapped[str | None] = mapped_column(sa.Text)
    sentiment: Mapped[str | None] = mapped_column(sa.String(20))
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AgentPresenceLog(UUIDPk, Base):
    """Linha do tempo de status do agente → ocupação (logado × conversando × pausa)."""

    __tablename__ = "agent_presence_log"
    __table_args__ = (sa.Index("idx_presence_user_time", "user_id", "started_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(sa.String(20))
    reason: Mapped[str | None] = mapped_column(sa.String(60))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
