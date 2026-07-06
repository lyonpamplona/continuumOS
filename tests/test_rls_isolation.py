"""Fase 6 — Teste de isolamento multi-tenant (RLS) ponta a ponta.

Cria dois tenants (A e B), insere dados escopados em cada um e garante que, ao
assumir o contexto de A, nunca se enxergam linhas de B (e vice-versa). Também
valida que o owner do banco está sujeito ao RLS (FORCE ROW LEVEL SECURITY).

Este teste é de INTEGRAÇÃO: exige um Postgres real. É pulado por padrão e só
roda quando ``RUN_DB_TESTS=1`` (no CI subimos um serviço Postgres). Usa a
``DATABASE_URL`` do ambiente.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="Teste de integração RLS: defina RUN_DB_TESTS=1 e DATABASE_URL para rodar.",
)


async def _set_tenant(conn, tenant_id):
    """Ativa o contexto de tenant na sessão (mesma GUC usada pela app)."""
    await conn.execute("SELECT set_config('app.current_tenant', $1, false)", str(tenant_id))


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_reads():
    import database as db

    await db.init_db()
    assert db.pool is not None
    try:
        # Cria dois tenants isolados.
        suffix = uuid.uuid4().hex[:8]
        tid_a = await db.create_tenant(f"tenant-a-{suffix}", f"a-{suffix}.example.com", "free")
        tid_b = await db.create_tenant(f"tenant-b-{suffix}", f"b-{suffix}.example.com", "free")
        assert tid_a and tid_b and tid_a != tid_b

        phone_a = f"55{suffix}0001"
        phone_b = f"55{suffix}0002"

        async with db.pool.acquire() as conn:
            # Insere um contato para cada tenant sob o respectivo contexto.
            await _set_tenant(conn, tid_a)
            await conn.execute(
                "INSERT INTO users (phone, name, tenant_id) VALUES ($1, $2, $3)",
                phone_a, "Contato A", tid_a,
            )
            await _set_tenant(conn, tid_b)
            await conn.execute(
                "INSERT INTO users (phone, name, tenant_id) VALUES ($1, $2, $3)",
                phone_b, "Contato B", tid_b,
            )

            # Contexto A: vê o próprio contato e NUNCA o de B.
            await _set_tenant(conn, tid_a)
            rows_a = await conn.fetch("SELECT phone FROM users")
            phones_a = {r["phone"] for r in rows_a}
            assert phone_a in phones_a
            assert phone_b not in phones_a
            seen_b = await conn.fetchval(
                "SELECT count(*) FROM users WHERE phone = $1", phone_b
            )
            assert seen_b == 0

            # Contexto B: simétrico.
            await _set_tenant(conn, tid_b)
            rows_b = await conn.fetch("SELECT phone FROM users")
            phones_b = {r["phone"] for r in rows_b}
            assert phone_b in phones_b
            assert phone_a not in phones_b

            # Sem contexto definido: RLS não deve vazar linhas de nenhum tenant.
            await conn.execute("SELECT set_config('app.current_tenant', '', false)")
            leaked = await conn.fetchval(
                "SELECT count(*) FROM users WHERE phone IN ($1, $2)", phone_a, phone_b
            )
            assert leaked == 0
    finally:
        await db.close_db()
