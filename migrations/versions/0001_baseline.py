"""Fase 6 — Baseline: schema canônico do continnumOS CRM.

Esta migration reutiliza o DDL testado de ``database.init_db`` (multi-tenant +
RLS + billing) para construir o schema completo de forma reprodutível. Assim,
``alembic upgrade head`` substitui a "migração no boot": em produção defina
``RUN_DB_MIGRATIONS_ON_BOOT=false`` e rode as migrations no deploy.

Como ``init_db`` é assíncrono (asyncpg) e cria seu próprio pool a partir de
``settings.DATABASE_URL``, executamos via ``asyncio.run`` forçando o flag de
boot para garantir a aplicação do DDL independentemente do ambiente.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-05
"""

from __future__ import annotations

import asyncio

# Identificadores Alembic
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    import config
    import database as db

    # Força a aplicação do DDL mesmo que o ambiente use Alembic no boot.
    original = getattr(config.settings, "RUN_DB_MIGRATIONS_ON_BOOT", True)
    config.settings.RUN_DB_MIGRATIONS_ON_BOOT = True
    try:
        asyncio.run(_apply_schema(db))
    finally:
        config.settings.RUN_DB_MIGRATIONS_ON_BOOT = original


async def _apply_schema(db) -> None:
    await db.init_db()
    await db.close_db()


def downgrade() -> None:
    """Baseline não é reversível automaticamente (dropar todo o schema é
    destrutivo). Para reset completo, use um banco limpo. Migrações incrementais
    posteriores implementam downgrade granular."""
    raise NotImplementedError(
        "Downgrade do baseline é destrutivo; recrie o banco a partir de um dump."
    )
