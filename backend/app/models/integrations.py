"""Webhooks outbound (Open API) e fila de jobs agendados persistente."""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, utcnow
from app.security.crypto import EncryptedString


class WebhookEndpoint(UUIDPk, Timestamps, Base):
    __tablename__ = "webhook_endpoints"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(100))
    url: Mapped[str] = mapped_column(sa.String(500))
    secret: Mapped[str] = mapped_column(EncryptedString)  # assina o payload (HMAC-SHA256)
    events: Mapped[list] = mapped_column(JSONType, default=list)  # ["deal.won", "case.created", "*"]
    is_active: Mapped[bool] = mapped_column(default=True)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    disabled_reason: Mapped[str | None] = mapped_column(sa.String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)


class WebhookDelivery(UUIDPk, Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (sa.Index("idx_wh_deliv_status", "status", "next_attempt_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, index=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(sa.String(40))
    event_type: Mapped[str] = mapped_column(sa.String(60))
    payload: Mapped[dict] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(sa.String(15), default="pending")  # pending|success|failed|dead
    attempts: Mapped[int] = mapped_column(default=0)
    response_status: Mapped[int | None] = mapped_column()
    response_body: Mapped[str | None] = mapped_column(sa.Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ScheduledJob(UUIDPk, Base):
    """Jobs duráveis (ex.: disparo de pesquisa após janela de carência, renovação de token)."""

    __tablename__ = "scheduled_jobs"
    __table_args__ = (sa.Index("idx_jobs_due", "status", "run_at"),)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    job_type: Mapped[str] = mapped_column(sa.String(60))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(120), unique=True)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(sa.String(12), default="pending")  # pending|running|done|failed|cancelled
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
