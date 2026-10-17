"""Schema inicial: tabelas do CRM + WhatsApp BSP, mensagens particionadas por mês e auditoria append-only.

Revision ID: 0001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa

import app.models  # noqa: F401
from app.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

MESSAGE_ENUMS = {
    "message_direction": ("inbound", "outbound"),
    "message_type": ("text", "image", "audio", "video", "document", "interactive", "template", "location", "system"),
    "message_status": ("received", "queued", "sent", "delivered", "read", "failed"),
}

# Cada item é UM comando (drivers assíncronos não aceitam múltiplos comandos por chamada).
PG_STATEMENTS = [
    # --- Mensagens: particionamento por faixa mensal (created_at); PK física = (id, created_at)
    """
    CREATE TABLE messages (
        id               UUID        NOT NULL,
        ticket_id        UUID        NOT NULL,
        organization_id  UUID        NOT NULL,
        meta_message_id  VARCHAR(100),
        direction        message_direction NOT NULL,
        sender_user_id   UUID,
        is_bot           BOOLEAN     NOT NULL DEFAULT FALSE,
        type             message_type NOT NULL DEFAULT 'text',
        body             TEXT,
        media_asset_id   UUID,
        media_url        TEXT,
        payload          JSONB,
        status           message_status NOT NULL DEFAULT 'received',
        error            TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        delivered_at     TIMESTAMPTZ,
        read_at          TIMESTAMPTZ,
        PRIMARY KEY (id, created_at)
    ) PARTITION BY RANGE (created_at)
    """,
    "CREATE TABLE messages_default PARTITION OF messages DEFAULT",
    "CREATE INDEX idx_messages_ticket_id ON messages (ticket_id, created_at)",
    "CREATE INDEX idx_messages_meta_id ON messages (meta_message_id)",
    "CREATE INDEX ix_messages_organization_id ON messages (organization_id)",
    # Função que cria partições mensais; é chamada na migração e diariamente pela aplicação (job db.partitions)
    """
    CREATE OR REPLACE FUNCTION ensure_message_partitions(months_back INT DEFAULT 1, months_ahead INT DEFAULT 3)
    RETURNS VOID AS $$
    DECLARE
        m DATE := date_trunc('month', CURRENT_DATE)::DATE;
        start_d DATE;
        end_d DATE;
        part TEXT;
        i INT;
    BEGIN
        FOR i IN -months_back..months_ahead LOOP
            start_d := (m + (i || ' month')::INTERVAL)::DATE;
            end_d   := (start_d + INTERVAL '1 month')::DATE;
            part    := 'messages_' || to_char(start_d, 'YYYY_MM');
            IF to_regclass(part) IS NULL THEN
                EXECUTE format('CREATE TABLE %I PARTITION OF messages FOR VALUES FROM (%L) TO (%L)', part, start_d, end_d);
            END IF;
        END LOOP;
    END;
    $$ LANGUAGE plpgsql
    """,
    "SELECT ensure_message_partitions(1, 6)",
    # --- Auditoria imutável (append-only)
    """
    CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs é append-only: UPDATE/DELETE/TRUNCATE não são permitidos';
    END;
    $$ LANGUAGE plpgsql
    """,
    "CREATE TRIGGER trg_audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable()",
    "CREATE TRIGGER trg_audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_immutable()",
]


def upgrade() -> None:
    bind = op.get_bind()
    tables = Base.metadata.tables
    others = [t for name, t in tables.items() if name != "messages"]
    Base.metadata.create_all(bind, tables=others)  # ordena por dependência e cria os enums nativos
    if bind.dialect.name == "postgresql":
        for name, values in MESSAGE_ENUMS.items():
            sa.Enum(*values, name=name).create(bind, checkfirst=True)
        for stmt in PG_STATEMENTS:
            bind.exec_driver_sql(stmt)
    else:
        tables["messages"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_audit_logs_no_update_delete ON audit_logs")
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_audit_logs_no_truncate ON audit_logs")
        bind.exec_driver_sql("DROP FUNCTION IF EXISTS audit_logs_immutable()")
        bind.exec_driver_sql("DROP TABLE IF EXISTS messages CASCADE")
        bind.exec_driver_sql("DROP FUNCTION IF EXISTS ensure_message_partitions(INT, INT)")
        for name in MESSAGE_ENUMS:
            bind.exec_driver_sql(f"DROP TYPE IF EXISTS {name}")
    Base.metadata.drop_all(bind)
