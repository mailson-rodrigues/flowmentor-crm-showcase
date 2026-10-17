"""LGPD (consents, data-subject requests) and immutable (append-only) audit trail."""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, JSONType, UTCDateTime, UUIDPk, utcnow


class LgpdConsent(UUIDPk, Base):
    __tablename__ = "lgpd_consents"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    legal_basis: Mapped[str] = mapped_column(sa.String(80))
    purpose: Mapped[str] = mapped_column(sa.Text)
    opt_in_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ip_address: Mapped[str | None] = mapped_column(sa.String(45))
    consent_source: Mapped[str] = mapped_column(sa.String(50))  # whatsapp_optin|formulario_web|contrato_b2b


class DataSubjectRequest(UUIDPk, Base):
    """Data-subject rights — LGPD arts. 18 and 19."""

    __tablename__ = "data_subject_requests"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    request_type: Mapped[str] = mapped_column(sa.String(30))  # confirmation|access|rectification|anonymization|deletion|revoke_consent|portability|info_sharing
    status: Mapped[str] = mapped_column(sa.String(20), default="open")  # open|in_progress|completed|rejected
    requester_name: Mapped[str | None] = mapped_column(sa.String(150))
    requester_contact: Mapped[str | None] = mapped_column(sa.String(150))
    details: Mapped[str | None] = mapped_column(sa.Text)
    result: Mapped[dict | None] = mapped_column(JSONType)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # prazo legal (15 dias — art. 19, II)
    handled_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditLog(UUIDPk, Base):
    """Append-only. PostgreSQL triggers block UPDATE/DELETE; the hash chain detects tampering."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("idx_audit_resource", "resource_type", "resource_id"),
        sa.Index("idx_audit_created", "created_at"),
        sa.UniqueConstraint("organization_id", "seq", name="uq_audit_org_seq"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)  # per-organization sequence
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    action: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    ip_address: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.Text)
    old_state: Mapped[dict | None] = mapped_column(JSONType)
    new_state: Mapped[dict | None] = mapped_column(JSONType)
    prev_hash: Mapped[str | None] = mapped_column(sa.String(64))
    hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
