"""Core / Cadastros: organizações (tenants), usuários, departamentos, contatos, contas institucionais."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, Timestamps, UTCDateTime, UUIDPk, enum_col, utcnow
from app.security.crypto import EncryptedString


class Organization(UUIDPk, Timestamps, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(100), unique=True, nullable=False)
    # business_hours, survey, routing, bot, branding...
    settings: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)


class UserDepartment(Base):
    __tablename__ = "user_departments"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    department_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True)


class User(UUIDPk, Base):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(150), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(sa.String(255))
    role: Mapped[str] = mapped_column(sa.String(30), default="agent", nullable=False)  # admin|supervisor|consultor|agent|auditor|bot
    status: Mapped[str] = mapped_column(sa.String(20), default="offline", nullable=False)  # available|busy|offline|paused
    is_active: Mapped[bool] = mapped_column(default=True)
    skills: Mapped[list] = mapped_column(JSONType, default=list)
    max_concurrent_chats: Mapped[int] = mapped_column(default=5)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    departments: Mapped[list["Department"]] = relationship(secondary="user_departments", lazy="selectin")

    @property
    def department_ids(self) -> list[uuid.UUID]:
        return [d.id for d in self.departments]


class RefreshToken(UUIDPk, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    user_agent: Mapped[str | None] = mapped_column(sa.String(300))
    ip_address: Mapped[str | None] = mapped_column(sa.String(45))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApiKey(UUIDPk, Base):
    """Chaves da Open API (integrações machine-to-machine)."""

    __tablename__ = "api_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(100))
    prefix: Mapped[str] = mapped_column(sa.String(12), index=True)
    key_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Department(UUIDPk, Base):
    __tablename__ = "departments"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    sla_first_response_seconds: Mapped[int] = mapped_column(default=300)
    sla_solution_seconds: Mapped[int] = mapped_column(default=3600)
    routing_strategy: Mapped[str] = mapped_column(sa.String(20), default="least_busy")  # round_robin|least_busy|skill_based
    required_skills: Mapped[list] = mapped_column(JSONType, default=list)
    business_hours: Mapped[dict | None] = mapped_column(JSONType)  # None = herda da organização
    after_hours_message: Mapped[str | None] = mapped_column(sa.Text)
    is_default: Mapped[bool] = mapped_column(default=False)
    rr_last_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Contact(UUIDPk, Base):
    """Pessoa física que fala pelo WhatsApp (prescritor, médico, responsável técnico, lead)."""

    __tablename__ = "contacts"
    __table_args__ = (sa.UniqueConstraint("organization_id", "wa_id", name="uq_contact_org_wa"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    wa_id: Mapped[str] = mapped_column(sa.String(50), nullable=False)  # E.164 sem '+'
    name: Mapped[str | None] = mapped_column(sa.String(150))
    email: Mapped[str | None] = mapped_column(sa.String(150))
    job_title: Mapped[str | None] = mapped_column(sa.String(100))
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    source: Mapped[str | None] = mapped_column(sa.String(40))  # whatsapp|manual|import|api|web_form
    opted_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_interaction_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AccountType(str, enum.Enum):
    hospital = "hospital"
    laboratorio = "laboratorio"
    clinica = "clinica"
    universidade = "universidade"
    outro = "outro"


class Account(UUIDPk, Timestamps, Base):
    """Conta institucional B2B de saúde (hospitais, laboratórios, clínicas...)."""

    __tablename__ = "accounts"

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    corporate_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    trade_name: Mapped[str | None] = mapped_column(sa.String(150))
    tax_id: Mapped[str | None] = mapped_column(EncryptedString)  # CNPJ cifrado (envelope encryption)
    tax_id_hash: Mapped[str | None] = mapped_column(sa.String(64), index=True)  # blind index p/ busca/duplicidade
    account_type: Mapped[AccountType] = mapped_column(enum_col(AccountType, "account_type"), default=AccountType.laboratorio)
    website: Mapped[str | None] = mapped_column(sa.String(150))
    status: Mapped[str] = mapped_column(sa.String(20), default="prospect")  # prospect|customer|inactive
    city: Mapped[str | None] = mapped_column(sa.String(80))
    state: Mapped[str | None] = mapped_column(sa.String(2))
    segment: Mapped[str | None] = mapped_column(sa.String(80))
    institutional_discount_pct: Mapped[float] = mapped_column(sa.Numeric(5, 2), default=0)  # desconto institucional padrão
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[str | None] = mapped_column(sa.Text)

    contacts: Mapped[list["AccountContact"]] = relationship(back_populates="account", cascade="all, delete-orphan", lazy="selectin")


class AccountContact(UUIDPk, Base):
    __tablename__ = "account_contacts"
    __table_args__ = (sa.UniqueConstraint("account_id", "contact_id", name="uq_account_contact"),)

    account_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    role_description: Mapped[str | None] = mapped_column(sa.String(100))
    council_registration: Mapped[str | None] = mapped_column(sa.String(50))  # CRM / CRBM
    is_primary: Mapped[bool] = mapped_column(default=False)

    account: Mapped[Account] = relationship(back_populates="contacts")
    contact: Mapped[Contact] = relationship(lazy="joined")
