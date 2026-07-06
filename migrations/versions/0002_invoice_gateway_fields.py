"""Fase 6 — Campos de gateway de pagamento em invoices (reversível).

Adiciona ``provider`` e ``external_id`` à tabela ``invoices`` e um índice único
parcial sobre ``external_id`` (permite casar o webhook do gateway com a fatura).

Exemplo de migration incremental idempotente e reversível — o fluxo esperado a
partir de agora para mudanças de schema (em vez de DDL no boot).

Revision ID: 0002_invoice_gateway_fields
Revises: 0001_baseline
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op

revision = "0002_invoice_gateway_fields"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS external_id TEXT")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_external "
        "ON invoices (external_id) WHERE external_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_invoices_external")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS external_id")
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS provider")
