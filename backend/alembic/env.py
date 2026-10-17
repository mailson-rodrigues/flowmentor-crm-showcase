"""Alembic environment (async). Usage:  alembic upgrade head"""
import asyncio
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  (registra todos os mapeamentos)
from app.config import settings
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata
_MESSAGE_PARTITION = re.compile(r"^messages_(default|\d{4}_\d{2})$")


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """The monthly partitions of `messages` are created by the migration/job, not by the metadata: ignore them in autogenerate."""
    if type_ == "table" and reflected and name and _MESSAGE_PARTITION.match(name):
        return False
    if type_ == "index" and reflected and obj.table is not None and _MESSAGE_PARTITION.match(obj.table.name):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata, include_object=include_object, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object)
    pg = connection.dialect.name == "postgresql"
    if pg:  # replicas starting together run `alembic upgrade head`: only one migrates at a time
        connection.exec_driver_sql("SELECT pg_advisory_lock(hashtext('flowmentor:alembic'))")
        connection.commit()  # the lock is session-scoped; end the implicit transaction so Alembic can manage its own
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if pg:
            connection.exec_driver_sql("SELECT pg_advisory_unlock(hashtext('flowmentor:alembic'))")
            connection.commit()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
