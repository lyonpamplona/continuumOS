"""Fase 6 — Ambiente Alembic.

Resolve a URL do banco a partir de ``config.settings.DATABASE_URL`` (mesma fonte
da aplicação, incluindo secret manager via env_secret). Converte o driver
asyncpg para o driver síncrono do SQLAlchemy (psycopg2) apenas para rodar as
migrations, sem exigir mudança na app.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Garante que a raiz do projeto esteja no path para importar config.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass


def _sync_db_url() -> str:
    """Normaliza a URL para um driver síncrono compatível com Alembic."""
    url = settings.DATABASE_URL or ""
    # asyncpg -> psycopg2 (síncrono) apenas para migrations.
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


# Sem metadata declarativa (schema é gerido por SQL explícito nas migrations).
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_db_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
