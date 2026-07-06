"""Camada de persistência PostgreSQL do CRM.

O módulo mantém um pool global de conexões `asyncpg`, cria as tabelas
necessárias na inicialização e expõe funções pequenas para leitura/escrita
usadas pelas rotas, webhooks e tarefas de fundo.
"""

import asyncpg
import bcrypt
import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from calendar import monthrange
from config import settings
from starlette.concurrency import run_in_threadpool
from loguru import logger
from services.runtime import format_duration
from tenant_context import get_current_tenant, DEFAULT_TENANT_ID

pool = None
SCHEMA_LOCK_ID = 2026051101
BR_TZ = timezone(timedelta(hours=-3))

# Fase 1 — Multi-tenancy: tabelas que recebem `tenant_id` + RLS.
# Mapeia tabela -> coluna "quente" usada no índice composto (tenant_id, <col>).
TENANT_TABLES = {
    "system_users": "username",
    "users": "phone",
    "messages": "phone",
    "processed_webhook_messages": "message_id",
    "internal_messages": "target_name",
    "bot_settings": "id",
    "user_snippets": "username",
    "calendar_events": "assigned_to",
    "tags": "name",
    "contact_tags": "phone",
    "contact_lists": "name",
    "contact_list_members": "list_id",
    "metrics": "seller_name",
    "seller_status": "seller_name",
    "audit_logs": "created_at",
    "status_posts": "status_id",
    "status_targets": "status_id",
    "status_views": "status_id",
}


def current_business_date() -> str:
    """Retorna a data operacional em fuso de Brasília para métricas diárias."""
    return datetime.now(BR_TZ).strftime('%Y-%m-%d')

def get_month_bounds(year_month: str = "") -> tuple[str, str]:
    """Retorna as datas inicial e final de um mês calendário em `YYYY-MM-DD`.

    `year_month` deve vir no formato `YYYY-MM`. Quando vazio, usa o mês
    operacional atual em UTC-3. O retorno é inclusivo e compatível com a coluna
    textual `metrics.date`, que já usa ordenação lexicográfica segura.
    """
    clean_value = (year_month or "").strip()
    if not clean_value:
        clean_value = datetime.now(BR_TZ).strftime("%Y-%m")
    try:
        year, month = map(int, clean_value.split("-", 1))
        if month < 1 or month > 12:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("year_month deve estar no formato YYYY-MM")
    last_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"

def _date_param(value: str | date) -> date:
    """Converte `YYYY-MM-DD` em `date` para parâmetros `$n::date` do asyncpg."""
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()

async def _apply_tenant_guc(conn):
    """Hook do pool: aplica a GUC `app.current_tenant` a cada conexão adquirida.

    Junto com as policies de RLS, garante que toda query fique restrita ao
    tenant atual (definido no contextvar da requisição). O asyncpg executa este
    hook em todo `pool.acquire()`, inclusive nas chamadas diretas
    `pool.fetch/execute/fetchrow/fetchval`.
    """
    tenant_id = get_current_tenant()
    await conn.execute("SELECT set_config('app.current_tenant', $1, false)", str(tenant_id))


async def get_tenant_id_by_host(host: str):
    """Resolve o id do tenant a partir do host (domínio exato ou subdomínio)."""
    if not host or pool is None:
        return None
    row = await pool.fetchrow("SELECT id FROM tenants WHERE domain = $1 AND status = 'active'", host)
    if row:
        return row["id"]
    subdomain = host.split(".")[0]
    row = await pool.fetchrow("SELECT id FROM tenants WHERE slug = $1 AND status = 'active'", subdomain)
    if row:
        return row["id"]
    return None


async def get_tenant_id_by_instance(instance: str):
    """Fase 2: resolve o tenant dono de uma instância Evolution (roteia webhook)."""
    if not instance or pool is None:
        return None
    row = await pool.fetchrow(
        "SELECT id FROM tenants WHERE evolution_instance = $1 AND status = 'active'",
        instance,
    )
    return row["id"] if row else None


async def get_tenant_instance(tenant_id) -> str:
    """Fase 2: retorna o nome da instância Evolution configurada para o tenant."""
    if pool is None:
        return "vendas"
    try:
        row = await pool.fetchrow("SELECT evolution_instance FROM tenants WHERE id = $1", int(tenant_id))
    except (TypeError, ValueError):
        return "vendas"
    if row and row["evolution_instance"]:
        return row["evolution_instance"]
    return "vendas"


async def get_tenant_record(tenant_id):
    """Fase 3: retorna o registro completo do tenant, com `settings` JSONB parseado."""
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT id, slug, display_name, status, evolution_instance, brand_color, domain, logo_url, settings "
            "FROM tenants WHERE id = $1",
            int(tenant_id),
        )
    except (TypeError, ValueError):
        return None
    if not row:
        return None
    data = dict(row)
    raw = data.get("settings")
    if isinstance(raw, str):
        try:
            data["settings"] = json.loads(raw)
        except Exception:
            data["settings"] = {}
    elif raw is None:
        data["settings"] = {}
    return data


async def update_tenant_settings(tenant_id, patch: dict):
    """Fase 3: mescla um patch (raso) no JSONB `tenants.settings` do tenant."""
    if pool is None or not isinstance(patch, dict):
        return
    await pool.execute(
        "UPDATE tenants SET settings = COALESCE(settings, '{}'::jsonb) || $2::jsonb WHERE id = $1",
        int(tenant_id),
        json.dumps(patch),
    )


# --------------------------------------------------------------------------
# Fase 4: Billing e super-admin (helpers)
# --------------------------------------------------------------------------

async def get_tenant_status(tenant_id) -> str:
    """Fase 4: retorna o status do tenant (active/suspended)."""
    if pool is None:
        return "active"
    try:
        row = await pool.fetchrow("SELECT status FROM tenants WHERE id = $1", int(tenant_id))
    except (TypeError, ValueError):
        return "active"
    return row["status"] if row else "active"


async def get_tenant_route_by_instance(instance: str):
    """Fase 4: resolve (id, status) do tenant dono da instância, sem filtrar por status."""
    if not instance or pool is None:
        return None
    row = await pool.fetchrow("SELECT id, status FROM tenants WHERE evolution_instance = $1", instance)
    return {"id": row["id"], "status": row["status"]} if row else None


async def get_all_plans():
    """Fase 4: lista os planos disponíveis."""
    if pool is None:
        return []
    rows = await pool.fetch("SELECT * FROM plans ORDER BY price_cents ASC")
    return [dict(r) for r in rows]


async def get_plan(code: str):
    """Fase 4: retorna um plano pelo código."""
    if pool is None or not code:
        return None
    row = await pool.fetchrow("SELECT * FROM plans WHERE code = $1", code)
    return dict(row) if row else None


async def list_tenants():
    """Fase 4: lista todos os tenants (uso do super-admin; tabela sem RLS)."""
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT id, slug, display_name, status, plan, billing_status, evolution_instance, "
        "brand_color, domain, plan_overrides, trial_ends_at, created_at FROM tenants ORDER BY id ASC"
    )
    result = []
    for r in rows:
        d = dict(r)
        raw = d.get("plan_overrides")
        if isinstance(raw, str):
            try:
                d["plan_overrides"] = json.loads(raw)
            except Exception:
                d["plan_overrides"] = {}
        result.append(d)
    return result


async def create_tenant(slug, display_name, domain=None, evolution_instance=None, plan="free", brand_color="#FF6600"):
    """Fase 4: cria um novo tenant (cliente da plataforma)."""
    if pool is None:
        return None
    instance = evolution_instance or slug
    row = await pool.fetchrow(
        """
        INSERT INTO tenants (slug, display_name, domain, evolution_instance, plan, brand_color)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (slug) DO NOTHING
        RETURNING id, slug, display_name, status, plan, evolution_instance, domain, brand_color
        """,
        slug, display_name, (domain or None), instance, plan, brand_color,
    )
    return dict(row) if row else None


async def count_tenant_instances(tenant_id) -> int:
    """Fase 7: quantas instâncias Evolution o tenant tem provisionadas.

    Modelo atual: 1 instância por tenant (coluna evolution_instance). Retorna 1 se
    houver instância definida, 0 caso contrário. Centralizado aqui para o dia em
    que houver uma tabela dedicada de instâncias.
    """
    if pool is None:
        return 0
    try:
        row = await pool.fetchrow(
            "SELECT evolution_instance FROM tenants WHERE id = $1", int(tenant_id)
        )
    except (TypeError, ValueError):
        return 0
    return 1 if (row and row["evolution_instance"]) else 0


async def check_instance_limit(tenant_id):
    """Fase 7: (ok, current, limit) para provisionamento de instâncias por plano.

    limit < 0 significa ilimitado. ok=True quando ainda cabe uma nova instância.
    """
    limits = await get_tenant_limits(tenant_id)
    limit = limits.get("max_instances", 1)
    current = await count_tenant_instances(tenant_id)
    if limit is None or int(limit) < 0:
        return (True, current, -1)
    return (current < int(limit), current, int(limit))


async def set_tenant_status(tenant_id, status_value):
    """Fase 4: ativa/suspende um tenant."""
    if pool is None:
        return
    await pool.execute("UPDATE tenants SET status = $2 WHERE id = $1", int(tenant_id), status_value)


# --------------------------------------------------------------------------
# Fase 8: Signup self-service + trial, portal de billing e LGPD
# --------------------------------------------------------------------------

async def get_tenant(tenant_id):
    """Fase 8: retorna o registro completo de um tenant (uso do portal do cliente)."""
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT id, slug, display_name, status, plan, billing_status, evolution_instance, "
            "brand_color, domain, plan_overrides, trial_ends_at, created_at "
            "FROM tenants WHERE id = $1",
            int(tenant_id),
        )
    except (TypeError, ValueError):
        return None
    if not row:
        return None
    d = dict(row)
    raw = d.get("plan_overrides")
    if isinstance(raw, str):
        try:
            d["plan_overrides"] = json.loads(raw)
        except Exception:
            d["plan_overrides"] = {}
    return d


async def slug_available(slug: str) -> bool:
    """Fase 8: verifica se um slug de tenant está livre (signup self-service)."""
    if pool is None or not slug:
        return False
    row = await pool.fetchrow("SELECT 1 FROM tenants WHERE slug = $1", slug)
    return row is None


async def username_available(username: str) -> bool:
    """Fase 8: system_users.username é PK GLOBAL; garante unicidade no signup."""
    if pool is None or not username:
        return False
    row = await pool.fetchrow("SELECT 1 FROM system_users WHERE username = $1", username)
    return row is None


async def create_tenant_self_service(
    slug, display_name, admin_username, admin_password_hash, admin_name,
    plan="free", trial_days=14, domain=None,
):
    """Fase 8: cria um tenant em modo trial + o primeiro usuário admin do cliente.

    Retorna dict com o tenant criado, ou None se o slug/username já existir. O
    usuário admin é inserido no contexto (GUC) do novo tenant para respeitar o RLS.
    """
    if pool is None:
        return None
    if not await slug_available(slug) or not await username_available(admin_username):
        return None
    instance = slug
    row = await pool.fetchrow(
        """
        INSERT INTO tenants (slug, display_name, domain, evolution_instance, plan,
                             status, billing_status, trial_ends_at)
        VALUES ($1, $2, $3, $4, $5, 'active', 'trial',
                now() + ($6 || ' days')::interval)
        ON CONFLICT (slug) DO NOTHING
        RETURNING id, slug, display_name, status, plan, billing_status,
                  evolution_instance, domain, brand_color, trial_ends_at
        """,
        slug, display_name, (domain or None), instance, plan, str(int(trial_days)),
    )
    if not row:
        return None
    tenant = dict(row)
    prev = get_current_tenant()
    try:
        set_current_tenant(tenant["id"])
        await pool.execute(
            "INSERT INTO system_users (username, password, role, name, is_active, tenant_id) "
            "VALUES ($1, $2, 'admin', $3, 1, $4) ON CONFLICT (username) DO NOTHING",
            admin_username, admin_password_hash, admin_name, tenant["id"],
        )
    finally:
        set_current_tenant(prev)
    return tenant


async def set_tenant_billing_status(tenant_id, billing_status):
    """Fase 8: atualiza o estado de cobrança do tenant (trial/active/past_due...)."""
    if pool is None:
        return
    await pool.execute(
        "UPDATE tenants SET billing_status = $2 WHERE id = $1",
        int(tenant_id), billing_status,
    )


async def export_contact_data(phone: str) -> dict:
    """Fase 8 (LGPD): exporta todos os dados de um titular (por telefone) no tenant
    corrente. Respeita RLS: só enxerga o tenant ativo no contexto.
    """
    if pool is None or not phone:
        return {}
    export = {"phone": phone, "tables": {}}
    # Tabelas que referenciam o titular por telefone/relacionamento direto.
    queries = {
        "users": "SELECT * FROM users WHERE phone = $1",
        "messages": "SELECT * FROM messages WHERE phone = $1",
        "calendar_events": "SELECT * FROM calendar_events WHERE phone = $1",
        "contact_tags": "SELECT * FROM contact_tags WHERE phone = $1",
        "contact_list_members": "SELECT * FROM contact_list_members WHERE phone = $1",
    }
    for table, sql in queries.items():
        try:
            rows = await pool.fetch(sql, phone)
            export["tables"][table] = [dict(r) for r in rows]
        except asyncpg.PostgresError as exc:
            export["tables"][table] = {"__error__": str(exc)}
    return export


async def delete_contact_data(phone: str) -> dict:
    """Fase 8 (LGPD): apaga os dados de um titular (por telefone) no tenant corrente.
    Retorna a contagem por tabela. Respeita RLS.
    """
    if pool is None or not phone:
        return {}
    deleted = {}
    tables = [
        "messages", "calendar_events", "contact_tags",
        "contact_list_members", "users",
    ]
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in tables:
                try:
                    res = await conn.execute(f'DELETE FROM "{table}" WHERE phone = $1', phone)
                    # res no formato "DELETE <n>"
                    deleted[table] = int(res.split()[-1]) if res else 0
                except asyncpg.PostgresError as exc:
                    deleted[table] = f"erro: {exc}"
    return deleted


async def purge_old_messages(retention_days: int) -> int:
    """Fase 8 (LGPD/retenção): apaga mensagens mais antigas que N dias no tenant
    corrente. Retorna quantas linhas foram removidas. 0/negativo desativa.
    """
    if pool is None or not retention_days or int(retention_days) <= 0:
        return 0
    res = await pool.execute(
        "DELETE FROM messages WHERE timestamp < now() - ($1 || ' days')::interval",
        str(int(retention_days)),
    )
    try:
        return int(res.split()[-1]) if res else 0
    except Exception:
        return 0


# --------------------------------------------------------------------------
# Fase 9: Diferenciais — API pública, webhooks de saída, integrações, analytics
#
# Estas tabelas são GLOBAIS (sem RLS), com coluna tenant_id e filtro explícito.
# Isso simplifica a resolução token->tenant (API pública) e a entrega de
# webhooks fora do contexto de uma requisição web.
# --------------------------------------------------------------------------

async def create_phase9_tables(db):
    """Fase 9: cria as tabelas de API keys, webhooks e integrações (idempotente)."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            name TEXT NOT NULL DEFAULT 'default',
            key_hash TEXT UNIQUE NOT NULL,
            prefix TEXT NOT NULL,
            scopes TEXT NOT NULL DEFAULT 'read,write',
            revoked BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys (tenant_id)")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS webhook_endpoints (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            url TEXT NOT NULL,
            secret TEXT NOT NULL DEFAULT '',
            events TEXT NOT NULL DEFAULT '*',
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_tenant ON webhook_endpoints (tenant_id)")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            endpoint_id INTEGER,
            event TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            status_code INTEGER,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_tenant ON webhook_deliveries (tenant_id, created_at)")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS tenant_integrations (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            integration_key TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT false,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(tenant_id, integration_key)
        )"""
    )
    # Fase 9: coluna de logo para white-label (brand_color/domain já existem).
    await db.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS logo_url TEXT DEFAULT ''")


def _hash_api_key(raw: str) -> str:
    import hashlib
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


async def create_api_key(tenant_id, name="default", scopes="read,write"):
    """Fase 9: gera uma API key. Retorna (registro, chave_em_texto_puro UMA vez)."""
    if pool is None:
        return None, None
    import secrets
    raw = "ebk_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    key_hash = _hash_api_key(raw)
    row = await pool.fetchrow(
        "INSERT INTO api_keys (tenant_id, name, key_hash, prefix, scopes) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id, tenant_id, name, prefix, scopes, created_at",
        int(tenant_id), name, key_hash, prefix, scopes,
    )
    return (dict(row) if row else None), raw


async def list_api_keys(tenant_id):
    """Fase 9: lista as API keys de um tenant (sem expor o hash)."""
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT id, name, prefix, scopes, revoked, created_at, last_used_at "
        "FROM api_keys WHERE tenant_id = $1 ORDER BY created_at DESC",
        int(tenant_id),
    )
    return [dict(r) for r in rows]


async def revoke_api_key(tenant_id, key_id):
    """Fase 9: revoga uma API key do tenant."""
    if pool is None:
        return False
    res = await pool.execute(
        "UPDATE api_keys SET revoked = true WHERE id = $1 AND tenant_id = $2",
        int(key_id), int(tenant_id),
    )
    return bool(res and res.split()[-1] != "0")


async def resolve_tenant_by_api_key(raw_key: str):
    """Fase 9: resolve (tenant_id, scopes) a partir da chave crua. None se inválida,
    revogada ou de tenant inativo. Atualiza last_used_at.
    """
    if pool is None or not raw_key:
        return None
    key_hash = _hash_api_key(raw_key)
    row = await pool.fetchrow(
        "SELECT k.id, k.tenant_id, k.scopes FROM api_keys k "
        "JOIN tenants t ON t.id = k.tenant_id "
        "WHERE k.key_hash = $1 AND k.revoked = false AND t.status = 'active'",
        key_hash,
    )
    if not row:
        return None
    await pool.execute("UPDATE api_keys SET last_used_at = now() WHERE id = $1", row["id"])
    return {"tenant_id": row["tenant_id"], "scopes": (row["scopes"] or "").split(",")}


async def create_webhook_endpoint(tenant_id, url, events="*", secret=""):
    """Fase 9: registra um endpoint de webhook de saída para o tenant."""
    if pool is None:
        return None
    row = await pool.fetchrow(
        "INSERT INTO webhook_endpoints (tenant_id, url, events, secret) "
        "VALUES ($1, $2, $3, $4) RETURNING id, url, events, active, created_at",
        int(tenant_id), url, events, secret,
    )
    return dict(row) if row else None


async def list_webhook_endpoints(tenant_id):
    """Fase 9: lista os endpoints de webhook do tenant (sem expor o secret)."""
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT id, url, events, active, created_at FROM webhook_endpoints "
        "WHERE tenant_id = $1 ORDER BY created_at DESC",
        int(tenant_id),
    )
    return [dict(r) for r in rows]


async def delete_webhook_endpoint(tenant_id, endpoint_id):
    """Fase 9: remove um endpoint de webhook do tenant."""
    if pool is None:
        return False
    res = await pool.execute(
        "DELETE FROM webhook_endpoints WHERE id = $1 AND tenant_id = $2",
        int(endpoint_id), int(tenant_id),
    )
    return bool(res and res.split()[-1] != "0")


async def get_active_webhooks_for_event(tenant_id, event):
    """Fase 9: endpoints ativos que assinam um evento (ou '*')."""
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT id, url, secret, events FROM webhook_endpoints "
        "WHERE tenant_id = $1 AND active = true",
        int(tenant_id),
    )
    out = []
    for r in rows:
        ev = (r["events"] or "*").strip()
        subs = [e.strip() for e in ev.split(",")] if ev != "*" else ["*"]
        if "*" in subs or event in subs:
            out.append(dict(r))
    return out


async def record_webhook_delivery(tenant_id, endpoint_id, event, status, status_code=None, error=None):
    """Fase 9: registra o resultado da entrega de um webhook (observabilidade)."""
    if pool is None:
        return
    await pool.execute(
        "INSERT INTO webhook_deliveries (tenant_id, endpoint_id, event, status, status_code, error) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        int(tenant_id), (int(endpoint_id) if endpoint_id else None), event, status,
        (int(status_code) if status_code is not None else None),
        (str(error)[:500] if error else None),
    )


async def list_tenant_integrations(tenant_id):
    """Fase 9: estado das integrações (marketplace) do tenant."""
    if pool is None:
        return {}
    rows = await pool.fetch(
        "SELECT integration_key, enabled, config, updated_at FROM tenant_integrations WHERE tenant_id = $1",
        int(tenant_id),
    )
    result = {}
    for r in rows:
        cfg = r["config"]
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        result[r["integration_key"]] = {"enabled": r["enabled"], "config": cfg}
    return result


async def set_tenant_integration(tenant_id, integration_key, enabled, config=None):
    """Fase 9: habilita/desabilita e configura uma integração do marketplace."""
    if pool is None:
        return
    await pool.execute(
        """INSERT INTO tenant_integrations (tenant_id, integration_key, enabled, config, updated_at)
           VALUES ($1, $2, $3, $4::jsonb, now())
           ON CONFLICT (tenant_id, integration_key)
           DO UPDATE SET enabled = EXCLUDED.enabled, config = EXCLUDED.config, updated_at = now()""",
        int(tenant_id), integration_key, bool(enabled), json.dumps(config or {}),
    )


async def list_contacts_paginated(limit=100, offset=0):
    """Fase 9: lista contatos do tenant corrente (RLS via GUC) paginados.

    Usa o pool (a GUC app.current_tenant é aplicada por conexão), portanto o
    chamador deve ter ativado o contexto do tenant via set_current_tenant.
    """
    if pool is None:
        return []
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    rows = await pool.fetch(
        "SELECT phone, name, stage, assigned_to, last_interaction, session_id "
        "FROM users ORDER BY last_interaction DESC NULLS LAST LIMIT $1 OFFSET $2",
        limit, offset,
    )
    return [dict(r) for r in rows]


async def update_tenant_branding(tenant_id, display_name=None, brand_color=None, logo_url=None, domain=None):
    """Fase 9: atualiza os campos de white-label do tenant (apenas os informados).

    `domain` deve ser único; conflito retorna erro amigável via exceção tratada.
    """
    if pool is None:
        return None
    sets = []
    params = []
    idx = 1
    for col, val in (("display_name", display_name), ("brand_color", brand_color),
                     ("logo_url", logo_url), ("domain", domain)):
        if val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
    if not sets:
        return await get_tenant_record(tenant_id)
    params.append(int(tenant_id))
    query = ("UPDATE tenants SET " + ", ".join(sets) + f" WHERE id = ${idx} "
             "RETURNING id, slug, display_name, status, brand_color, domain, logo_url")
    row = await pool.fetchrow(query, *params)
    return dict(row) if row else None


async def get_tenant_analytics(tenant_id, days=30):
    """Fase 9: analytics por tenant (RLS-scoped) para o portal/API.

    Retorna totais e séries: mensagens/dia, split in/out, contatos e contatos
    ativos na janela. Usa contexto do tenant (GUC) para respeitar o RLS.
    """
    if pool is None:
        return {}
    days = max(1, min(int(days or 30), 365))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_tenant', $1, true)", str(int(tenant_id)))
            total_contacts = await conn.fetchval("SELECT count(*) FROM users") or 0
            total_messages = await conn.fetchval(
                "SELECT count(*) FROM messages WHERE timestamp >= now() - ($1 || ' days')::interval",
                str(days),
            ) or 0
            inbound = await conn.fetchval(
                "SELECT count(*) FROM messages WHERE sender = 'user' "
                "AND timestamp >= now() - ($1 || ' days')::interval", str(days),
            ) or 0
            outbound = await conn.fetchval(
                "SELECT count(*) FROM messages WHERE sender <> 'user' "
                "AND timestamp >= now() - ($1 || ' days')::interval", str(days),
            ) or 0
            active_contacts = await conn.fetchval(
                "SELECT count(DISTINCT phone) FROM messages "
                "WHERE timestamp >= now() - ($1 || ' days')::interval", str(days),
            ) or 0
            series_rows = await conn.fetch(
                "SELECT to_char(date_trunc('day', timestamp), 'YYYY-MM-DD') AS day, count(*) AS total "
                "FROM messages WHERE timestamp >= now() - ($1 || ' days')::interval "
                "GROUP BY day ORDER BY day", str(days),
            )
    return {
        "window_days": days,
        "totals": {
            "contacts": int(total_contacts),
            "active_contacts": int(active_contacts),
            "messages": int(total_messages),
            "inbound": int(inbound),
            "outbound": int(outbound),
        },
        "messages_per_day": [{"day": r["day"], "total": int(r["total"])} for r in series_rows],
    }


async def set_tenant_plan(tenant_id, plan_code=None, overrides=None):
    """Fase 4: define o plano e/ou os overrides de limites do tenant."""
    if pool is None:
        return
    if plan_code is not None:
        await pool.execute("UPDATE tenants SET plan = $2 WHERE id = $1", int(tenant_id), plan_code)
    if overrides is not None and isinstance(overrides, dict):
        await pool.execute(
            "UPDATE tenants SET plan_overrides = $2::jsonb WHERE id = $1",
            int(tenant_id), json.dumps(overrides),
        )


async def get_tenant_limits(tenant_id):
    """Fase 4: limites efetivos do tenant (plano + overrides). -1 = ilimitado."""
    plan_code = "free"
    overrides = {}
    if pool is not None:
        try:
            row = await pool.fetchrow("SELECT plan, plan_overrides FROM tenants WHERE id = $1", int(tenant_id))
        except (TypeError, ValueError):
            row = None
        if row:
            plan_code = row["plan"] or "free"
            raw = row["plan_overrides"]
            if isinstance(raw, str):
                try:
                    overrides = json.loads(raw)
                except Exception:
                    overrides = {}
            elif isinstance(raw, dict):
                overrides = raw
    plan = await get_plan(plan_code) or {}
    limits = {
        "max_users": plan.get("max_users", -1),
        "max_contacts": plan.get("max_contacts", -1),
        "max_messages_month": plan.get("max_messages_month", -1),
        "max_instances": plan.get("max_instances", 1),
    }
    for k, v in (overrides or {}).items():
        if k in limits:
            try:
                limits[k] = int(v)
            except Exception:
                logger.debug('excecao ignorada (best-effort)', exc_info=True)
    limits["plan"] = plan_code
    return limits


_COUNT_TABLES = {"system_users", "users", "messages"}


async def count_scoped(tenant_id, table):
    """Fase 4: conta linhas de uma tabela RLS no escopo de um tenant específico."""
    if pool is None or table not in _COUNT_TABLES:
        return 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_tenant', $1, true)", str(int(tenant_id)))
            if table == "messages":
                val = await conn.fetchval(
                    "SELECT count(*) FROM messages WHERE timestamp >= date_trunc('month', now())"
                )
            else:
                val = await conn.fetchval(f"SELECT count(*) FROM {table}")
    return int(val or 0)


async def get_tenant_usage(tenant_id):
    """Fase 4: uso atual do tenant vs. limites do plano."""
    limits = await get_tenant_limits(tenant_id)
    usage = {
        "users": await count_scoped(tenant_id, "system_users"),
        "contacts": await count_scoped(tenant_id, "users"),
        "messages_month": await count_scoped(tenant_id, "messages"),
    }
    return {"limits": limits, "usage": usage}


async def check_tenant_limit(tenant_id, kind):
    """Fase 4: (ok, current, limit) para um recurso. kind: users|contacts|messages."""
    limits = await get_tenant_limits(tenant_id)
    mapping = {
        "users": ("max_users", "system_users"),
        "contacts": ("max_contacts", "users"),
        "messages": ("max_messages_month", "messages"),
    }
    if kind not in mapping:
        return (True, 0, -1)
    limit_key, table = mapping[kind]
    limit = limits.get(limit_key, -1)
    if limit is None or int(limit) < 0:
        return (True, 0, -1)
    current = await count_scoped(tenant_id, table)
    return (current < int(limit), current, int(limit))


async def list_invoices(tenant_id=None):
    """Fase 4: lista faturas (de um tenant ou de todos)."""
    if pool is None:
        return []
    if tenant_id is None:
        rows = await pool.fetch("SELECT * FROM invoices ORDER BY created_at DESC LIMIT 500")
    else:
        rows = await pool.fetch(
            "SELECT * FROM invoices WHERE tenant_id = $1 ORDER BY created_at DESC", int(tenant_id)
        )
    return [dict(r) for r in rows]


async def create_invoice(tenant_id, amount_cents, period_start=None, period_end=None, due_date=None):
    """Fase 4: cria uma fatura no ledger."""
    if pool is None:
        return None
    row = await pool.fetchrow(
        """
        INSERT INTO invoices (tenant_id, amount_cents, period_start, period_end, due_date)
        VALUES ($1, $2, $3::date, $4::date, $5::date)
        RETURNING *
        """,
        int(tenant_id), int(amount_cents), period_start, period_end, due_date,
    )
    return dict(row) if row else None


async def mark_invoice_paid(invoice_id):
    """Fase 4: marca uma fatura como paga."""
    if pool is None:
        return
    await pool.execute("UPDATE invoices SET status = 'paid', paid_at = now() WHERE id = $1", int(invoice_id))


# --------------------------------------------------------------------------
# Fase 6: Pagamento real (webhook) + suspensão automática por inadimplência
# --------------------------------------------------------------------------

async def get_invoice(invoice_id):
    """Fase 6: retorna uma fatura pelo id interno."""
    if pool is None:
        return None
    try:
        row = await pool.fetchrow("SELECT * FROM invoices WHERE id = $1", int(invoice_id))
    except (TypeError, ValueError):
        return None
    return dict(row) if row else None


async def find_invoice_by_external_id(external_id: str):
    """Fase 6: casa a fatura interna pelo id da cobrança no provedor."""
    if pool is None or not external_id:
        return None
    row = await pool.fetchrow("SELECT * FROM invoices WHERE external_id = $1", str(external_id))
    return dict(row) if row else None


async def set_invoice_external_ref(invoice_id, provider: str, external_id: str):
    """Fase 6: víncula uma fatura interna à cobrança criada no gateway."""
    if pool is None:
        return
    await pool.execute(
        "UPDATE invoices SET provider = $2, external_id = $3 WHERE id = $1",
        int(invoice_id), (provider or ""), (external_id or None),
    )


async def mark_invoice_paid_by_external_id(external_id: str, provider: str = ""):
    """Fase 6: marca como paga a fatura correspondente ao id externo. Retorna a linha."""
    if pool is None or not external_id:
        return None
    row = await pool.fetchrow(
        "UPDATE invoices SET status = 'paid', paid_at = now(), provider = COALESCE(NULLIF($2,''), provider) "
        "WHERE external_id = $1 RETURNING *",
        str(external_id), (provider or ""),
    )
    return dict(row) if row else None


async def mark_overdue_invoices(grace_days: int = 0) -> int:
    """Fase 6: marca como ``past_due`` faturas abertas cujo vencimento + graça passou.

    Retorna quantas faturas foram afetadas.
    """
    if pool is None:
        return 0
    val = await pool.fetch(
        "UPDATE invoices SET status = 'past_due' "
        "WHERE status = 'open' AND due_date IS NOT NULL "
        "AND due_date + ($1 || ' days')::interval < now() "
        "RETURNING id",
        str(int(max(0, grace_days))),
    )
    return len(val)


async def get_tenants_with_overdue():
    """Fase 6: ids de tenants ativos que possuem alguma fatura ``past_due``."""
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT DISTINCT i.tenant_id FROM invoices i "
        "JOIN tenants t ON t.id = i.tenant_id "
        "WHERE i.status = 'past_due' AND t.status = 'active'"
    )
    return [r["tenant_id"] for r in rows]


async def tenant_has_open_debt(tenant_id) -> bool:
    """Fase 6: True se o tenant tem faturas em aberto/vencidas."""
    if pool is None:
        return False
    try:
        val = await pool.fetchval(
            "SELECT count(*) FROM invoices WHERE tenant_id = $1 AND status IN ('open','past_due')",
            int(tenant_id),
        )
    except (TypeError, ValueError):
        return False
    return int(val or 0) > 0


async def reactivate_tenant_if_paid(tenant_id) -> bool:
    """Fase 6: reativa um tenant suspenso se ele não tiver mais débitos em aberto.

    Retorna True se reativou.
    """
    if pool is None:
        return False
    status = await get_tenant_status(tenant_id)
    if status != "suspended":
        return False
    if await tenant_has_open_debt(tenant_id):
        return False
    await set_tenant_status(tenant_id, "active")
    return True


async def resolve_request_tenant_id(request):
    """Determina o tenant de uma requisição HTTP; usa o tenant padrão como fallback."""
    try:
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    except Exception:
        host = ""
    if host:
        tid = await get_tenant_id_by_host(host)
        if tid:
            return tid
    return DEFAULT_TENANT_ID


async def _migrate_multitenant(db):
    """Fase 1: cria a tabela `tenants`, adiciona `tenant_id` em todas as tabelas,
    faz o backfill do tenant padrão (continnumOS = 1) e ativa o RLS por tenant.

    Idempotente: seguro para rodar em todo boot sob o mesmo advisory lock do schema.
    """
    # 1) Catálogo global de tenants (SEM RLS: usado para resolver host -> tenant).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            evolution_instance TEXT NOT NULL DEFAULT 'vendas',
            brand_color TEXT NOT NULL DEFAULT '#FF6600',
            domain TEXT,
            settings JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 2) Tenant padrão com id fixo = 1 (dados legados do continnumOS).
    await db.execute(
        """
        INSERT INTO tenants (id, slug, display_name, evolution_instance, brand_color, domain)
        VALUES (1, 'continnumos', 'continnumOS', 'vendas', '#FF6600', 'painel.continnumos.shop')
        ON CONFLICT (slug) DO NOTHING
        """
    )
    await db.execute(
        "SELECT setval(pg_get_serial_sequence('tenants', 'id'), GREATEST((SELECT MAX(id) FROM tenants), 1))"
    )

    # 3) tenant_id + backfill + NOT NULL + DEFAULT(GUC) + índice + RLS por tabela.
    for table, hot_col in TENANT_TABLES.items():
        await db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES tenants(id)")
        await db.execute(f"UPDATE {table} SET tenant_id = 1 WHERE tenant_id IS NULL")
        await db.execute(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL")
        await db.execute(
            f"ALTER TABLE {table} ALTER COLUMN tenant_id "
            f"SET DEFAULT COALESCE(NULLIF(current_setting('app.current_tenant', true), '')::int, 1)"
        )
        await db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id, {hot_col})")
        await db.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        await db.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        await db.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        await db.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = COALESCE(NULLIF(current_setting('app.current_tenant', true), '')::int, 0)) "
            f"WITH CHECK (tenant_id = COALESCE(NULLIF(current_setting('app.current_tenant', true), '')::int, 0))"
        )
    # ----------------------------------------------------------------------
    # Fase 4: Billing e super-admin (planos, limites, cobrança, suspensão).
    # ----------------------------------------------------------------------
    await db.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'")
    await db.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_overrides JSONB NOT NULL DEFAULT '{}'::jsonb")
    await db.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_status TEXT NOT NULL DEFAULT 'active'")
    await db.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ")
    # Catálogo global de planos (SEM RLS).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price_cents INTEGER NOT NULL DEFAULT 0,
            max_users INTEGER NOT NULL DEFAULT -1,
            max_contacts INTEGER NOT NULL DEFAULT -1,
            max_messages_month INTEGER NOT NULL DEFAULT -1,
            max_instances INTEGER NOT NULL DEFAULT 1,
            features JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    await db.execute(
        """
        INSERT INTO plans (code, name, price_cents, max_users, max_contacts, max_messages_month, max_instances)
        VALUES
            ('free', 'Free', 0, 2, 500, 2000, 1),
            ('pro', 'Pro', 19900, 10, 20000, 100000, 3),
            ('enterprise', 'Enterprise', 99900, -1, -1, -1, 5)
        ON CONFLICT (code) DO NOTHING
        """
    )
    # Faturas (ledger global; SEM RLS — gerido pelo super-admin).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            period_start DATE,
            period_end DATE,
            amount_cents INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            due_date DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            paid_at TIMESTAMPTZ
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_tenant ON invoices (tenant_id, created_at DESC)")
    # Fase 6: vínculo com o gateway de pagamento (webhook casa por external_id).
    await db.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT ''")
    await db.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS external_id TEXT")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_external ON invoices (external_id) WHERE external_id IS NOT NULL")
    logger.info("Fase 4/6: billing/super-admin (planos, invoices, vínculo de gateway).")

    logger.info("Fase 1: multi-tenancy aplicada (tenants + tenant_id + RLS em %d tabelas).", len(TENANT_TABLES))


async def init_db():
    """Inicializa o pool PostgreSQL, cria tabelas e garante o usuário admin."""
    global pool
    retries = 5
    for i in range(retries):
        try:
            pool = await asyncpg.create_pool(settings.DATABASE_URL, setup=_apply_tenant_guc)
            logger.info("Conexão com PostgreSQL estabelecida.")
            break
        except Exception as e:
            logger.warning(f"Tentativa {i+1} de conexão ao banco falhou: {e}")
            await asyncio.sleep(3)
            
    if not pool:
        logger.error("Falha crítica: O banco de dados não respondeu após múltiplas tentativas.")
        raise Exception("Falha crítica: O banco de dados não respondeu.")

    async with pool.acquire() as db:
        await db.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_ID)
        try:
            # Fase 6: com Alembic, o schema é aplicado por `alembic upgrade head`.
            # Defina RUN_DB_MIGRATIONS_ON_BOOT=false para não migrar no boot (o
            # `finally` abaixo libera o advisory lock normalmente ao retornar).
            if not settings.RUN_DB_MIGRATIONS_ON_BOOT:
                logger.info("RUN_DB_MIGRATIONS_ON_BOOT=false: schema gerido via Alembic; pulando DDL de boot.")
                return
            await db.execute("CREATE TABLE IF NOT EXISTS system_users (username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT, is_active INTEGER, avatar TEXT, bio TEXT)")
            await db.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS bg_color TEXT DEFAULT '#0B1014'")
            await db.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS primary_color TEXT DEFAULT '#ff6600'")
            await db.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS text_color TEXT DEFAULT '#E9EDEF'")
            await db.execute("CREATE TABLE IF NOT EXISTS users (phone TEXT PRIMARY KEY, name TEXT, stage TEXT, assigned_to TEXT, last_interaction TIMESTAMP, attendance_start TIMESTAMP, notes TEXT, extra_info TEXT, session_id TEXT DEFAULT 'vendas')")
            await db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pinned_message TEXT")
            await db.execute("CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, phone TEXT REFERENCES users(phone) ON DELETE CASCADE, sender TEXT, msg_text TEXT, msg_type TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, waha_id TEXT, ack INTEGER DEFAULT 0, session_id TEXT DEFAULT 'vendas')")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_phone_waha ON messages (phone, waha_id)")
            await db.execute("CREATE TABLE IF NOT EXISTS processed_webhook_messages (message_id TEXT PRIMARY KEY, phone TEXT, session_id TEXT, event_type TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_processed_webhook_messages_created ON processed_webhook_messages (created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_processed_webhook_messages_phone ON processed_webhook_messages (phone)")
            await db.execute("DELETE FROM processed_webhook_messages WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '14 days'")
            await db.execute("CREATE TABLE IF NOT EXISTS internal_messages (id SERIAL PRIMARY KEY, sender_name TEXT, target_name TEXT, msg_text TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS reply_to_id INTEGER REFERENCES internal_messages(id) ON DELETE SET NULL")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS forwarded_from_id INTEGER REFERENCES internal_messages(id) ON DELETE SET NULL")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS reactions JSONB DEFAULT '{}'::jsonb")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS mentions TEXT[] DEFAULT '{}'::text[]")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMP")
            await db.execute("ALTER TABLE internal_messages ADD COLUMN IF NOT EXISTS pinned_by TEXT")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_internal_messages_target_recent ON internal_messages (target_name, timestamp DESC, id DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_internal_messages_private_recent ON internal_messages (sender_name, target_name, timestamp DESC, id DESC)")
            await db.execute("CREATE TABLE IF NOT EXISTS bot_settings (id SERIAL PRIMARY KEY, greeting TEXT, menu TEXT, off_hours TEXT)")
            await db.execute("CREATE TABLE IF NOT EXISTS user_snippets (id SERIAL PRIMARY KEY, username TEXT REFERENCES system_users(username) ON DELETE CASCADE, shortcut TEXT, content TEXT)")
            await db.execute("CREATE TABLE IF NOT EXISTS calendar_events (id SERIAL PRIMARY KEY, user_name TEXT, event_date TEXT, note TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS phone TEXT DEFAULT ''")
            await db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS assigned_to TEXT DEFAULT ''")
            await db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS due_at TIMESTAMP")
            await db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pendente'")
            await db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS repeat_until_done BOOLEAN DEFAULT TRUE")
            await db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_due_status ON calendar_events (status, due_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_assigned_due ON calendar_events (assigned_to, due_at)")
            await db.execute("CREATE TABLE IF NOT EXISTS tags (id SERIAL PRIMARY KEY, name TEXT, color TEXT)")
            await db.execute("CREATE TABLE IF NOT EXISTS contact_tags (phone TEXT REFERENCES users(phone) ON DELETE CASCADE, tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(phone, tag_id))")
            await db.execute("CREATE TABLE IF NOT EXISTS contact_lists (id SERIAL PRIMARY KEY, name TEXT, color TEXT DEFAULT '#00a884', owner_name TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(name, owner_name))")
            await db.execute("CREATE TABLE IF NOT EXISTS contact_list_members (list_id INTEGER REFERENCES contact_lists(id) ON DELETE CASCADE, phone TEXT REFERENCES users(phone) ON DELETE CASCADE, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(list_id, phone))")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_contact_list_members_phone ON contact_list_members (phone)")
            await db.execute("INSERT INTO contact_lists (name, color, owner_name) VALUES ('Fornecedores', '#00a884', ''), ('Clientes', '#ff6600', ''), ('Revisar nome', '#f15c6d', ''), ('Herdados Flavia', '#64b5f6', ''), ('Herdados Kimberly', '#ba68c8', ''), ('Telefones revisar', '#ffd54f', '') ON CONFLICT (name, owner_name) DO NOTHING")
            await db.execute("CREATE TABLE IF NOT EXISTS metrics (id SERIAL PRIMARY KEY, seller_name TEXT, atendimentos INTEGER DEFAULT 0, vendas INTEGER DEFAULT 0, nao_vendas INTEGER DEFAULT 0, date TEXT, UNIQUE(seller_name, date))")
            await db.execute("ALTER TABLE metrics ADD COLUMN IF NOT EXISTS total_attendance_seconds BIGINT DEFAULT 0")
            await db.execute("ALTER TABLE metrics ADD COLUMN IF NOT EXISTS nao_vendas INTEGER DEFAULT 0")
            await db.execute("CREATE TABLE IF NOT EXISTS seller_status (seller_name TEXT PRIMARY KEY, status TEXT, last_update TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, actor_username TEXT, actor_name TEXT, actor_role TEXT, action TEXT, phone TEXT, target TEXT, details JSONB DEFAULT '{}'::jsonb, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs (created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs (action)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_name ON audit_logs (actor_name)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_phone ON audit_logs (phone)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_action ON audit_logs (created_at DESC, action)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_actor ON audit_logs (created_at DESC, actor_name)")
            await db.execute("CREATE TABLE IF NOT EXISTS status_posts (id SERIAL PRIMARY KEY, status_id TEXT UNIQUE, seller_name TEXT, session_id TEXT, status_type TEXT, preview TEXT, target_count INTEGER DEFAULT 0, filter_word TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS status_targets (status_id TEXT REFERENCES status_posts(status_id) ON DELETE CASCADE, target_jid TEXT, contact_name TEXT DEFAULT '', assigned_to TEXT DEFAULT '', sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, viewed_at TIMESTAMP, PRIMARY KEY(status_id, target_jid))")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_status_targets_status_viewed ON status_targets (status_id, viewed_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_status_targets_jid ON status_targets (target_jid)")
            await db.execute("CREATE TABLE IF NOT EXISTS status_views (status_id TEXT REFERENCES status_posts(status_id) ON DELETE CASCADE, viewer_jid TEXT, viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(status_id, viewer_jid))")
            
            await db.execute("INSERT INTO bot_settings (id, greeting, menu, off_hours) VALUES (1, 'Olá! Bem-vindo(a).', 'Menu Principal', 'Fora do horário.') ON CONFLICT (id) DO NOTHING")
            
            admin_user = settings.ADMIN_USERNAME
            admin_pass = settings.ADMIN_PASSWORD
            pwd_hash_bytes = await run_in_threadpool(bcrypt.hashpw, admin_pass.encode('utf-8'), bcrypt.gensalt())
            pwd_hash = pwd_hash_bytes.decode('utf-8')
            await db.execute("INSERT INTO system_users (username, password, role, name, is_active) VALUES ($1, $2, 'admin', 'Administrador', 1) ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password", admin_user, pwd_hash)

            # Fase 1 — Multi-tenancy: aplica tenants + tenant_id + RLS ao final,
            # sob o mesmo advisory lock, garantindo backfill do tenant padrão.
            await _migrate_multitenant(db)
            # Fase 9: tabelas globais de diferenciais (após tenants existir).
            await create_phase9_tables(db)
        finally:
            await db.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_ID)

async def close_db():
    """Fecha o pool global do PostgreSQL no encerramento da aplicação."""
    global pool
    if pool:
        await pool.close()
        logger.info("Conexão com PostgreSQL encerrada.")

async def health_check():
    """Executa uma consulta simples para validar o pool PostgreSQL."""
    try:
        value = await pool.fetchval("SELECT 1")
        return {"ok": value == 1}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

async def get_user(phone: str):
    """Busca um contato pelo identificador WhatsApp/JID."""
    row = await pool.fetchrow("SELECT * FROM users WHERE phone = $1", phone)
    return dict(row) if row else None

async def create_user(phone: str, session_id: str = "vendas", name: str = None, assigned_to: str = None, stage: str = "menu_wait"):
    """Cria um contato sem sobrescrever registros existentes."""
    await pool.execute("INSERT INTO users (phone, name, stage, assigned_to, session_id) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (phone) DO NOTHING", phone, name, stage, assigned_to, session_id)

async def upsert_synced_contact(phone: str, session_id: str, name: str = "", assigned_to: str = None, stage: str = "carteira"):
    """Sincroniza contato da agenda sem sobrescrever nome/responsável editados manualmente."""
    existing = await get_user(phone)
    if existing:
        current_name = existing.get("name")
        if name and (not current_name or current_name == "None"):
            await update_contact_details(phone, name, existing.get("notes", ""), existing.get("extra_info", ""))
            return {"inserted": False, "filled_name": True}
        return {"inserted": False, "filled_name": False}
    await create_user(phone, session_id, name or "", assigned_to, stage)
    return {"inserted": True, "filled_name": bool(name)}

async def update_user_stage(phone: str, stage: str, assigned_to: str = None):
    """Atualiza etapa do funil e, quando aplicável, o responsável."""
    if assigned_to:
        if stage == 'em_atendimento':
            await pool.execute("UPDATE users SET stage = $1, assigned_to = $2, attendance_start = CURRENT_TIMESTAMP WHERE phone = $3", stage, assigned_to, phone)
        else:
            await pool.execute("UPDATE users SET stage = $1, assigned_to = $2 WHERE phone = $3", stage, assigned_to, phone)
    else:
        if stage == 'em_atendimento':
            await pool.execute("UPDATE users SET stage = $1, attendance_start = CURRENT_TIMESTAMP WHERE phone = $2", stage, phone)
        else:
            await pool.execute("UPDATE users SET stage = $1 WHERE phone = $2", stage, phone)

async def update_user_session(phone: str, session_id: str):
    """Atualiza a instância Evolution responsável por um contato."""
    await pool.execute("UPDATE users SET session_id = $1 WHERE phone = $2", session_id, phone)

async def update_last_interaction(phone: str):
    """Marca o horário da última interação recebida de um contato."""
    await pool.execute("UPDATE users SET last_interaction = CURRENT_TIMESTAMP WHERE phone = $1", phone)

async def update_contact_details(phone: str, name: str, notes: str, extra: str):
    """Atualiza dados operacionais visíveis na ficha do contato."""
    await pool.execute("UPDATE users SET name = $1, notes = $2, extra_info = $3 WHERE phone = $4", name, notes, extra, phone)

async def get_contact_details(phone: str):
    """Busca cadastro do contato com tags e listas para edição em painéis."""
    row = await pool.fetchrow(
        """
        SELECT u.*,
            (SELECT string_agg(t.id || '|' || t.name || '|' || t.color, ',') FROM contact_tags ct JOIN tags t ON ct.tag_id = t.id WHERE ct.phone = u.phone) as tags_data,
            (SELECT string_agg(cl.id || '|' || cl.name || '|' || cl.color, ',') FROM contact_list_members clm JOIN contact_lists cl ON cl.id = clm.list_id WHERE clm.phone = u.phone) as list_data
        FROM users u
        WHERE u.phone = $1
        """,
        phone
    )
    return dict(row) if row else None

async def pin_message(phone: str, text: str):
    """Salva uma mensagem fixada no cadastro do contato."""
    await pool.execute("UPDATE users SET pinned_message = $1 WHERE phone = $2", text, phone)

async def unpin_message(phone: str):
    """Remove a mensagem fixada do contato."""
    await pool.execute("UPDATE users SET pinned_message = NULL WHERE phone = $1", phone)

def contact_bucket_condition(bucket: str) -> str:
    """Retorna a condição SQL de um depósito virtual de contatos."""
    bucket = (bucket or "").strip().lower()
    if bucket in ("fornecedores", "fornecedor"):
        return """
            AND EXISTS (
                SELECT 1 FROM contact_list_members clm_bucket
                JOIN contact_lists cl_bucket ON cl_bucket.id = clm_bucket.list_id
                WHERE clm_bucket.phone = u.phone AND cl_bucket.name ILIKE '%fornecedor%'
            )
        """
    if bucket == "listas":
        return "AND EXISTS (SELECT 1 FROM contact_list_members clm_bucket WHERE clm_bucket.phone = u.phone)"
    if bucket in ("sem_responsavel", "sem-responsavel"):
        return "AND (u.assigned_to IS NULL OR TRIM(u.assigned_to) = '' OR u.assigned_to = 'Não Atribuído')"
    if bucket in ("revisar_numero", "revisar-numero"):
        return "AND u.phone !~ '^55[0-9]{10,11}@s\\.whatsapp\\.net$'"
    if bucket in ("revisar_nome", "revisar-nome"):
        return """
            AND (
                u.name IS NULL
                OR TRIM(u.name) = ''
                OR u.name ~* '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)[[:space:]_-]*'
                OR EXISTS (
                    SELECT 1 FROM contact_list_members clm_bucket
                    JOIN contact_lists cl_bucket ON cl_bucket.id = clm_bucket.list_id
                    WHERE clm_bucket.phone = u.phone AND cl_bucket.name ILIKE 'Revisar%'
                )
            )
        """
    return ""

async def get_clients(role: str, user_name: str, tab: str = 'home', search: str = "", limit: int = 100, offset: int = 0, list_id: int = 0, broaden_search: bool = False, order_mode: str = "", bucket: str = "", initial: str = ""):
    """Lista contatos com escopo por papel, busca ampliada, listas, depósitos, inicial e ordenação."""
    base_query = """
        SELECT u.*,
            (SELECT string_agg(t.id || '|' || t.name || '|' || t.color, ',') FROM contact_tags ct JOIN tags t ON ct.tag_id = t.id WHERE ct.phone = u.phone) as tags_data,
            (SELECT string_agg(cl.id || '|' || cl.name || '|' || cl.color, ',') FROM contact_list_members clm JOIN contact_lists cl ON cl.id = clm.list_id WHERE clm.phone = u.phone) as list_data
        FROM users u
    """
    params = []
    idx = 1
    if tab == 'home':
        if role == 'admin':
            base_query += " WHERE u.stage IN ('pendente_assumir', 'em_atendimento')"
        elif role.startswith('vendas'):
            base_query += f" WHERE u.stage IN ('pendente_assumir', 'em_atendimento') AND u.assigned_to = ${idx}"
            params.append(user_name)
            idx += 1
        else:
            base_query += f" WHERE u.stage IN ('pendente_assumir', 'em_atendimento') AND (u.assigned_to = ${idx} OR u.assigned_to = ${idx+1})"
            params.extend([user_name, role])
            idx += 2
    else:
        if broaden_search:
            base_query += " WHERE u.stage IN ('carteira', 'pendente_assumir', 'em_atendimento')"
            if role != 'admin':
                if role.startswith('vendas'):
                    base_query += f" AND u.assigned_to = ${idx}"
                    params.append(user_name)
                    idx += 1
                else:
                    base_query += f" AND (u.assigned_to = ${idx} OR u.assigned_to = ${idx+1})"
                    params.extend([user_name, role])
                    idx += 2
        elif role == 'admin':
            base_query += " WHERE u.stage = 'carteira'"
        elif role.startswith('vendas'):
            base_query += f" WHERE u.stage = 'carteira' AND u.assigned_to = ${idx}"
            params.append(user_name)
            idx += 1
        else:
            base_query += f" WHERE u.stage = 'carteira' AND u.assigned_to = ${idx}"
            params.append(user_name)
            idx += 1
    if list_id:
        base_query += f" AND EXISTS (SELECT 1 FROM contact_list_members clm_filter WHERE clm_filter.phone = u.phone AND clm_filter.list_id = ${idx})"
        params.append(list_id)
        idx += 1
    if bucket:
        base_query += contact_bucket_condition(bucket)
    initial = (initial or "").strip().upper()[:1]
    clean_initial_expr = """
        LEFT(
            UPPER(
                translate(
                    regexp_replace(COALESCE(NULLIF(u.name, ''), u.phone, ''), '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)\\s*[-_]?\\s*', '', 'i'),
                    'ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ',
                    'AAAAAEEEEIIIIOOOOOUUUUC'
                )
            ),
            1
        )
    """
    if initial:
        if initial == "#":
            base_query += f" AND {clean_initial_expr} !~ '^[A-Z]$'"
        elif re.match(r"^[A-Z]$", initial):
            base_query += f" AND {clean_initial_expr} = ${idx}"
            params.append(initial)
            idx += 1
    if search:
        search_like = f"%{search}%"
        cleaned_digits = re.sub(r"\D", "", search or "")
        phone_conditions = ""
        params.append(search_like)
        text_idx = idx
        idx += 1
        if cleaned_digits:
            phone_like = f"%{cleaned_digits.lstrip('0')}%"
            params.append(phone_like)
            phone_idx = idx
            idx += 1
            phone_conditions = f"""
                OR regexp_replace(u.phone, '\\D', '', 'g') LIKE ${phone_idx}
                OR regexp_replace(regexp_replace(u.phone, '\\D', '', 'g'), '^55', '') LIKE ${phone_idx}
                OR regexp_replace(regexp_replace(regexp_replace(u.phone, '\\D', '', 'g'), '^55', ''), '^0+', '') LIKE ${phone_idx}
            """
        base_query += f"""
            AND (
                u.name ILIKE ${text_idx}
                OR regexp_replace(COALESCE(u.name, ''), '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)\\s*[-_]?\\s*', '', 'i') ILIKE ${text_idx}
                OR u.phone ILIKE ${text_idx}
                OR COALESCE(u.notes, '') ILIKE ${text_idx}
                OR COALESCE(u.extra_info, '') ILIKE ${text_idx}
                OR EXISTS (
                    SELECT 1 FROM contact_tags ct_s
                    JOIN tags t_s ON t_s.id = ct_s.tag_id
                    WHERE ct_s.phone = u.phone AND t_s.name ILIKE ${text_idx}
                )
                OR EXISTS (
                    SELECT 1 FROM contact_list_members clm_s
                    JOIN contact_lists cl_s ON cl_s.id = clm_s.list_id
                    WHERE clm_s.phone = u.phone AND cl_s.name ILIKE ${text_idx}
                )
                {phone_conditions}
            )
        """
    if order_mode == "audit":
        base_query += f"""
            ORDER BY
                CASE
                    WHEN u.stage = 'pendente_assumir' THEN 0
                    WHEN u.stage = 'em_atendimento' THEN 1
                    ELSE 2
                END,
                LOWER(regexp_replace(COALESCE(u.name, u.phone, ''), '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)\\s*[-_]?\\s*', '', 'i')) ASC,
                u.phone
            LIMIT ${idx} OFFSET ${idx+1}
        """
    elif tab == 'home' or broaden_search:
        base_query += f"""
            ORDER BY
                CASE WHEN u.stage = 'pendente_assumir' THEN 0 ELSE 1 END,
                CASE
                    WHEN u.stage = 'pendente_assumir' THEN COALESCE(u.last_interaction, u.attendance_start)
                    WHEN u.stage = 'em_atendimento' THEN COALESCE(u.attendance_start, u.last_interaction)
                    ELSE COALESCE(u.last_interaction, u.attendance_start)
                END ASC NULLS LAST,
                u.phone
            LIMIT ${idx} OFFSET ${idx+1}
        """
    else:
        base_query += f"""
            ORDER BY
                LOWER(regexp_replace(COALESCE(NULLIF(u.name, ''), u.phone, ''), '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)\\s*[-_]?\\s*', '', 'i')) ASC,
                u.phone
            LIMIT ${idx} OFFSET ${idx+1}
        """
    params.extend([limit, offset])
    rows = await pool.fetch(base_query, *params)
    return [dict(row) for row in rows]

async def get_contact_bucket_summary(role: str, user_name: str):
    """Conta a carteira do usuário em depósitos operacionais para a tela de contatos."""
    where_clause = "WHERE u.stage = 'carteira'"
    params = []
    if role != "admin":
        params.append(user_name)
        where_clause += f" AND u.assigned_to = $1"
    row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*) AS carteira,
            COUNT(*) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM contact_list_members clm
                    JOIN contact_lists cl ON cl.id = clm.list_id
                    WHERE clm.phone = u.phone AND cl.name ILIKE '%fornecedor%'
                )
            ) AS fornecedores,
            COUNT(*) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM contact_list_members clm
                    WHERE clm.phone = u.phone
                )
            ) AS listas,
            COUNT(*) FILTER (
                WHERE u.assigned_to IS NULL OR TRIM(u.assigned_to) = '' OR u.assigned_to = 'Não Atribuído'
            ) AS sem_responsavel,
            COUNT(*) FILTER (
                WHERE u.phone !~ '^55[0-9]{{10,11}}@s\\.whatsapp\\.net$'
            ) AS revisar_numero,
            COUNT(*) FILTER (
                WHERE u.name IS NULL
                   OR TRIM(u.name) = ''
                   OR u.name ~* '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)[[:space:]_-]*'
                   OR EXISTS (
                        SELECT 1 FROM contact_list_members clm
                        JOIN contact_lists cl ON cl.id = clm.list_id
                        WHERE clm.phone = u.phone AND cl.name ILIKE 'Revisar%'
                   )
            ) AS revisar_nome
        FROM users u
        {where_clause}
        """,
        *params
    )
    return dict(row) if row else {
        "carteira": 0,
        "fornecedores": 0,
        "listas": 0,
        "sem_responsavel": 0,
        "revisar_numero": 0,
        "revisar_nome": 0,
    }

async def save_message(phone: str, sender: str, text: str, msg_type: str, waha_id: str, ack: int, session_id: str = "vendas"):
    """Persiste uma mensagem do chat evitando duplicar o mesmo ID remoto."""
    if waha_id:
        status = await pool.execute(
            """
            INSERT INTO messages (phone, sender, msg_text, msg_type, waha_id, ack, session_id)
            SELECT $1, $2, $3, $4, $5, $6, $7
            WHERE NOT EXISTS (
                SELECT 1 FROM messages WHERE phone = $1 AND waha_id = $5
            )
            """,
            phone,
            sender,
            text,
            msg_type,
            waha_id,
            ack,
            session_id,
        )
    else:
        status = await pool.execute(
            "INSERT INTO messages (phone, sender, msg_text, msg_type, waha_id, ack, session_id) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            phone,
            sender,
            text,
            msg_type,
            waha_id,
            ack,
            session_id,
        )
    return status.endswith(" 1")

async def claim_webhook_message(message_id: str, phone: str, session_id: str, event_type: str = "messages.upsert") -> bool:
    """Registra um ID remoto de webhook e retorna falso quando ele já foi processado."""
    if not message_id:
        return True
    row = await pool.fetchrow(
        """
        INSERT INTO processed_webhook_messages (message_id, phone, session_id, event_type)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING message_id
        """,
        message_id,
        phone or "",
        session_id or "",
        event_type or "",
    )
    return bool(row)

async def get_messages(phone: str):
    """Retorna o histórico completo de mensagens de um contato."""
    rows = await pool.fetch("SELECT * FROM messages WHERE phone = $1 ORDER BY timestamp ASC", phone)
    return [dict(row) for row in rows]

async def get_message_by_id(msg_id: int):
    """Busca uma mensagem local pelo identificador numérico."""
    row = await pool.fetchrow("SELECT * FROM messages WHERE id = $1", msg_id)
    return dict(row) if row else None

async def get_message_by_waha_id(phone: str, waha_id: str):
    """Busca uma mensagem local pelo ID retornado pela Evolution API."""
    row = await pool.fetchrow("SELECT * FROM messages WHERE phone = $1 AND waha_id = $2", phone, waha_id)
    return dict(row) if row else None

async def update_message_text_by_waha_id(phone: str, waha_id: str, new_text: str):
    """Atualiza localmente o texto de uma mensagem já editada no WhatsApp."""
    await pool.execute("UPDATE messages SET msg_text = $1 WHERE phone = $2 AND waha_id = $3", new_text, phone, waha_id)

async def mark_message_deleted_by_waha_id(phone: str, waha_id: str):
    """Marca uma mensagem como apagada mantendo rastro de auditoria no histórico."""
    await pool.execute("UPDATE messages SET msg_text = 'Mensagem apagada para todos.', msg_type = 'deleted' WHERE phone = $1 AND waha_id = $2", phone, waha_id)

async def update_message_ack(waha_id: str, ack: int):
    """Atualiza o status de entrega/leitura sem regredir ACK já maior."""
    await pool.execute("UPDATE messages SET ack = $1 WHERE waha_id = $2 AND ack < $1", ack, waha_id)

async def finish_attendance(phone: str, seller_name: str, result: str):
    """Finaliza atendimento, devolve à carteira e incrementa métricas com TMA real."""
    duration_row = await pool.fetchrow(
        """
        SELECT GREATEST(0, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - attendance_start)))::BIGINT AS duration
        FROM users
        WHERE phone = $1 AND attendance_start IS NOT NULL
        """,
        phone
    )
    duration_seconds = duration_row["duration"] if duration_row else 0
    await pool.execute("UPDATE users SET stage = 'carteira', assigned_to = $1, attendance_start = NULL WHERE phone = $2", seller_name, phone)
    today = current_business_date()
    await pool.execute("INSERT INTO metrics (seller_name, date) VALUES ($1, $2) ON CONFLICT (seller_name, date) DO NOTHING", seller_name, today)
    normalized_result = (result or "").strip().lower()
    sold_results = {'vendeu', 'com_venda', 'finalizado_com_venda'}
    if normalized_result in sold_results:
        await pool.execute("UPDATE metrics SET atendimentos = atendimentos + 1, vendas = vendas + 1, total_attendance_seconds = total_attendance_seconds + $3 WHERE seller_name = $1 AND date = $2", seller_name, today, duration_seconds)
    else:
        await pool.execute("UPDATE metrics SET atendimentos = atendimentos + 1, nao_vendas = nao_vendas + 1, total_attendance_seconds = total_attendance_seconds + $3 WHERE seller_name = $1 AND date = $2", seller_name, today, duration_seconds)

async def get_seller_status(seller_name: str):
    """Consulta o status atual de disponibilidade de um operador."""
    row = await pool.fetchrow("SELECT status FROM seller_status WHERE seller_name = $1", seller_name)
    return row['status'] if row else 'offline'

async def set_seller_status(seller_name: str, status: str):
    """Grava status online/offline de um operador."""
    await pool.execute("INSERT INTO seller_status (seller_name, status, last_update) VALUES ($1, $2, CURRENT_TIMESTAMP) ON CONFLICT (seller_name) DO UPDATE SET status = EXCLUDED.status, last_update = CURRENT_TIMESTAMP", seller_name, status)

async def get_all_active_sellers():
    """Lista nomes de operadores marcados como online."""
    rows = await pool.fetch("SELECT seller_name FROM seller_status WHERE status = 'online'")
    return [row['seller_name'] for row in rows]

async def get_transfer_targets():
    """Lista operadores ativos para transferencia, incluindo status operacional."""
    rows = await pool.fetch("""
        SELECT su.name, su.role, COALESCE(ss.status, 'offline') AS status
        FROM system_users su
        LEFT JOIN seller_status ss ON ss.seller_name = su.name
        WHERE su.is_active = 1
          AND su.role IN ('vendas_1', 'vendas_2', 'financeiro', 'engenharia')
        ORDER BY
            CASE COALESCE(ss.status, 'offline')
                WHEN 'online' THEN 1
                WHEN 'ausente' THEN 2
                ELSE 3
            END,
            su.name
    """)
    return [dict(row) for row in rows]

async def get_all_system_users():
    """Retorna usuários internos cadastrados em ordem alfabética para painéis admin."""
    rows = await pool.fetch("SELECT * FROM system_users ORDER BY LOWER(name), name")
    return [dict(row) for row in rows]

async def get_sales_users():
    """Retorna vendedoras ativas usadas em divisão de carteira."""
    rows = await pool.fetch(
        """
        SELECT username, name, role
        FROM system_users
        WHERE role IN ('vendas_1', 'vendas_2') AND COALESCE(is_active, 1) = 1
        ORDER BY role, name
        """
    )
    return [dict(row) for row in rows]

async def get_system_user(username: str):
    """Busca um usuário interno pelo login."""
    row = await pool.fetchrow("SELECT * FROM system_users WHERE username = $1", username)
    return dict(row) if row else None

async def upsert_system_user(username: str, password_hash: str, role: str, name: str, is_active: int):
    """Cria ou atualiza usuário interno, preservando senha quando vazia."""
    if password_hash:
        await pool.execute("INSERT INTO system_users (username, password, role, name, is_active) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (username) DO UPDATE SET password=EXCLUDED.password, role=EXCLUDED.role, name=EXCLUDED.name, is_active=EXCLUDED.is_active", username, password_hash, role, name, is_active)
    else:
        await pool.execute("UPDATE system_users SET role = $1, name = $2, is_active = $3 WHERE username = $4", role, name, is_active, username)

async def rename_seller_status(old_name: str, new_name: str):
    """Migra o status operacional quando o nome exibido do usuario muda."""
    if old_name and new_name and old_name != new_name:
        await pool.execute(
            """
            INSERT INTO seller_status (seller_name, status, last_update)
            SELECT $1, status, last_update FROM seller_status WHERE seller_name = $2
            ON CONFLICT (seller_name) DO UPDATE
            SET status = EXCLUDED.status, last_update = EXCLUDED.last_update
            """,
            new_name,
            old_name
        )
        await pool.execute("DELETE FROM seller_status WHERE seller_name = $1", old_name)

async def ensure_seller_status(seller_name: str, default_status: str = "online"):
    """Garante registro de presenca para operador ativo sem sobrescrever status atual."""
    await pool.execute(
        """
        INSERT INTO seller_status (seller_name, status, last_update)
        VALUES ($1, $2, CURRENT_TIMESTAMP)
        ON CONFLICT (seller_name) DO NOTHING
        """,
        seller_name,
        default_status
    )

async def update_user_profile(username: str, name: str, bio: str, avatar: str, bg_color: str, primary_color: str, text_color: str):
    """Atualiza perfil visual e dados pessoais do usuário interno."""
    await pool.execute("UPDATE system_users SET name = $1, bio = $2, avatar = $3, bg_color = $4, primary_color = $5, text_color = $6 WHERE username = $7", name, bio, avatar, bg_color, primary_color, text_color, username)

async def get_bot_settings():
    """Obtém textos configuráveis do bot de atendimento."""
    row = await pool.fetchrow("SELECT * FROM bot_settings WHERE id = 1")
    return dict(row) if row else {}

async def update_bot_setting(field: str, value: str):
    """Atualiza um campo textual de configuração do bot."""
    await pool.execute(f"UPDATE bot_settings SET {field} = $1 WHERE id = 1", value)

async def get_admin_reports():
    """Monta indicadores diários por operador em ordem alfabética para o termômetro."""
    today = current_business_date()
    reports = {}
    users = await pool.fetch("SELECT username, role, name FROM system_users WHERE role != 'admin' ORDER BY LOWER(name), name")
    for u in users:
        name = u['name']
        m_row = await pool.fetchrow("SELECT vendas, atendimentos, nao_vendas, total_attendance_seconds FROM metrics WHERE seller_name = $1 AND date = $2", name, today)
        vendas = (m_row['vendas'] or 0) if m_row else 0
        atends = (m_row['atendimentos'] or 0) if m_row else 0
        nao_vendas = (m_row['nao_vendas'] or 0) if m_row else 0
        total_seconds = (m_row['total_attendance_seconds'] or 0) if m_row else 0
        avg_seconds = (total_seconds / atends) if atends else 0
        a_row = await pool.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE stage = 'em_atendimento') as ativos,
                COUNT(*) FILTER (WHERE stage = 'pendente_assumir') as aguardando
            FROM users
            WHERE assigned_to IN ($1, $2, $3)
              AND stage IN ('em_atendimento', 'pendente_assumir')
            """,
            name,
            u["role"],
            u["username"],
        )
        ativos = (a_row['ativos'] or 0) if a_row else 0
        aguardando = (a_row['aguardando'] or 0) if a_row else 0
        status = await get_seller_status(name)
        reports[name] = {
            "role": u["role"],
            "hoje": vendas,
            "nao_vendas": nao_vendas,
            "tma": format_duration(avg_seconds),
            "ativos": ativos,
            "aguardando": aguardando,
            "status": status.upper(),
        }
    return reports

async def get_global_metrics():
    """Calcula métricas agregadas exibidas no painel admin."""
    today = current_business_date()
    row = await pool.fetchrow("SELECT SUM(vendas) as v, SUM(atendimentos) as a, SUM(nao_vendas) as nv, SUM(total_attendance_seconds) as s FROM metrics WHERE date = $1", today)
    vendas = row['v'] if row and row['v'] else 0
    atends = row['a'] if row and row['a'] else 0
    nao_vendas = row['nv'] if row and row['nv'] else 0
    total_seconds = row['s'] if row and row['s'] else 0
    cur2 = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE stage = 'em_atendimento') as ativos,
            COUNT(*) FILTER (WHERE stage = 'pendente_assumir') as aguardando
        FROM users
        WHERE stage IN ('pendente_assumir', 'em_atendimento')
        """
    )
    ativos = (cur2['ativos'] or 0) if cur2 else 0
    aguardando = (cur2['aguardando'] or 0) if cur2 else 0
    cur3 = await pool.fetchrow("SELECT COUNT(*) as c FROM users")
    total = cur3['c']
    return {
        "vendas_hoje": vendas,
        "atendimentos_hoje": atends,
        "nao_vendas_hoje": nao_vendas,
        "tma": format_duration(total_seconds / atends if atends else 0),
        "ativos": ativos + aguardando,
        "em_atendimento": ativos,
        "aguardando": aguardando,
        "total_contatos": total,
    }

def _period_start(days: int) -> str:
    """Calcula a data inicial inclusiva para relatórios por janela móvel."""
    return (datetime.now(BR_TZ) - timedelta(days=days - 1)).strftime('%Y-%m-%d')

async def _metric_period_summary(seller_name: str = "", days: int = 7):
    """Resume vendas, finalizações e TMA para um usuário ou para a equipe."""
    start_date = _period_start(days)
    if seller_name:
        row = await pool.fetchrow(
            """
            SELECT
                COALESCE(SUM(vendas), 0) as vendas,
                COALESCE(SUM(nao_vendas), 0) as nao_vendas,
                COALESCE(SUM(atendimentos), 0) as atendimentos,
                COALESCE(SUM(total_attendance_seconds), 0) as total_seconds
            FROM metrics
            WHERE seller_name = $1 AND date >= $2
            """,
            seller_name,
            start_date,
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT
                COALESCE(SUM(vendas), 0) as vendas,
                COALESCE(SUM(nao_vendas), 0) as nao_vendas,
                COALESCE(SUM(atendimentos), 0) as atendimentos,
                COALESCE(SUM(total_attendance_seconds), 0) as total_seconds
            FROM metrics
            WHERE date >= $1
            """,
            start_date,
        )
    vendas = row["vendas"] or 0
    nao_vendas = row["nao_vendas"] or 0
    atendimentos = row["atendimentos"] or 0
    total_seconds = row["total_seconds"] or 0
    conversion = round((vendas / atendimentos) * 100, 1) if atendimentos else 0
    return {
        "days": days,
        "since": start_date,
        "vendas": vendas,
        "nao_vendas": nao_vendas,
        "atendimentos": atendimentos,
        "tma": format_duration(total_seconds / atendimentos if atendimentos else 0),
        "conversion": conversion,
    }

async def _sold_clients_for_period(seller_name: str, days: int):
    """Lista clientes vendidos por usuário, tolerando formatos antigos dos logs."""
    start_date = _period_start(days)
    start_date_param = _date_param(start_date)
    try:
        rows = await pool.fetch(
            """
            SELECT
                al.phone,
                COALESCE(NULLIF(u.name, ''), al.phone) as contact_name,
                to_char(al.created_at, 'DD/MM/YYYY HH24:MI') as finished_at,
                COALESCE(al.details->>'result', al.details->>'resultado', '') as result
            FROM audit_logs al
            LEFT JOIN users u ON u.phone = al.phone
            WHERE al.action = 'chat_finished'
              AND (al.actor_name = $1 OR al.target = $1)
              AND al.created_at >= $2::date
              AND LOWER(COALESCE(al.details->>'result', al.details->>'resultado', '')) IN ('vendeu', 'com_venda', 'finalizado_com_venda', 'vendido', 'venda')
            ORDER BY al.created_at DESC
            """,
            seller_name,
            start_date_param,
        )
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.warning(f"Falha ao listar vendas detalhadas de {seller_name} em {days} dias: {exc}")
        return []

async def _metric_range_summary(seller_name: str = "", start_date: str = "", end_date: str = ""):
    """Resume métricas entre datas inclusivas para um usuário ou para a equipe."""
    if seller_name:
        row = await pool.fetchrow(
            """
            SELECT
                COALESCE(SUM(vendas), 0) as vendas,
                COALESCE(SUM(nao_vendas), 0) as nao_vendas,
                COALESCE(SUM(atendimentos), 0) as atendimentos,
                COALESCE(SUM(total_attendance_seconds), 0) as total_seconds
            FROM metrics
            WHERE seller_name = $1 AND date >= $2 AND date <= $3
            """,
            seller_name,
            start_date,
            end_date,
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT
                COALESCE(SUM(vendas), 0) as vendas,
                COALESCE(SUM(nao_vendas), 0) as nao_vendas,
                COALESCE(SUM(atendimentos), 0) as atendimentos,
                COALESCE(SUM(total_attendance_seconds), 0) as total_seconds
            FROM metrics
            WHERE date >= $1 AND date <= $2
            """,
            start_date,
            end_date,
        )
    vendas = row["vendas"] or 0
    nao_vendas = row["nao_vendas"] or 0
    atendimentos = row["atendimentos"] or 0
    total_seconds = row["total_seconds"] or 0
    return {
        "start_date": start_date,
        "end_date": end_date,
        "vendas": vendas,
        "nao_vendas": nao_vendas,
        "atendimentos": atendimentos,
        "tma": format_duration(total_seconds / atendimentos if atendimentos else 0),
        "conversion": round((vendas / atendimentos) * 100, 1) if atendimentos else 0,
        "total_attendance_seconds": total_seconds,
    }

async def _finished_clients_for_range(seller_name: str, start_date: str, end_date: str, sold_only: bool = False):
    """Lista finalizações do período mensal com cliente e resultado registrados em auditoria."""
    start_date_param = _date_param(start_date)
    end_date_param = _date_param(end_date)
    result_filter = ""
    if sold_only:
        result_filter = """
              AND LOWER(COALESCE(al.details->>'result', al.details->>'resultado', '')) IN
                  ('vendeu', 'com_venda', 'finalizado_com_venda', 'vendido', 'venda')
        """
    try:
        rows = await pool.fetch(
            f"""
            SELECT
                al.phone,
                COALESCE(NULLIF(u.name, ''), al.phone) as contact_name,
                to_char(al.created_at, 'DD/MM/YYYY HH24:MI') as finished_at,
                COALESCE(al.details->>'result', al.details->>'resultado', '') as result,
                al.actor_name,
                al.target
            FROM audit_logs al
            LEFT JOIN users u ON u.phone = al.phone
            WHERE al.action = 'chat_finished'
              AND (al.actor_name = $1 OR al.target = $1)
              AND al.created_at >= $2::date
              AND al.created_at < ($3::date + INTERVAL '1 day')
              {result_filter}
            ORDER BY al.created_at DESC
            """,
            seller_name,
            start_date_param,
            end_date_param,
        )
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.warning(
            "Falha ao listar finalizações mensais de {} entre {} e {}: {}",
            seller_name,
            start_date,
            end_date,
            exc,
        )
        return []

async def _audit_summary_for_range(start_date: str, end_date: str, actor_name: str = ""):
    """Agrupa logs do período por ação e ator para relatórios executivos mensais."""
    params = [_date_param(start_date), _date_param(end_date)]
    actor_clause = ""
    if actor_name:
        params.append(actor_name)
        actor_clause = "AND al.actor_name = $3"
    rows = await pool.fetch(
        f"""
        SELECT
            al.action,
            COALESCE(NULLIF(al.actor_name, ''), 'Sistema') as actor_name,
            COALESCE(NULLIF(al.actor_role, ''), '-') as actor_role,
            COUNT(*) as total
        FROM audit_logs al
        WHERE al.created_at >= $1::date
          AND al.created_at < ($2::date + INTERVAL '1 day')
          {actor_clause}
        GROUP BY al.action, COALESCE(NULLIF(al.actor_name, ''), 'Sistema'), COALESCE(NULLIF(al.actor_role, ''), '-')
        ORDER BY total DESC, al.action, actor_name
        """,
        *params,
    )
    by_action = {}
    by_actor = {}
    for row in rows:
        action = row["action"] or "unknown"
        actor = row["actor_name"] or "Sistema"
        total = row["total"] or 0
        by_action[action] = by_action.get(action, 0) + total
        actor_item = by_actor.setdefault(actor, {"actor_name": actor, "actor_role": row["actor_role"], "total": 0, "actions": {}})
        actor_item["total"] += total
        actor_item["actions"][action] = actor_item["actions"].get(action, 0) + total
    return {
        "by_action": by_action,
        "by_actor": sorted(by_actor.values(), key=lambda item: (-item["total"], item["actor_name"].lower())),
    }

async def _audit_events_for_range(start_date: str, end_date: str, actor_name: str = "", limit: int = 80):
    """Lista eventos recentes do período para detalhamento mensal por equipe ou usuário."""
    params = [_date_param(start_date), _date_param(end_date)]
    actor_clause = ""
    if actor_name:
        params.append(actor_name)
        actor_clause = f"AND al.actor_name = ${len(params)}"
    params.append(limit)
    rows = await pool.fetch(
        f"""
        SELECT
            al.action,
            al.actor_name,
            al.actor_role,
            al.phone,
            COALESCE(NULLIF(u.name, ''), al.phone, '') as contact_name,
            al.target,
            al.details,
            to_char(al.created_at, 'DD/MM/YYYY HH24:MI') as created_at_label
        FROM audit_logs al
        LEFT JOIN users u ON u.phone = al.phone
        WHERE al.created_at >= $1::date
          AND al.created_at < ($2::date + INTERVAL '1 day')
          {actor_clause}
        ORDER BY al.created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [dict(row) for row in rows]

async def _status_post_summary_for_range(start_date: str, end_date: str, seller_name: str = ""):
    """Conta status publicados no período calendário, opcionalmente filtrando por vendedor."""
    params = [_date_param(start_date), _date_param(end_date)]
    seller_clause = ""
    if seller_name:
        params.append(seller_name)
        seller_clause = "AND sp.seller_name = $3"
    row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(COALESCE(sp.target_count, 0)), 0) as targets
        FROM status_posts sp
        WHERE sp.created_at >= $1::date
          AND sp.created_at < ($2::date + INTERVAL '1 day')
          {seller_clause}
        """,
        *params,
    )
    return {
        "total": (row["total"] or 0) if row else 0,
        "targets": (row["targets"] or 0) if row else 0,
    }

async def get_monthly_team_report(year_month: str = ""):
    """Monta relatório mensal calendário da equipe com métricas e auditoria do período."""
    start_date, end_date = get_month_bounds(year_month)
    users = await pool.fetch(
        """
        SELECT username, role, name, COALESCE(is_active, 1) as is_active
        FROM system_users
        WHERE role != 'admin'
        ORDER BY LOWER(name), name
        """
    )
    user_reports = []
    for user in users:
        metrics = await _metric_range_summary(user["name"], start_date, end_date)
        status_posts = await _status_post_summary_for_range(start_date, end_date, user["name"])
        queues = await pool.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE stage = 'em_atendimento') as ativos,
                COUNT(*) FILTER (WHERE stage = 'pendente_assumir') as aguardando
            FROM users
            WHERE assigned_to IN ($1, $2, $3)
              AND stage IN ('em_atendimento', 'pendente_assumir')
            """,
            user["name"],
            user["role"],
            user["username"],
        )
        user_reports.append({
            "user": dict(user),
            "metrics": metrics,
            "status_posts": status_posts,
            "queues": {
                "ativos": (queues["ativos"] or 0) if queues else 0,
                "aguardando": (queues["aguardando"] or 0) if queues else 0,
            },
        })
    return {
        "month": start_date[:7],
        "start_date": start_date,
        "end_date": end_date,
        "team": await _metric_range_summary("", start_date, end_date),
        "users": user_reports,
        "audit": await _audit_summary_for_range(start_date, end_date),
        "status_posts": await _status_post_summary_for_range(start_date, end_date),
        "recent_events": await _audit_events_for_range(start_date, end_date, limit=300),
    }

async def get_user_monthly_performance(seller_name: str, year_month: str = ""):
    """Retorna desempenho mensal calendário individual com detalhes de auditoria."""
    start_date, end_date = get_month_bounds(year_month)
    user = await pool.fetchrow(
        "SELECT username, role, name, COALESCE(is_active, 1) as is_active FROM system_users WHERE name = $1",
        seller_name,
    )
    if not user:
        return None
    queues = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE stage = 'em_atendimento') as ativos,
            COUNT(*) FILTER (WHERE stage = 'pendente_assumir') as aguardando
        FROM users
        WHERE assigned_to IN ($1, $2, $3)
          AND stage IN ('em_atendimento', 'pendente_assumir')
        """,
        user["name"],
        user["role"],
        user["username"],
    )
    finished = await _finished_clients_for_range(seller_name, start_date, end_date)
    sales = [
        item for item in finished
        if (item.get("result") or "").strip().lower() in {'vendeu', 'com_venda', 'finalizado_com_venda', 'vendido', 'venda'}
    ]
    no_sales = [item for item in finished if item not in sales]
    return {
        "month": start_date[:7],
        "start_date": start_date,
        "end_date": end_date,
        "user": dict(user),
        "metrics": await _metric_range_summary(seller_name, start_date, end_date),
        "team": await _metric_range_summary("", start_date, end_date),
        "queues": {
            "ativos": (queues["ativos"] or 0) if queues else 0,
            "aguardando": (queues["aguardando"] or 0) if queues else 0,
        },
        "sales": sales,
        "no_sales": no_sales,
        "finished_chats": finished,
        "status_posts": await _status_post_summary_for_range(start_date, end_date, seller_name),
        "audit": await _audit_summary_for_range(start_date, end_date, actor_name=seller_name),
        "recent_events": await _audit_events_for_range(start_date, end_date, actor_name=seller_name, limit=180),
    }

async def get_user_performance_report(seller_name: str, year_month: str = ""):
    """Monta relatório individual mantendo 7/15 dias móveis e mês calendário."""
    user = await pool.fetchrow(
        "SELECT username, role, name, COALESCE(is_active, 1) as is_active FROM system_users WHERE name = $1",
        seller_name,
    )
    if not user:
        return None
    periods = {
        "weekly": 7,
        "fortnightly": 15,
    }
    user_periods = {}
    team_periods = {}
    sold_clients = {}
    for key, days in periods.items():
        user_periods[key] = await _metric_period_summary(seller_name, days)
        team_periods[key] = await _metric_period_summary("", days)
        sold_clients[key] = await _sold_clients_for_period(seller_name, days)
    monthly_report = await get_user_monthly_performance(seller_name, year_month)
    user_periods["monthly"] = monthly_report["metrics"]
    team_periods["monthly"] = monthly_report["team"]
    sold_clients["monthly"] = monthly_report["sales"]
    queues = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE stage = 'em_atendimento') as ativos,
            COUNT(*) FILTER (WHERE stage = 'pendente_assumir') as aguardando
        FROM users
        WHERE assigned_to IN ($1, $2, $3)
          AND stage IN ('em_atendimento', 'pendente_assumir')
        """,
        user["name"],
        user["role"],
        user["username"],
    )
    return {
        "user": dict(user),
        "month": monthly_report["month"],
        "month_start": monthly_report["start_date"],
        "month_end": monthly_report["end_date"],
        "periods": user_periods,
        "team": team_periods,
        "sales": sold_clients,
        "monthly": monthly_report,
        "queues": {
            "ativos": (queues["ativos"] or 0) if queues else 0,
            "aguardando": (queues["aguardando"] or 0) if queues else 0,
        },
    }

async def get_attendance_breakdown_by_user():
    """Agrupa atendimentos ativos e aguardando por todos os usuários do sistema."""
    rows = await pool.fetch(
        """
        SELECT
            su.username,
            su.name,
            su.role,
            COALESCE(su.is_active, 1) as is_active,
            COUNT(u.phone) FILTER (WHERE u.stage = 'em_atendimento') as ativos,
            COUNT(u.phone) FILTER (WHERE u.stage = 'pendente_assumir') as aguardando
        FROM system_users su
        LEFT JOIN users u
          ON u.stage IN ('em_atendimento', 'pendente_assumir')
         AND u.assigned_to IN (su.name, su.role, su.username)
        GROUP BY su.username, su.name, su.role, su.is_active
        ORDER BY
            CASE WHEN COALESCE(su.is_active, 1) = 1 THEN 0 ELSE 1 END,
            CASE su.role
                WHEN 'vendas_1' THEN 0
                WHEN 'vendas_2' THEN 1
                WHEN 'financeiro' THEN 2
                WHEN 'engenharia' THEN 3
                WHEN 'admin' THEN 4
                ELSE 5
            END,
            su.name
        """
    )
    unassigned = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE stage = 'em_atendimento') as ativos,
            COUNT(*) FILTER (WHERE stage = 'pendente_assumir') as aguardando
        FROM users
        WHERE stage IN ('em_atendimento', 'pendente_assumir')
          AND (assigned_to IS NULL OR assigned_to = '')
        """
    )
    users = []
    totals = {"ativos": 0, "aguardando": 0, "total": 0}
    for row in rows:
        item = dict(row)
        item["ativos"] = item.get("ativos") or 0
        item["aguardando"] = item.get("aguardando") or 0
        item["total"] = item["ativos"] + item["aguardando"]
        totals["ativos"] += item["ativos"]
        totals["aguardando"] += item["aguardando"]
        totals["total"] += item["total"]
        users.append(item)

    unassigned_item = {
        "username": "",
        "name": "Sem responsável",
        "role": "triagem",
        "is_active": 1,
        "ativos": (unassigned["ativos"] or 0) if unassigned else 0,
        "aguardando": (unassigned["aguardando"] or 0) if unassigned else 0,
    }
    unassigned_item["total"] = unassigned_item["ativos"] + unassigned_item["aguardando"]
    totals["ativos"] += unassigned_item["ativos"]
    totals["aguardando"] += unassigned_item["aguardando"]
    totals["total"] += unassigned_item["total"]

    return {"users": users, "unassigned": unassigned_item, "totals": totals}

async def save_audit_log(actor: dict, action: str, phone: str = "", target: str = "", details: dict = None):
    """Registra uma acao operacional da equipe para auditoria administrativa."""
    details_json = json.dumps(details or {}, ensure_ascii=False, default=str)
    await pool.execute(
        """
        INSERT INTO audit_logs (actor_username, actor_name, actor_role, action, phone, target, details)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        """,
        actor.get("username", "") if actor else "",
        actor.get("name", "") if actor else "",
        actor.get("role", "") if actor else "",
        action,
        phone or "",
        target or "",
        details_json
    )

async def get_audit_logs(
    action: str = "",
    actor: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    category_actions: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
):
    """Lista logs operacionais; converte datas `YYYY-MM-DD` para `date` do asyncpg."""
    def parse_filter_date(value: str):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            logger.warning("Data inválida no filtro de logs: {}", value)
            return None

    query = """
        SELECT al.*, COALESCE(u.name, '') AS contact_name
        FROM audit_logs al
        LEFT JOIN users u ON u.phone = al.phone
        WHERE 1 = 1
    """
    params = []
    idx = 1
    if action:
        query += f" AND al.action = ${idx}"
        params.append(action)
        idx += 1
    elif category_actions:
        safe_actions = [str(item) for item in category_actions if item]
        if safe_actions:
            query += f" AND al.action = ANY(${idx})"
            params.append(safe_actions)
            idx += 1
        else:
            query += " AND 1 = 0"
    if actor:
        query += f" AND al.actor_name = ${idx}"
        params.append(actor)
        idx += 1
    parsed_date_from = parse_filter_date(date_from)
    parsed_date_to = parse_filter_date(date_to)
    if parsed_date_from:
        query += f" AND al.created_at >= ${idx}::date"
        params.append(parsed_date_from)
        idx += 1
    if parsed_date_to:
        query += f" AND al.created_at < (${idx}::date + INTERVAL '1 day')"
        params.append(parsed_date_to)
        idx += 1
    if search:
        query += f"""
            AND (
                al.actor_name ILIKE ${idx}
                OR al.phone ILIKE ${idx}
                OR al.target ILIKE ${idx}
                OR al.action ILIKE ${idx}
                OR al.details::text ILIKE ${idx}
                OR u.name ILIKE ${idx}
            )
        """
        params.append(f"%{search}%")
        idx += 1
    query += f" ORDER BY al.created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
    params.extend([limit, offset])
    rows = await pool.fetch(query, *params)
    return [dict(row) for row in rows]

async def get_seller_metrics(seller_name: str):
    """Calcula métricas do dia para um operador específico."""
    today = current_business_date()
    row = await pool.fetchrow("SELECT vendas, atendimentos, nao_vendas, total_attendance_seconds FROM metrics WHERE seller_name = $1 AND date = $2", seller_name, today)
    atends = (row['atendimentos'] or 0) if row else 0
    nao_vendas = (row['nao_vendas'] or 0) if row else 0
    total_seconds = (row['total_attendance_seconds'] or 0) if row else 0
    return {
        "vendas": (row['vendas'] or 0) if row else 0,
        "nao_vendas": nao_vendas,
        "atendimentos": atends,
        "tma": format_duration(total_seconds / atends if atends else 0),
    }

def _normalize_internal_message_rows(rows) -> list[dict]:
    """Normaliza metadados JSON/array das mensagens internas."""
    messages = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("reactions"), str):
            try:
                item["reactions"] = json.loads(item["reactions"])
            except json.JSONDecodeError:
                item["reactions"] = {}
        item["reactions"] = item.get("reactions") or {}
        item["mentions"] = item.get("mentions") or []
        messages.append(item)
    return messages

INTERNAL_MESSAGE_SELECT = """
        SELECT im.*,
               reply.sender_name AS reply_sender_name,
               reply.msg_text AS reply_msg_text,
               su.role AS sender_role
        FROM internal_messages im
        LEFT JOIN internal_messages reply ON reply.id = im.reply_to_id
        LEFT JOIN system_users su ON su.name = im.sender_name
    """

async def get_internal_messages(user_name: str, target: str):
    """Lista as 80 mensagens internas mais recentes com metadados de ações."""
    if target == 'geral':
        rows = await pool.fetch(
            f"""
            SELECT * FROM (
                {INTERNAL_MESSAGE_SELECT}
                WHERE im.target_name = 'geral'
                ORDER BY im.timestamp DESC, im.id DESC
                LIMIT 80
            ) recent
            ORDER BY timestamp ASC, id ASC
            """
        )
    else:
        rows = await pool.fetch(
            f"""
            SELECT * FROM (
                {INTERNAL_MESSAGE_SELECT}
                WHERE (im.sender_name = $1 AND im.target_name = $2)
                   OR (im.sender_name = $3 AND im.target_name = $4)
                ORDER BY im.timestamp DESC, im.id DESC
                LIMIT 80
            ) recent
            ORDER BY timestamp ASC, id ASC
            """,
            user_name, target, target, user_name
        )
    return _normalize_internal_message_rows(rows)

async def get_latest_internal_message_id(user_name: str) -> int:
    """Retorna o último ID de mensagem interna visível para o usuário."""
    return await pool.fetchval(
        """
        SELECT COALESCE(MAX(id), 0)
        FROM internal_messages
        WHERE target_name = 'geral'
           OR target_name = $1
           OR sender_name = $1
        """,
        user_name,
    ) or 0

async def get_internal_messages_since(user_name: str, last_id: int = 0, limit: int = 80):
    """Lista mensagens internas visíveis criadas após `last_id`."""
    rows = await pool.fetch(
        f"""
        {INTERNAL_MESSAGE_SELECT}
        WHERE im.id > $1
          AND (
              im.target_name = 'geral'
              OR im.target_name = $2
              OR im.sender_name = $2
          )
        ORDER BY im.id ASC
        LIMIT $3
        """,
        max(0, int(last_id or 0)),
        user_name,
        max(1, min(int(limit or 80), 200)),
    )
    return _normalize_internal_message_rows(rows)

async def get_internal_message(message_id: int):
    """Busca uma mensagem interna por id para validar leitura e ações permitidas."""
    row = await pool.fetchrow("SELECT * FROM internal_messages WHERE id = $1", message_id)
    return dict(row) if row else None

async def save_internal_message(sender: str, target: str, text: str, reply_to_id: int | None = None, forwarded_from_id: int | None = None, mentions: list[str] | None = None):
    """Persiste mensagem interna textual/mídia e retorna o id criado."""
    return await pool.fetchval(
        """
        INSERT INTO internal_messages (sender_name, target_name, msg_text, reply_to_id, forwarded_from_id, mentions)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        sender, target, text, reply_to_id, forwarded_from_id, mentions or []
    )

async def react_internal_message(message_id: int, actor_name: str, emoji: str):
    """Registra ou substitui a reação de um usuário em mensagem não apagada."""
    await pool.execute(
        """
        UPDATE internal_messages
        SET reactions = COALESCE(reactions, '{}'::jsonb) || jsonb_build_object($2::text, $3::text)
        WHERE id = $1
        """,
        message_id, actor_name, emoji
    )

async def edit_internal_message(message_id: int, text: str, mentions: list[str] | None = None):
    """Atualiza texto, menções e horário de edição de mensagem interna."""
    await pool.execute(
        """
        UPDATE internal_messages
        SET msg_text = $2, mentions = $3, edited_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND deleted_at IS NULL
        """,
        message_id, text, mentions or []
    )

async def delete_internal_message(message_id: int):
    """Marca mensagem como apagada e limpa reação, menção e fixação."""
    await pool.execute(
        """
        UPDATE internal_messages
        SET deleted_at = CURRENT_TIMESTAMP,
            reply_to_id = NULL,
            reactions = '{}'::jsonb,
            mentions = '{}'::text[],
            pinned_at = NULL,
            pinned_by = NULL
        WHERE id = $1 AND deleted_at IS NULL
        """,
        message_id
    )

async def pin_internal_message(message_id: int, actor_name: str):
    """Fixa mensagem interna visível e registra quem realizou a ação."""
    await pool.execute(
        """
        UPDATE internal_messages
        SET pinned_at = CURRENT_TIMESTAMP, pinned_by = $2
        WHERE id = $1 AND deleted_at IS NULL
        """,
        message_id, actor_name
    )

async def unpin_internal_message(message_id: int):
    """Remove fixação e responsável pela fixação de uma mensagem interna."""
    await pool.execute(
        """
        UPDATE internal_messages
        SET pinned_at = NULL, pinned_by = NULL
        WHERE id = $1
        """,
        message_id
    )

async def save_status_post(status_id: str, seller_name: str, session_id: str, status_type: str, preview: str, target_count: int, filter_word: str = ""):
    """Registra um status publicado para auditoria e acompanhamento."""
    await pool.execute(
        """
        INSERT INTO status_posts (status_id, seller_name, session_id, status_type, preview, target_count, filter_word)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (status_id) DO UPDATE SET target_count = EXCLUDED.target_count, preview = EXCLUDED.preview
        """,
        status_id, seller_name, session_id, status_type, preview, target_count, filter_word
    )

async def save_status_targets(status_id: str, targets: list):
    """Persiste a lista real de destinatários enviada para um status."""
    if not targets:
        return
    rows = [
        (
            status_id,
            target.get("target_jid", ""),
            target.get("contact_name", ""),
            target.get("assigned_to", ""),
        )
        for target in targets
        if target.get("target_jid")
    ]
    if not rows:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO status_targets (status_id, target_jid, contact_name, assigned_to)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (status_id, target_jid) DO UPDATE
                SET contact_name = EXCLUDED.contact_name,
                    assigned_to = EXCLUDED.assigned_to
                """,
                rows
            )
            await conn.execute(
                "UPDATE status_posts SET target_count = $2 WHERE status_id = $1",
                status_id, len(rows)
            )

async def add_status_view(status_id: str, viewer_jid: str):
    """Marca um contato como visualizador e informa se post/alvo existiam."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            post_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM status_posts WHERE status_id = $1)",
                status_id
            )
            if not post_exists:
                return {"post_exists": False, "target_updated": False}
            await conn.execute(
                """
                INSERT INTO status_views (status_id, viewer_jid)
                VALUES ($1, $2)
                ON CONFLICT (status_id, viewer_jid) DO UPDATE SET viewed_at = CURRENT_TIMESTAMP
                """,
                status_id, viewer_jid
            )
            update_result = await conn.execute(
                """
                UPDATE status_targets
                SET viewed_at = COALESCE(viewed_at, CURRENT_TIMESTAMP)
                WHERE status_id = $1 AND target_jid = $2
                """,
                status_id, viewer_jid
            )
            updated_rows = int(update_result.split()[-1]) if update_result else 0
            return {"post_exists": True, "target_updated": updated_rows > 0}

async def get_status_posts(seller_name: str, role: str, limit: int = 20):
    """Lista status publicados com alvo real rastreado e visualizações."""
    counters_query = """
        LEFT JOIN (
            SELECT
                status_id,
                COUNT(*) AS tracked_targets,
                COUNT(*) FILTER (WHERE viewed_at IS NOT NULL) AS tracked_views
            FROM status_targets
            GROUP BY status_id
        ) st ON st.status_id = sp.status_id
        LEFT JOIN (
            SELECT status_id, COUNT(*) AS legacy_views
            FROM status_views
            GROUP BY status_id
        ) sv ON sv.status_id = sp.status_id
    """
    if role in {"admin", "financeiro"}:
        rows = await pool.fetch(
            f"""
            SELECT
                sp.*,
                COALESCE(st.tracked_targets, sp.target_count, 0) AS tracked_targets,
                COALESCE(st.tracked_views, sv.legacy_views, 0) AS views_count
            FROM status_posts sp
            {counters_query}
            ORDER BY sp.created_at DESC
            LIMIT $1
            """,
            limit
        )
    else:
        rows = await pool.fetch(
            f"""
            SELECT
                sp.*,
                COALESCE(st.tracked_targets, sp.target_count, 0) AS tracked_targets,
                COALESCE(st.tracked_views, sv.legacy_views, 0) AS views_count
            FROM status_posts sp
            {counters_query}
            WHERE sp.seller_name = $1
            ORDER BY sp.created_at DESC
            LIMIT $2
            """,
            seller_name, limit
        )
    return [dict(row) for row in rows]

async def get_status_viewers(status_id: str, seller_name: str = "", role: str = ""):
    """Lista visualizadores conhecidos de um status respeitando o escopo."""
    params = [status_id]
    access_clause = ""
    if role and role not in {"admin", "financeiro"}:
        params.append(seller_name)
        access_clause = f"AND sp.seller_name = ${len(params)}"

    rows = await pool.fetch(
        f"""
        SELECT sv.viewer_jid, sv.viewed_at, u.name
        FROM status_views sv
        JOIN status_posts sp ON sp.status_id = sv.status_id
        LEFT JOIN users u ON u.phone = sv.viewer_jid
        WHERE sv.status_id = $1
          {access_clause}
        ORDER BY sv.viewed_at DESC
        """,
        *params
    )
    return [dict(row) for row in rows]

async def get_status_targets(status_id: str, seller_name: str, role: str, view_filter: str = "all"):
    """Lista destinatários rastreados de um status com filtro de visualização."""
    if view_filter not in {"all", "viewed", "pending"}:
        view_filter = "all"

    params = [status_id]
    access_clause = ""
    if role not in {"admin", "financeiro"}:
        params.append(seller_name)
        access_clause = f"AND sp.seller_name = ${len(params)}"

    filter_clause = ""
    if view_filter == "viewed":
        filter_clause = "AND st.viewed_at IS NOT NULL"
    elif view_filter == "pending":
        filter_clause = "AND st.viewed_at IS NULL"

    rows = await pool.fetch(
        f"""
        SELECT
            st.target_jid,
            st.contact_name,
            st.assigned_to,
            st.sent_at,
            st.viewed_at,
            COALESCE(NULLIF(st.contact_name, ''), NULLIF(u.name, ''), st.target_jid) AS name
        FROM status_targets st
        JOIN status_posts sp ON sp.status_id = st.status_id
        LEFT JOIN users u ON u.phone = st.target_jid
        WHERE st.status_id = $1
          {access_clause}
          {filter_clause}
        ORDER BY
            CASE WHEN st.viewed_at IS NULL THEN 1 ELSE 0 END,
            st.viewed_at DESC NULLS LAST,
            LOWER(COALESCE(NULLIF(st.contact_name, ''), NULLIF(u.name, ''), st.target_jid))
        """,
        *params
    )
    return [dict(row) for row in rows]

async def get_status_target_summary(status_id: str, seller_name: str, role: str):
    """Resume total, visualizados e pendentes dos destinatários rastreados."""
    params = [status_id]
    access_clause = ""
    if role not in {"admin", "financeiro"}:
        params.append(seller_name)
        access_clause = f"AND sp.seller_name = ${len(params)}"

    row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(st.target_jid) AS total,
            COUNT(st.target_jid) FILTER (WHERE st.viewed_at IS NOT NULL) AS viewed,
            COUNT(st.target_jid) FILTER (WHERE st.viewed_at IS NULL) AS pending
        FROM status_posts sp
        LEFT JOIN status_targets st ON st.status_id = sp.status_id
        WHERE sp.status_id = $1
          {access_clause}
        """,
        *params
    )
    return dict(row) if row else {"total": 0, "viewed": 0, "pending": 0}

async def delete_status_post(status_id: str, seller_name: str, role: str):
    """Remove um status do histórico local respeitando o escopo do usuário."""
    params = [status_id]
    access_clause = ""
    if role not in {"admin", "financeiro"}:
        params.append(seller_name)
        access_clause = f"AND seller_name = ${len(params)}"
    row = await pool.fetchrow(
        f"""
        DELETE FROM status_posts
        WHERE status_id = $1
          {access_clause}
        RETURNING status_id, seller_name, session_id, target_count
        """,
        *params
    )
    return dict(row) if row else None

async def get_status_diagnostics(seller_name: str, role: str, limit: int = 10):
    """Retorna diagnóstico dos status recentes e do rastreio de visualizações."""
    params = []
    where_clause = ""
    if role not in {"admin", "financeiro"}:
        params.append(seller_name)
        where_clause = f"WHERE sp.seller_name = ${len(params)}"
    params.append(limit)
    limit_param = f"${len(params)}"

    rows = await pool.fetch(
        f"""
        SELECT
            sp.status_id,
            sp.seller_name,
            sp.session_id,
            sp.status_type,
            sp.target_count,
            sp.filter_word,
            sp.created_at,
            COALESCE(st.tracked_targets, 0) AS tracked_targets,
            COALESCE(st.tracked_views, 0) AS tracked_views,
            COALESCE(st.pending_targets, 0) AS pending_targets,
            st.latest_view_at,
            COALESCE(sv.legacy_views, 0) AS legacy_views,
            sv.latest_legacy_view_at
        FROM status_posts sp
        LEFT JOIN (
            SELECT
                status_id,
                COUNT(*) AS tracked_targets,
                COUNT(*) FILTER (WHERE viewed_at IS NOT NULL) AS tracked_views,
                COUNT(*) FILTER (WHERE viewed_at IS NULL) AS pending_targets,
                MAX(viewed_at) AS latest_view_at
            FROM status_targets
            GROUP BY status_id
        ) st ON st.status_id = sp.status_id
        LEFT JOIN (
            SELECT
                status_id,
                COUNT(*) AS legacy_views,
                MAX(viewed_at) AS latest_legacy_view_at
            FROM status_views
            GROUP BY status_id
        ) sv ON sv.status_id = sp.status_id
        {where_clause}
        ORDER BY sp.created_at DESC
        LIMIT {limit_param}
        """,
        *params
    )
    return [dict(row) for row in rows]

async def get_calendar_events_month(user_name: str, year_month: str, role: str = ""):
    """Lista eventos de agenda no mês respeitando escopo operacional ou administrativo."""
    admin_view = role in {"admin", "financeiro"}
    scope_sql = "" if admin_view else "AND (ce.user_name = $2 OR ce.user_name = 'admin' OR ce.assigned_to = $2)"
    params = [f"{year_month}-%"] if admin_view else [f"{year_month}-%", user_name]
    rows = await pool.fetch(
        f"""
        SELECT ce.*, COALESCE(NULLIF(u.name, ''), '') AS contact_name
        FROM calendar_events ce
        LEFT JOIN users u ON u.phone = ce.phone
        WHERE ce.event_date LIKE $1
          AND COALESCE(ce.status, 'pendente') != 'cancelado'
          {scope_sql}
        ORDER BY COALESCE(ce.due_at, ce.event_date::date::timestamp), ce.id
        """,
        *params,
    )
    return [dict(row) for row in rows]

async def add_calendar_event(
    user_name: str,
    date: str,
    note: str,
    phone: str = "",
    assigned_to: str = "",
    due_at: datetime | None = None,
    repeat_until_done: bool = True,
):
    """Cria tarefa de agenda com horário, responsável e repetição opcional."""
    await pool.execute(
        """
        INSERT INTO calendar_events (user_name, event_date, note, phone, assigned_to, due_at, status, repeat_until_done)
        VALUES ($1, $2, $3, $4, $5, $6, 'pendente', $7)
        """,
        user_name,
        date,
        note,
        phone or "",
        assigned_to or user_name,
        due_at,
        repeat_until_done,
    )

async def get_calendar_event(event_id: int):
    """Busca uma tarefa de agenda por ID."""
    row = await pool.fetchrow("SELECT * FROM calendar_events WHERE id = $1", event_id)
    return dict(row) if row else None

async def complete_calendar_event(event_id: int):
    """Marca tarefa da agenda como contato realizado."""
    await pool.execute(
        "UPDATE calendar_events SET status = 'contatado', completed_at = CURRENT_TIMESTAMP WHERE id = $1",
        event_id,
    )

async def reschedule_calendar_event(event_id: int, event_date: str, due_at: datetime):
    """Remarca tarefa da agenda e reativa lembretes recorrentes."""
    await pool.execute(
        """
        UPDATE calendar_events
        SET event_date = $2, due_at = $3, status = 'remarcado', completed_at = NULL
        WHERE id = $1
        """,
        event_id,
        event_date,
        due_at,
    )

async def delete_calendar_event(event_id: int):
    """Cancela uma tarefa de agenda sem apagar o histórico."""
    await pool.execute("UPDATE calendar_events SET status = 'cancelado' WHERE id = $1", event_id)

async def get_due_calendar_events(limit: int = 50):
    """Lista tarefas vencidas que ainda devem notificar o responsável."""
    rows = await pool.fetch(
        """
        SELECT *
        FROM calendar_events
        WHERE COALESCE(status, 'pendente') IN ('pendente', 'remarcado')
          AND repeat_until_done IS TRUE
          AND due_at IS NOT NULL
          AND due_at <= CURRENT_TIMESTAMP
        ORDER BY due_at ASC
        LIMIT $1
        """,
        limit,
    )
    return [dict(row) for row in rows]

async def get_due_calendar_events_for_user(user_name: str, role: str, limit: int = 30):
    """Lista tarefas vencidas visíveis para um usuário ou painel administrativo."""
    admin_view = role in {"admin", "financeiro"}
    scope_sql = "" if admin_view else "AND (ce.user_name = $1 OR ce.assigned_to = $1)"
    limit_param = "$1" if admin_view else "$2"
    params = [limit] if admin_view else [user_name, limit]
    rows = await pool.fetch(
        f"""
        SELECT ce.*, COALESCE(NULLIF(u.name, ''), '') AS contact_name
        FROM calendar_events ce
        LEFT JOIN users u ON u.phone = ce.phone
        WHERE COALESCE(ce.status, 'pendente') IN ('pendente', 'remarcado')
          AND ce.due_at IS NOT NULL
          AND ce.due_at <= CURRENT_TIMESTAMP
          {scope_sql}
        ORDER BY ce.due_at ASC
        LIMIT {limit_param}
        """,
        *params,
    )
    return [dict(row) for row in rows]

async def get_snippets(username: str):
    """Lista atalhos de texto salvos para um usuário."""
    rows = await pool.fetch("SELECT * FROM user_snippets WHERE username = $1", username)
    return [dict(row) for row in rows]

async def add_snippet(username: str, shortcut: str, content: str):
    """Cria um atalho de texto para respostas rápidas."""
    await pool.execute("INSERT INTO user_snippets (username, shortcut, content) VALUES ($1, $2, $3)", username, shortcut, content)

async def get_all_tags():
    """Lista tags disponíveis para contatos."""
    rows = await pool.fetch("SELECT * FROM tags")
    return [dict(row) for row in rows]

async def create_tag(name: str, color: str):
    """Cria uma tag com nome e cor."""
    await pool.execute("INSERT INTO tags (name, color) VALUES ($1, $2)", name, color)

async def toggle_user_tag(phone: str, tag_id: int, action: str):
    """Vincula ou desvincula uma tag de um contato."""
    if action == 'add':
        await pool.execute("INSERT INTO contact_tags (phone, tag_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", phone, tag_id)
    else:
        await pool.execute("DELETE FROM contact_tags WHERE phone = $1 AND tag_id = $2", phone, tag_id)

async def get_contact_lists(user_name: str = "", role: str = ""):
    """Lista listas globais e listas do usuario com contagem de contatos."""
    if role == "admin":
        rows = await pool.fetch(
            """
            SELECT cl.*, COUNT(clm.phone) AS contact_count
            FROM contact_lists cl
            LEFT JOIN contact_list_members clm ON clm.list_id = cl.id
            GROUP BY cl.id
            ORDER BY cl.owner_name, cl.name
            """
        )
    else:
        rows = await pool.fetch(
            """
            SELECT cl.*, COUNT(clm.phone) AS contact_count
            FROM contact_lists cl
            LEFT JOIN contact_list_members clm ON clm.list_id = cl.id
            WHERE cl.owner_name = '' OR cl.owner_name = $1
            GROUP BY cl.id
            ORDER BY CASE WHEN cl.owner_name = '' THEN 0 ELSE 1 END, cl.name
            """,
            user_name
        )
    return [dict(row) for row in rows]

async def create_contact_list(name: str, color: str, owner_name: str = ""):
    """Cria uma lista de contatos e retorna o registro criado ou existente."""
    row = await pool.fetchrow(
        """
        INSERT INTO contact_lists (name, color, owner_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (name, owner_name) DO UPDATE SET color = EXCLUDED.color
        RETURNING *
        """,
        name,
        color,
        owner_name
    )
    return dict(row) if row else None

async def toggle_contact_list_member(phone: str, list_id: int, action: str):
    """Adiciona ou remove um contato de uma lista."""
    if action == "add":
        await pool.execute("INSERT INTO contact_list_members (phone, list_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", phone, list_id)
    else:
        await pool.execute("DELETE FROM contact_list_members WHERE phone = $1 AND list_id = $2", phone, list_id)

async def mark_contact_reviewed(phone: str, reviewer_name: str):
    """Marca um contato como revisado removendo listas de revisão e adicionando `Revisados`."""
    reviewed_list = await create_contact_list("Revisados", "#00a884", reviewer_name)
    removed_rows = await pool.fetch(
        """
        DELETE FROM contact_list_members clm
        USING contact_lists cl
        WHERE cl.id = clm.list_id
          AND clm.phone = $1
          AND (cl.name ILIKE 'Revisar%' OR cl.name ILIKE 'Telefones revisar%')
        RETURNING cl.name
        """,
        phone
    )
    if reviewed_list:
        await toggle_contact_list_member(phone, reviewed_list["id"], "add")
    return {
        "reviewed_list_id": reviewed_list.get("id") if reviewed_list else None,
        "removed_lists": [row["name"] for row in removed_rows],
    }

async def get_contacts_for_prefix_scan():
    """Retorna contatos usados pela ferramenta admin de listas por prefixos."""
    rows = await pool.fetch("SELECT phone, name, assigned_to, stage FROM users ORDER BY name")
    return [dict(row) for row in rows]

async def get_contacts_for_owner_repair():
    """Lista contatos de carteira/triagem elegíveis para correção entre vendedoras."""
    rows = await pool.fetch(
        """
        SELECT phone, name, assigned_to, stage, last_interaction
        FROM users
        WHERE stage IN ('carteira', 'menu_wait')
          AND (
            assigned_to IS NULL
            OR assigned_to = ''
            OR assigned_to IN ('vendas_1', 'vendas_2')
            OR assigned_to IN (
                SELECT name FROM system_users
                WHERE role IN ('vendas_1', 'vendas_2') AND COALESCE(is_active, 1) = 1
            )
          )
        ORDER BY last_interaction DESC NULLS LAST, phone
        """
    )
    return [dict(row) for row in rows]

async def apply_contact_owner_assignments(assignments: list):
    """Atualiza responsável apenas de carteira/triagem e preserva histórico do contato."""
    if not assignments:
        return {"updated": 0}
    updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in assignments:
                phone = item.get("phone")
                owner = item.get("assigned_to")
                if not phone or not owner:
                    continue
                result = await conn.execute(
                    """
                    UPDATE users
                    SET assigned_to = $1,
                        stage = 'carteira'
                    WHERE phone = $2
                      AND stage IN ('menu_wait', 'carteira')
                    """,
                    owner,
                    phone
                )
                if result and result.endswith(" 1"):
                    updated += 1
    return {"updated": updated}

async def apply_contact_prefix_lists(grouped_contacts: dict, colors: dict = None):
    """Cria listas herdadas e vincula contatos sem alterar nome ou responsável."""
    colors = colors or {}
    result = {}
    for canonical_name, contacts in grouped_contacts.items():
        list_name = f"Herdados {canonical_name}"
        contact_list = await create_contact_list(list_name, colors.get(canonical_name, "#00a884"), "")
        list_id = contact_list["id"]
        inserted = 0
        for contact in contacts:
            status = await pool.execute(
                """
                INSERT INTO contact_list_members (list_id, phone)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                list_id,
                contact["phone"]
            )
            if status.endswith(" 1"):
                inserted += 1
        result[list_name] = {
            "list_id": list_id,
            "matched": len(contacts),
            "inserted": inserted,
        }
    return result

async def get_contact_sanity_report(limit: int = 80):
    """Gera relatório técnico de contatos a revisar, mantido fora do menu admin."""
    summary = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE stage = 'carteira') AS carteira,
            COUNT(*) FILTER (WHERE phone !~ '^55[0-9]+@s\\.whatsapp\\.net$') AS phone_review,
            COUNT(*) FILTER (WHERE name IS NULL OR TRIM(name) = '') AS missing_name,
            COUNT(*) FILTER (
                WHERE stage = 'carteira'
                AND (
                    (assigned_to = 'Rosana' AND name ~* '^(kim|kimberly|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)[[:space:]_-]*')
                    OR (assigned_to = 'Kimberly' AND name ~* '^(rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)[[:space:]_-]*')
                )
            ) AS inherited_name_review
        FROM users
        """
    )
    suspects = await pool.fetch(
        """
        SELECT phone, name, assigned_to, stage,
            CASE
                WHEN phone !~ '^55[0-9]+@s\\.whatsapp\\.net$' THEN 'telefone_fora_padrao'
                WHEN name IS NULL OR TRIM(name) = '' THEN 'sem_nome'
                WHEN assigned_to = 'Rosana' AND name ~* '^(kim|kimberly)[[:space:]_-]*' THEN 'nome_kim_na_rosana'
                WHEN assigned_to = 'Rosana' AND name ~* '^(fla|flavia|flávia)[[:space:]_-]*' THEN 'nome_flavia_na_rosana'
                WHEN assigned_to = 'Kimberly' AND name ~* '^(rosana|ro)[[:space:]_-]*' THEN 'nome_rosana_na_kimberly'
                WHEN name ~* '^(thalita|sandro|denise|ronaldo|rodrigo)[[:space:]_-]*' THEN 'nome_vendedor_antigo'
                ELSE 'revisar'
            END AS reason,
            regexp_replace(COALESCE(name, ''), '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)\\s*[-_]?\\s*', '', 'i') AS suggested_name
        FROM users
        WHERE phone !~ '^55[0-9]+@s\\.whatsapp\\.net$'
           OR name IS NULL
           OR TRIM(name) = ''
           OR name ~* '^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)[[:space:]_-]*'
        ORDER BY reason, assigned_to, name
        LIMIT $1
        """,
        limit
    )
    return {"summary": dict(summary) if summary else {}, "suspects": [dict(row) for row in suspects]}


# =====================================================================
# ROADMAP DE EVOLUÇÃO — Quick wins (tabelas + helpers)
# Tabelas novas são GLOBAIS (sem RLS), com tenant_id + filtro explícito,
# no padrão da Fase 9. Colunas novas em tabelas existentes usam
# ALTER ... ADD COLUMN IF NOT EXISTS (idempotente). user_snippets e
# system_users já possuem RLS por tenant (TENANT_TABLES).
# =====================================================================

async def create_quickwins_tables():
    """Quick wins: cria tabelas/colunas novas de forma idempotente."""
    if pool is None:
        return
    await pool.execute("ALTER TABLE user_snippets ADD COLUMN IF NOT EXISTS shared BOOLEAN NOT NULL DEFAULT false")
    await pool.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS signature TEXT DEFAULT ''")
    await pool.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS signature_enabled BOOLEAN NOT NULL DEFAULT false")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS scheduled_messages (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT 'vendas',
            text TEXT NOT NULL,
            scheduled_at TIMESTAMPTZ NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            sent_at TIMESTAMPTZ,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_messages (status, scheduled_at)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS saved_filters (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'private',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_saved_filters_tenant ON saved_filters (tenant_id)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS csat_responses (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT 'vendas',
            seller_name TEXT NOT NULL DEFAULT '',
            score INTEGER,
            comment TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'sent',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            answered_at TIMESTAMPTZ
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_csat_tenant ON csat_responses (tenant_id, created_at)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS conversation_notes (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            author_name TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL,
            mentions JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_notes_phone ON conversation_notes (tenant_id, phone, created_at)")
    logger.info("Quick wins: tabelas/colunas garantidas.")


# ---- Respostas rápidas -------------------------------------------------
async def get_snippets_all(display_name: str):
    """Atalhos do usuário + atalhos compartilhados do tenant (RLS por tenant)."""
    rows = await pool.fetch(
        "SELECT id, username, shortcut, content, COALESCE(shared, false) AS shared "
        "FROM user_snippets WHERE username = $1 OR COALESCE(shared, false) = true "
        "ORDER BY shared, shortcut",
        display_name,
    )
    return [dict(r) for r in rows]


async def add_snippet_ex(display_name: str, shortcut: str, content: str, shared: bool = False):
    """Cria um atalho (opcionalmente compartilhado com a equipe)."""
    await pool.execute(
        "INSERT INTO user_snippets (username, shortcut, content, shared) VALUES ($1, $2, $3, $4)",
        display_name, shortcut, content, bool(shared),
    )


async def delete_snippet(snippet_id: int, username: str, role: str = ""):
    """Remove um atalho (admin remove qualquer; usuário só os próprios)."""
    if role == "admin":
        await pool.execute("DELETE FROM user_snippets WHERE id = $1", int(snippet_id))
    else:
        await pool.execute("DELETE FROM user_snippets WHERE id = $1 AND username = $2", int(snippet_id), username)


# ---- Assinatura do atendente ------------------------------------------
async def get_signature(username: str):
    row = await pool.fetchrow(
        "SELECT COALESCE(signature, '') AS signature, COALESCE(signature_enabled, false) AS signature_enabled "
        "FROM system_users WHERE username = $1",
        username,
    )
    if not row:
        return {"signature": "", "signature_enabled": False}
    return {"signature": row["signature"] or "", "signature_enabled": bool(row["signature_enabled"])}


async def set_signature(username: str, signature: str, enabled: bool):
    await pool.execute(
        "UPDATE system_users SET signature = $2, signature_enabled = $3 WHERE username = $1",
        username, signature or "", bool(enabled),
    )


# ---- Mensagens agendadas ----------------------------------------------
async def add_scheduled_message(phone, text, scheduled_at, created_by, session_id="vendas"):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO scheduled_messages (tenant_id, phone, session_id, text, scheduled_at, created_by) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        int(tid), phone, session_id or "vendas", text, scheduled_at, created_by or "",
    )
    return dict(row) if row else None


async def list_scheduled_messages(status="pending"):
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT id, phone, session_id, text, scheduled_at, created_by, status FROM scheduled_messages "
        "WHERE tenant_id = $1 AND status = $2 ORDER BY scheduled_at",
        int(tid), status,
    )
    return [dict(r) for r in rows]


async def cancel_scheduled_message(msg_id):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE scheduled_messages SET status = 'canceled' WHERE id = $1 AND tenant_id = $2 AND status = 'pending'",
        int(msg_id), int(tid),
    )


async def fetch_due_scheduled_messages(limit=50):
    """Loop de fundo: mensagens pendentes vencidas de TODOS os tenants."""
    rows = await pool.fetch(
        "SELECT id, tenant_id, phone, session_id, text, created_by FROM scheduled_messages "
        "WHERE status = 'pending' AND scheduled_at <= now() ORDER BY scheduled_at LIMIT $1",
        int(limit),
    )
    return [dict(r) for r in rows]


async def mark_scheduled_sent(msg_id, ok=True, error=""):
    if ok:
        await pool.execute("UPDATE scheduled_messages SET status = 'sent', sent_at = now() WHERE id = $1", int(msg_id))
    else:
        await pool.execute("UPDATE scheduled_messages SET status = 'error', error = $2 WHERE id = $1", int(msg_id), (error or "")[:500])


# ---- Filtros salvos ----------------------------------------------------
async def list_saved_filters(owner_name):
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT id, owner_name, name, scope, payload FROM saved_filters "
        "WHERE tenant_id = $1 AND (owner_name = $2 OR scope = 'team') ORDER BY scope, name",
        int(tid), owner_name,
    )
    return [dict(r) for r in rows]


async def add_saved_filter(owner_name, name, payload_json, scope="private"):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO saved_filters (tenant_id, owner_name, name, scope, payload) "
        "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id",
        int(tid), owner_name, name, scope, payload_json or "{}",
    )
    return dict(row) if row else None


async def delete_saved_filter(filter_id, owner_name, role=""):
    tid = get_current_tenant()
    if role == "admin":
        await pool.execute("DELETE FROM saved_filters WHERE id = $1 AND tenant_id = $2", int(filter_id), int(tid))
    else:
        await pool.execute("DELETE FROM saved_filters WHERE id = $1 AND tenant_id = $2 AND owner_name = $3", int(filter_id), int(tid), owner_name)


# ---- CSAT --------------------------------------------------------------
async def create_csat(phone, seller_name, session_id="vendas"):
    tid = get_current_tenant()
    await pool.execute(
        "INSERT INTO csat_responses (tenant_id, phone, session_id, seller_name, status) VALUES ($1, $2, $3, $4, 'sent')",
        int(tid), phone, session_id or "vendas", seller_name or "",
    )


async def record_csat_score(phone, score, comment=""):
    """Registra a nota na pesquisa pendente mais recente do contato."""
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "SELECT id FROM csat_responses WHERE tenant_id = $1 AND phone = $2 AND status = 'sent' ORDER BY created_at DESC LIMIT 1",
        int(tid), phone,
    )
    if row:
        await pool.execute(
            "UPDATE csat_responses SET score = $2, comment = $3, status = 'answered', answered_at = now() WHERE id = $1",
            row["id"], int(score), comment or "",
        )
    else:
        await pool.execute(
            "INSERT INTO csat_responses (tenant_id, phone, score, comment, status, answered_at) VALUES ($1, $2, $3, $4, 'answered', now())",
            int(tid), phone, int(score), comment or "",
        )


async def csat_report(days=30):
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT score, seller_name, comment FROM csat_responses "
        "WHERE tenant_id = $1 AND status = 'answered' AND answered_at >= now() - ($2 || ' days')::interval",
        int(tid), str(int(days)),
    )
    data = [dict(r) for r in rows]
    total = len(data)
    scores = [d["score"] for d in data if d.get("score") is not None]
    if not scores:
        return {"total": total, "avg": 0, "csat_pct": 0, "by_seller": {}}
    avg = round(sum(scores) / len(scores), 2)
    promoters = len([s for s in scores if s >= 4])
    csat_pct = round(100 * promoters / len(scores))
    by_seller = {}
    for d in data:
        k = d.get("seller_name") or "-"
        by_seller[k] = by_seller.get(k, 0) + 1
    return {"total": total, "avg": avg, "csat_pct": csat_pct, "by_seller": by_seller}


# ---- Notas internas por conversa --------------------------------------
async def get_conversation_notes(phone):
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT id, phone, author_name, note, mentions, created_at FROM conversation_notes "
        "WHERE tenant_id = $1 AND phone = $2 ORDER BY created_at DESC",
        int(tid), phone,
    )
    return [dict(r) for r in rows]


async def add_conversation_note(phone, author_name, note, mentions=None):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO conversation_notes (tenant_id, phone, author_name, note, mentions) "
        "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id",
        int(tid), phone, author_name or "", note, json.dumps(mentions or []),
    )
    return dict(row) if row else None


async def delete_conversation_note(note_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM conversation_notes WHERE id = $1 AND tenant_id = $2", int(note_id), int(tid))


# ---- Exportação de conversa -------------------------------------------
async def get_conversation_messages(phone):
    """Histórico de mensagens do contato (ordem cronológica) para exportação."""
    rows = await pool.fetch(
        "SELECT sender, msg_text, msg_type, timestamp FROM messages WHERE phone = $1 ORDER BY timestamp ASC",
        phone,
    )
    return [dict(r) for r in rows]


# =====================================================================
# FASE 10 - Automação & IA conversacional (tabelas + helpers)
# Tabelas globais (sem RLS) com tenant_id + filtro explícito, no padrão
# da Fase 9. flow_state e conversation_ai são keyed por (tenant_id, phone).
# =====================================================================

async def create_phase10_tables():
    """Fase 10: cria tabelas de IA/automação de forma idempotente."""
    if pool is None:
        return
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS ai_settings (
            tenant_id INTEGER PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT false,
            provider TEXT NOT NULL DEFAULT 'openai',
            api_base TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
            api_key TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
            features JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS chat_flows (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            definition JSONB NOT NULL DEFAULT '{}'::jsonb,
            trigger JSONB NOT NULL DEFAULT '{}'::jsonb,
            active BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_flows_tenant ON chat_flows (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS flow_state (
            tenant_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            flow_id INTEGER,
            node TEXT,
            vars JSONB NOT NULL DEFAULT '{}'::jsonb,
            done BOOLEAN NOT NULL DEFAULT false,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, phone)
        )"""
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS routing_rules (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 100,
            conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
            target TEXT NOT NULL DEFAULT '',
            skill TEXT NOT NULL DEFAULT '',
            strategy TEXT NOT NULL DEFAULT 'round_robin',
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_routing_tenant ON routing_rules (tenant_id, active, priority)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS agent_skills (
            tenant_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            skills JSONB NOT NULL DEFAULT '[]'::jsonb,
            regions JSONB NOT NULL DEFAULT '[]'::jsonb,
            PRIMARY KEY (tenant_id, username)
        )"""
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS assignment_cursor (
            tenant_id INTEGER NOT NULL,
            bucket TEXT NOT NULL,
            idx INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, bucket)
        )"""
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS lead_scores (
            tenant_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            band TEXT NOT NULL DEFAULT 'frio',
            reason TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, phone)
        )"""
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS kb_documents (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            content TEXT NOT NULL DEFAULT '',
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_kb_docs_tenant ON kb_documents (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS kb_chunks (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            ord INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_tenant ON kb_chunks (tenant_id)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS conversation_ai (
            tenant_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            sentiment TEXT NOT NULL DEFAULT 'neutro',
            sentiment_score REAL NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'pt',
            extracted JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, phone)
        )"""
    )
    logger.info("Fase 10: tabelas de IA/automação garantidas.")


# ---- Configuração de IA ------------------------------------------------
async def get_ai_settings():
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM ai_settings WHERE tenant_id = $1", int(tid))
    if not row:
        return {"enabled": False, "provider": "openai",
                "api_base": "https://api.openai.com/v1", "api_key": "",
                "model": "gpt-4o-mini", "features": {}}
    d = dict(row)
    if isinstance(d.get("features"), str):
        try:
            d["features"] = json.loads(d["features"])
        except Exception:
            d["features"] = {}
    return d


async def save_ai_settings(enabled, provider, api_base, api_key, model, features):
    tid = get_current_tenant()
    await pool.execute(
        """INSERT INTO ai_settings (tenant_id, enabled, provider, api_base, api_key, model, features, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, now())
           ON CONFLICT (tenant_id) DO UPDATE SET
             enabled=EXCLUDED.enabled, provider=EXCLUDED.provider, api_base=EXCLUDED.api_base,
             api_key=EXCLUDED.api_key, model=EXCLUDED.model, features=EXCLUDED.features, updated_at=now()""",
        int(tid), bool(enabled), provider or "openai",
        api_base or "https://api.openai.com/v1", api_key or "",
        model or "gpt-4o-mini", json.dumps(features or {}),
    )


# ---- Fluxos no-code ----------------------------------------------------
async def list_flows():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT id, name, active, definition, trigger, updated_at FROM chat_flows WHERE tenant_id = $1 ORDER BY updated_at DESC", int(tid))
    return [_row_flow(r) for r in rows]


def _row_flow(r):
    d = dict(r)
    for k in ("definition", "trigger"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = {}
    return d


async def get_flow(flow_id):
    tid = get_current_tenant()
    r = await pool.fetchrow("SELECT id, name, active, definition, trigger FROM chat_flows WHERE id = $1 AND tenant_id = $2", int(flow_id), int(tid))
    return _row_flow(r) if r else None


async def get_active_flow():
    tid = get_current_tenant()
    r = await pool.fetchrow("SELECT id, name, active, definition, trigger FROM chat_flows WHERE tenant_id = $1 AND active = true ORDER BY updated_at DESC LIMIT 1", int(tid))
    return _row_flow(r) if r else None


async def create_flow(name, definition, trigger=None, active=False):
    tid = get_current_tenant()
    r = await pool.fetchrow(
        "INSERT INTO chat_flows (tenant_id, name, definition, trigger, active) VALUES ($1, $2, $3::jsonb, $4::jsonb, $5) RETURNING id",
        int(tid), name, json.dumps(definition or {}), json.dumps(trigger or {}), bool(active),
    )
    return dict(r) if r else None


async def update_flow(flow_id, name=None, definition=None, trigger=None, active=None):
    tid = get_current_tenant()
    sets, args, i = [], [], 1
    if name is not None:
        sets.append("name = $" + str(i)); args.append(name); i += 1
    if definition is not None:
        sets.append("definition = $" + str(i) + "::jsonb"); args.append(json.dumps(definition)); i += 1
    if trigger is not None:
        sets.append("trigger = $" + str(i) + "::jsonb"); args.append(json.dumps(trigger)); i += 1
    if active is not None:
        sets.append("active = $" + str(i)); args.append(bool(active)); i += 1
    if not sets:
        return
    sets.append("updated_at = now()")
    args.append(int(flow_id)); args.append(int(tid))
    q = "UPDATE chat_flows SET " + ", ".join(sets) + " WHERE id = $" + str(i) + " AND tenant_id = $" + str(i + 1)
    await pool.execute(q, *args)


async def set_active_flow(flow_id):
    """Ativa um fluxo e desativa os demais do tenant (um ativo por vez)."""
    tid = get_current_tenant()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE chat_flows SET active = false WHERE tenant_id = $1", int(tid))
            await conn.execute("UPDATE chat_flows SET active = true, updated_at = now() WHERE id = $1 AND tenant_id = $2", int(flow_id), int(tid))


async def delete_flow(flow_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM chat_flows WHERE id = $1 AND tenant_id = $2", int(flow_id), int(tid))


# ---- Estado de fluxo por contato --------------------------------------
async def get_flow_state(phone):
    tid = get_current_tenant()
    r = await pool.fetchrow("SELECT flow_id, node, vars, done FROM flow_state WHERE tenant_id = $1 AND phone = $2", int(tid), phone)
    if not r:
        return None
    d = dict(r)
    if isinstance(d.get("vars"), str):
        try:
            d["vars"] = json.loads(d["vars"])
        except Exception:
            d["vars"] = {}
    return d


async def save_flow_state(phone, flow_id, node, variables, done):
    tid = get_current_tenant()
    await pool.execute(
        """INSERT INTO flow_state (tenant_id, phone, flow_id, node, vars, done, updated_at)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6, now())
           ON CONFLICT (tenant_id, phone) DO UPDATE SET
             flow_id=EXCLUDED.flow_id, node=EXCLUDED.node, vars=EXCLUDED.vars,
             done=EXCLUDED.done, updated_at=now()""",
        int(tid), phone, flow_id, node, json.dumps(variables or {}), bool(done),
    )


async def clear_flow_state(phone):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM flow_state WHERE tenant_id = $1 AND phone = $2", int(tid), phone)


# ---- Regras de roteamento + skills ------------------------------------
async def list_routing_rules(active_only=False):
    tid = get_current_tenant()
    q = "SELECT * FROM routing_rules WHERE tenant_id = $1"
    if active_only:
        q += " AND active = true"
    q += " ORDER BY priority ASC, id ASC"
    rows = await pool.fetch(q, int(tid))
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("conditions"), str):
            try:
                d["conditions"] = json.loads(d["conditions"])
            except Exception:
                d["conditions"] = []
        out.append(d)
    return out


async def create_routing_rule(name, priority, conditions, target, skill, strategy, active=True):
    tid = get_current_tenant()
    r = await pool.fetchrow(
        """INSERT INTO routing_rules (tenant_id, name, priority, conditions, target, skill, strategy, active)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8) RETURNING id""",
        int(tid), name or "", int(priority or 100), json.dumps(conditions or []),
        target or "", skill or "", strategy or "round_robin", bool(active),
    )
    return dict(r) if r else None


async def delete_routing_rule(rule_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM routing_rules WHERE id = $1 AND tenant_id = $2", int(rule_id), int(tid))


async def get_agent_skills():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT username, skills, regions FROM agent_skills WHERE tenant_id = $1", int(tid))
    out = {}
    for r in rows:
        d = dict(r)
        for k in ("skills", "regions"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    d[k] = []
        out[d["username"]] = {"skills": d.get("skills", []), "regions": d.get("regions", [])}
    return out


async def set_agent_skills(username, skills, regions):
    tid = get_current_tenant()
    await pool.execute(
        """INSERT INTO agent_skills (tenant_id, username, skills, regions)
           VALUES ($1, $2, $3::jsonb, $4::jsonb)
           ON CONFLICT (tenant_id, username) DO UPDATE SET skills=EXCLUDED.skills, regions=EXCLUDED.regions""",
        int(tid), username, json.dumps(skills or []), json.dumps(regions or []),
    )


async def next_round_robin(bucket, candidates):
    """Retorna o próximo atendente da lista em rodízio, persistindo o cursor."""
    if not candidates:
        return None
    tid = get_current_tenant()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT idx FROM assignment_cursor WHERE tenant_id = $1 AND bucket = $2 FOR UPDATE", int(tid), bucket)
            idx = (row["idx"] if row else 0)
            pick = candidates[idx % len(candidates)]
            nxt = (idx + 1) % max(1, len(candidates))
            await conn.execute(
                """INSERT INTO assignment_cursor (tenant_id, bucket, idx) VALUES ($1, $2, $3)
                   ON CONFLICT (tenant_id, bucket) DO UPDATE SET idx = EXCLUDED.idx""",
                int(tid), bucket, nxt,
            )
    return pick


async def count_open_by_agent(candidates):
    """Conta atendimentos em aberto por atendente (para estratégia por carga)."""
    if not candidates:
        return {}
    rows = await pool.fetch(
        "SELECT assigned_to, COUNT(*) AS n FROM users WHERE assigned_to = ANY($1::text[]) "
        "AND stage IN ('pendente_assumir', 'em_atendimento') GROUP BY assigned_to",
        candidates,
    )
    counts = {c: 0 for c in candidates}
    for r in rows:
        counts[r["assigned_to"]] = r["n"]
    return counts


# ---- Lead scoring ------------------------------------------------------
async def save_lead_score(phone, score, band, reason=""):
    tid = get_current_tenant()
    await pool.execute(
        """INSERT INTO lead_scores (tenant_id, phone, score, band, reason, updated_at)
           VALUES ($1, $2, $3, $4, $5, now())
           ON CONFLICT (tenant_id, phone) DO UPDATE SET score=EXCLUDED.score, band=EXCLUDED.band, reason=EXCLUDED.reason, updated_at=now()""",
        int(tid), phone, int(score), band, reason or "",
    )


async def get_lead_score(phone):
    tid = get_current_tenant()
    r = await pool.fetchrow("SELECT score, band, reason FROM lead_scores WHERE tenant_id = $1 AND phone = $2", int(tid), phone)
    return dict(r) if r else None


# ---- Base de conhecimento (RAG) ---------------------------------------
async def list_kb_documents():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT id, title, source, active, created_at, length(content) AS size FROM kb_documents WHERE tenant_id = $1 ORDER BY created_at DESC", int(tid))
    return [dict(r) for r in rows]


async def add_kb_document(title, content, source="manual", chunk_fn=None):
    tid = get_current_tenant()
    r = await pool.fetchrow(
        "INSERT INTO kb_documents (tenant_id, title, source, content, active) VALUES ($1, $2, $3, $4, true) RETURNING id",
        int(tid), title or "", source or "manual", content or "",
    )
    doc_id = r["id"] if r else None
    if doc_id and chunk_fn:
        chunks = chunk_fn(content or "")
        for i, ch in enumerate(chunks):
            await pool.execute(
                "INSERT INTO kb_chunks (tenant_id, doc_id, title, content, ord) VALUES ($1, $2, $3, $4, $5)",
                int(tid), doc_id, title or "", ch, i,
            )
    return {"id": doc_id, "chunks": (len(chunk_fn(content or "")) if chunk_fn else 0)}


async def delete_kb_document(doc_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM kb_chunks WHERE doc_id = $1 AND tenant_id = $2", int(doc_id), int(tid))
    await pool.execute("DELETE FROM kb_documents WHERE id = $1 AND tenant_id = $2", int(doc_id), int(tid))


async def get_kb_chunks():
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT c.id, c.title, c.content FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id "
        "WHERE c.tenant_id = $1 AND d.active = true",
        int(tid),
    )
    return [dict(r) for r in rows]


# ---- Cache de IA por conversa -----------------------------------------
async def save_conversation_ai(phone, sentiment=None, sentiment_score=None, summary=None, language=None, extracted=None):
    tid = get_current_tenant()
    existing = await pool.fetchrow("SELECT phone FROM conversation_ai WHERE tenant_id = $1 AND phone = $2", int(tid), phone)
    if not existing:
        await pool.execute(
            "INSERT INTO conversation_ai (tenant_id, phone, sentiment, sentiment_score, summary, language, extracted) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
            int(tid), phone, sentiment or "neutro", float(sentiment_score or 0),
            summary or "", language or "pt", json.dumps(extracted or {}),
        )
        return
    sets, args, i = [], [], 1
    if sentiment is not None:
        sets.append("sentiment = $" + str(i)); args.append(sentiment); i += 1
    if sentiment_score is not None:
        sets.append("sentiment_score = $" + str(i)); args.append(float(sentiment_score)); i += 1
    if summary is not None:
        sets.append("summary = $" + str(i)); args.append(summary); i += 1
    if language is not None:
        sets.append("language = $" + str(i)); args.append(language); i += 1
    if extracted is not None:
        sets.append("extracted = $" + str(i) + "::jsonb"); args.append(json.dumps(extracted)); i += 1
    if not sets:
        return
    sets.append("updated_at = now()")
    args.append(int(tid)); args.append(phone)
    q = "UPDATE conversation_ai SET " + ", ".join(sets) + " WHERE tenant_id = $" + str(i) + " AND phone = $" + str(i + 1)
    await pool.execute(q, *args)


async def get_conversation_ai(phone):
    tid = get_current_tenant()
    r = await pool.fetchrow("SELECT sentiment, sentiment_score, summary, language, extracted, updated_at FROM conversation_ai WHERE tenant_id = $1 AND phone = $2", int(tid), phone)
    if not r:
        return None
    d = dict(r)
    if isinstance(d.get("extracted"), str):
        try:
            d["extracted"] = json.loads(d["extracted"])
        except Exception:
            d["extracted"] = {}
    return d


async def ensure_tag(name, color="#ff6a00"):
    """Garante que uma tag exista e retorna seu id (para auto-tag de IA)."""
    row = await pool.fetchrow("SELECT id FROM tags WHERE name = $1", name)
    if row:
        return row["id"]
    row = await pool.fetchrow("INSERT INTO tags (name, color) VALUES ($1, $2) RETURNING id", name, color)
    return row["id"] if row else None


async def apply_tag_by_name(phone, name, color="#ff6a00"):
    """Aplica uma tag ao contato pelo nome, criando-a se necessário."""
    tag_id = await ensure_tag(name, color)
    if tag_id:
        await pool.execute("INSERT INTO contact_tags (phone, tag_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", phone, tag_id)
    return tag_id


# =====================================================================
#  FASE 11 - OMNICHANNEL
#  Tabelas globais (tenant_id + filtro manual, como nas Fases 9/10).
# =====================================================================
async def create_phase11_tables():
    """Fase 11: canais, caixa unificada, campanhas e segmentos (idempotente)."""
    if pool is None:
        return
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS channel_accounts (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'disconnected',
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, channel)
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_chan_tenant ON channel_accounts (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS omni_contacts (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            avatar TEXT NOT NULL DEFAULT '',
            meta JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, channel, external_id)
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_omnicontact_tenant ON omni_contacts (tenant_id, channel)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS omni_conversations (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            contact_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            assigned_to TEXT NOT NULL DEFAULT '',
            unread INTEGER NOT NULL DEFAULT 0,
            last_message TEXT NOT NULL DEFAULT '',
            last_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_omniconv_tenant ON omni_conversations (tenant_id, status, last_ts DESC)")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_omniconv_contact ON omni_conversations (tenant_id, contact_id, channel)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS omni_messages (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            direction TEXT NOT NULL DEFAULT 'in',
            sender TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            media_url TEXT NOT NULL DEFAULT '',
            msg_type TEXT NOT NULL DEFAULT 'text',
            external_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'sent',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_omnimsg_conv ON omni_messages (tenant_id, conversation_id, created_at)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS segments (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            rules JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_segments_tenant ON segments (tenant_id)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS campaigns (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT 'whatsapp_cloud',
            template TEXT NOT NULL DEFAULT '',
            media_url TEXT NOT NULL DEFAULT '',
            segment_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft',
            scheduled_at TIMESTAMPTZ,
            stats JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns (tenant_id, status)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS campaign_recipients (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            contact_ref TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '',
            sent_at TIMESTAMPTZ
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_camprec_campaign ON campaign_recipients (tenant_id, campaign_id, status)")
    logger.info("Fase 11: tabelas de omnichannel garantidas.")


# ---- Canais conectados ------------------------------------------------
async def list_channel_accounts():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM channel_accounts WHERE tenant_id = $1 ORDER BY channel", int(tid))
    out = []
    for r in rows:
        d = dict(r)
        cfg = d.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        # nunca devolve segredos crus para a UI
        d["has_config"] = bool(cfg)
        d["config_keys"] = sorted(list(cfg.keys())) if isinstance(cfg, dict) else []
        d.pop("config", None)
        out.append(d)
    return out


async def get_channel_account(channel):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "SELECT * FROM channel_accounts WHERE tenant_id = $1 AND channel = $2",
        int(tid), channel)
    if not row:
        return None
    d = dict(row)
    cfg = d.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    d["config"] = cfg
    return d


async def upsert_channel_account(channel, name, config, active=True):
    tid = get_current_tenant()
    # mescla config nova sobre a existente (para nao apagar segredos ao editar)
    existing = await get_channel_account(channel)
    merged = dict(existing.get("config", {})) if existing else {}
    for k, v in (config or {}).items():
        if v is not None and str(v).strip() != "":
            merged[k] = v
    status = "connected" if merged else "disconnected"
    await pool.execute(
        """INSERT INTO channel_accounts (tenant_id, channel, name, config, status, active)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6)
           ON CONFLICT (tenant_id, channel) DO UPDATE SET
             name = EXCLUDED.name, config = EXCLUDED.config,
             status = EXCLUDED.status, active = EXCLUDED.active""",
        int(tid), channel, name or channel, json.dumps(merged), status, bool(active))
    return {"status": "ok", "channel": channel, "connected": bool(merged)}


async def set_channel_status(channel, status=None, active=None):
    tid = get_current_tenant()
    if status is not None:
        await pool.execute("UPDATE channel_accounts SET status = $3 WHERE tenant_id = $1 AND channel = $2",
                           int(tid), channel, status)
    if active is not None:
        await pool.execute("UPDATE channel_accounts SET active = $3 WHERE tenant_id = $1 AND channel = $2",
                           int(tid), channel, bool(active))
    return {"status": "ok"}


async def delete_channel_account(channel):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM channel_accounts WHERE tenant_id = $1 AND channel = $2", int(tid), channel)
    return {"status": "ok"}


async def find_channel_by_config(channel, key, value):
    """Resolve o tenant/canal por um valor de config (ex.: verify_token do webhook).
    Busca em TODOS os tenants (o webhook chega sem contexto de tenant)."""
    rows = await pool.fetch("SELECT tenant_id, config FROM channel_accounts WHERE channel = $1", channel)
    for r in rows:
        cfg = r["config"]
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        if isinstance(cfg, dict) and str(cfg.get(key, "")) == str(value):
            return int(r["tenant_id"]), cfg
    return None, None


# ---- Contatos e conversas unificadas ---------------------------------
async def upsert_omni_contact(channel, external_id, name="", phone="", avatar=""):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO omni_contacts (tenant_id, channel, external_id, name, phone, avatar)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (tenant_id, channel, external_id) DO UPDATE SET
             name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE omni_contacts.name END,
             phone = CASE WHEN EXCLUDED.phone <> '' THEN EXCLUDED.phone ELSE omni_contacts.phone END
           RETURNING *""",
        int(tid), channel, str(external_id), name or "", phone or "", avatar or "")
    return dict(row) if row else None


async def get_or_create_omni_conversation(contact_id, channel):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "SELECT * FROM omni_conversations WHERE tenant_id = $1 AND contact_id = $2 AND channel = $3 AND status <> 'closed' ORDER BY last_ts DESC LIMIT 1",
        int(tid), int(contact_id), channel)
    if row:
        return dict(row)
    row = await pool.fetchrow(
        """INSERT INTO omni_conversations (tenant_id, contact_id, channel)
           VALUES ($1, $2, $3) RETURNING *""",
        int(tid), int(contact_id), channel)
    return dict(row) if row else None


async def save_omni_message(conversation_id, direction, sender, text, media_url="",
                            msg_type="text", external_id="", status="sent"):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO omni_messages
             (tenant_id, conversation_id, direction, sender, text, media_url, msg_type, external_id, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *""",
        int(tid), int(conversation_id), direction, sender or "", text or "",
        media_url or "", msg_type, str(external_id or ""), status)
    # atualiza a conversa (ultima msg + contador de nao lidas)
    unread_inc = 1 if direction == "in" else 0
    await pool.execute(
        """UPDATE omni_conversations
             SET last_message = $3, last_ts = now(), unread = unread + $4,
                 status = CASE WHEN status = 'closed' THEN 'open' ELSE status END
           WHERE tenant_id = $1 AND id = $2""",
        int(tid), int(conversation_id), (text or "")[:200], unread_inc)
    return dict(row) if row else None


async def list_omni_conversations(status="", channel="", assigned_to="", limit=100):
    tid = get_current_tenant()
    q = ["SELECT c.*, ct.name AS contact_name, ct.external_id AS contact_ref, ct.avatar",
         "FROM omni_conversations c JOIN omni_contacts ct ON ct.id = c.contact_id",
         "WHERE c.tenant_id = $1"]
    params = [int(tid)]
    if status:
        params.append(status)
        q.append("AND c.status = $" + str(len(params)))
    if channel:
        params.append(channel)
        q.append("AND c.channel = $" + str(len(params)))
    if assigned_to:
        params.append(assigned_to)
        q.append("AND c.assigned_to = $" + str(len(params)))
    params.append(int(limit))
    q.append("ORDER BY c.last_ts DESC LIMIT $" + str(len(params)))
    rows = await pool.fetch(" ".join(q), *params)
    return [dict(r) for r in rows]


async def get_omni_conversation(conversation_id):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """SELECT c.*, ct.name AS contact_name, ct.external_id AS contact_ref, ct.avatar
           FROM omni_conversations c JOIN omni_contacts ct ON ct.id = c.contact_id
           WHERE c.tenant_id = $1 AND c.id = $2""",
        int(tid), int(conversation_id))
    return dict(row) if row else None


async def get_omni_messages(conversation_id, limit=200):
    tid = get_current_tenant()
    rows = await pool.fetch(
        """SELECT * FROM omni_messages WHERE tenant_id = $1 AND conversation_id = $2
           ORDER BY created_at ASC LIMIT $3""",
        int(tid), int(conversation_id), int(limit))
    return [dict(r) for r in rows]


async def mark_conversation_read(conversation_id):
    tid = get_current_tenant()
    await pool.execute("UPDATE omni_conversations SET unread = 0 WHERE tenant_id = $1 AND id = $2",
                       int(tid), int(conversation_id))
    return {"status": "ok"}


async def assign_omni_conversation(conversation_id, assigned_to):
    tid = get_current_tenant()
    await pool.execute("UPDATE omni_conversations SET assigned_to = $3 WHERE tenant_id = $1 AND id = $2",
                       int(tid), int(conversation_id), assigned_to or "")
    return {"status": "ok"}


async def set_omni_conversation_status(conversation_id, status):
    tid = get_current_tenant()
    await pool.execute("UPDATE omni_conversations SET status = $3 WHERE tenant_id = $1 AND id = $2",
                       int(tid), int(conversation_id), status)
    return {"status": "ok"}


async def omni_unread_summary():
    tid = get_current_tenant()
    rows = await pool.fetch(
        """SELECT channel, COUNT(*) AS convs, COALESCE(SUM(unread),0) AS unread
           FROM omni_conversations WHERE tenant_id = $1 AND status = 'open'
           GROUP BY channel""", int(tid))
    return [dict(r) for r in rows]


# ---- Segmentos --------------------------------------------------------
async def create_segment(name, rules):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO segments (tenant_id, name, rules) VALUES ($1, $2, $3::jsonb) RETURNING id",
        int(tid), name or "Segmento", json.dumps(rules or []))
    return {"id": row["id"] if row else None}


async def list_segments():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM segments WHERE tenant_id = $1 ORDER BY created_at DESC", int(tid))
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("rules"), str):
            try:
                d["rules"] = json.loads(d["rules"])
            except Exception:
                d["rules"] = []
        out.append(d)
    return out


async def get_segment(segment_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM segments WHERE tenant_id = $1 AND id = $2", int(tid), int(segment_id))
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("rules"), str):
        try:
            d["rules"] = json.loads(d["rules"])
        except Exception:
            d["rules"] = []
    return d


async def delete_segment(segment_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM segments WHERE tenant_id = $1 AND id = $2", int(tid), int(segment_id))
    return {"status": "ok"}


async def resolve_segment_recipients(rules, limit=5000):
    """Resolve destinatarios a partir de regras simples sobre a base de contatos.

    Regras suportadas (lista de dicts), todas opcionais e combinadas com AND:
      - {"field": "stage", "op": "eq", "value": "carteira"}
      - {"field": "tag", "op": "has", "value": "lead-quente"}
      - {"field": "lead_band", "op": "eq", "value": "quente"}
    Retorna lista de dicts {contact_ref, name} a partir da tabela users (WhatsApp).
    """
    tid = get_current_tenant()
    where = ["u.tenant_id = $1"]
    params = [int(tid)]
    joins = []
    for rule in (rules or []):
        field = rule.get("field")
        op = rule.get("op", "eq")
        value = rule.get("value")
        if field == "stage" and value:
            params.append(value)
            where.append("u.stage = $" + str(len(params)))
        elif field == "lead_band" and value:
            joins.append("LEFT JOIN lead_scores ls ON ls.tenant_id = u.tenant_id AND ls.phone = u.phone")
            params.append(value)
            where.append("ls.band = $" + str(len(params)))
        elif field == "tag" and value:
            params.append(value)
            where.append(
                "EXISTS (SELECT 1 FROM tags tg WHERE tg.tenant_id = u.tenant_id AND tg.phone = u.phone AND tg.name = $"
                + str(len(params)) + ")")
    params.append(int(limit))
    sql = ("SELECT DISTINCT u.phone AS contact_ref, u.name FROM users u "
           + " ".join(joins) + " WHERE " + " AND ".join(where)
           + " LIMIT $" + str(len(params)))
    try:
        rows = await pool.fetch(sql, *params)
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[SEGMENT] resolve falhou: {e}")
        return []


# ---- Campanhas --------------------------------------------------------
async def create_campaign(name, channel, template, segment_id=None, media_url="", scheduled_at=None):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO campaigns (tenant_id, name, channel, template, media_url, segment_id, scheduled_at, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'draft') RETURNING id""",
        int(tid), name or "Campanha", channel or "whatsapp_cloud", template or "",
        media_url or "", segment_id, scheduled_at)
    return {"id": row["id"] if row else None}


async def list_campaigns():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM campaigns WHERE tenant_id = $1 ORDER BY created_at DESC", int(tid))
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("stats"), str):
            try:
                d["stats"] = json.loads(d["stats"])
            except Exception:
                d["stats"] = {}
        out.append(d)
    return out


async def get_campaign(campaign_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM campaigns WHERE tenant_id = $1 AND id = $2", int(tid), int(campaign_id))
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("stats"), str):
        try:
            d["stats"] = json.loads(d["stats"])
        except Exception:
            d["stats"] = {}
    return d


async def set_campaign_status(campaign_id, status, stats=None):
    tid = get_current_tenant()
    if stats is not None:
        await pool.execute("UPDATE campaigns SET status = $3, stats = $4::jsonb WHERE tenant_id = $1 AND id = $2",
                           int(tid), int(campaign_id), status, json.dumps(stats))
    else:
        await pool.execute("UPDATE campaigns SET status = $3 WHERE tenant_id = $1 AND id = $2",
                           int(tid), int(campaign_id), status)
    return {"status": "ok"}


async def delete_campaign(campaign_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM campaign_recipients WHERE tenant_id = $1 AND campaign_id = $2", int(tid), int(campaign_id))
    await pool.execute("DELETE FROM campaigns WHERE tenant_id = $1 AND id = $2", int(tid), int(campaign_id))
    return {"status": "ok"}


async def add_campaign_recipients(campaign_id, recipients):
    tid = get_current_tenant()
    n = 0
    for r in (recipients or []):
        ref = r.get("contact_ref") or r.get("phone") or ""
        if not ref:
            continue
        await pool.execute(
            """INSERT INTO campaign_recipients (tenant_id, campaign_id, contact_ref, name)
               VALUES ($1, $2, $3, $4)""",
            int(tid), int(campaign_id), str(ref), r.get("name", ""))
        n += 1
    return {"added": n}


async def list_campaign_recipients(campaign_id, status="", limit=1000):
    tid = get_current_tenant()
    if status:
        rows = await pool.fetch(
            "SELECT * FROM campaign_recipients WHERE tenant_id = $1 AND campaign_id = $2 AND status = $3 ORDER BY id LIMIT $4",
            int(tid), int(campaign_id), status, int(limit))
    else:
        rows = await pool.fetch(
            "SELECT * FROM campaign_recipients WHERE tenant_id = $1 AND campaign_id = $2 ORDER BY id LIMIT $3",
            int(tid), int(campaign_id), int(limit))
    return [dict(r) for r in rows]


async def update_recipient_status(recipient_id, status, error=""):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE campaign_recipients SET status = $3, error = $4, sent_at = now() WHERE tenant_id = $1 AND id = $2",
        int(tid), int(recipient_id), status, (error or "")[:300])
    return {"status": "ok"}


async def campaign_stats(campaign_id):
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT status, COUNT(*) AS n FROM campaign_recipients WHERE tenant_id = $1 AND campaign_id = $2 GROUP BY status",
        int(tid), int(campaign_id))
    stats = {"pending": 0, "sent": 0, "failed": 0, "total": 0}
    for r in rows:
        stats[r["status"]] = int(r["n"])
        stats["total"] += int(r["n"])
    return stats


# =====================================================================
#  FASE 12 - VENDAS & CRM AVANCADO
#  Tabelas globais (tenant_id + filtro manual, como nas Fases 9/10/11).
# =====================================================================
async def create_phase12_tables():
    """Fase 12: pipeline, negocios, catalogo, carrinho, pedidos, cadencias,
    metas e comissoes (idempotente)."""
    if pool is None:
        return
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS pipelines (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT 'Vendas',
            stages JSONB NOT NULL DEFAULT '[]'::jsonb,
            is_default BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_pipelines_tenant ON pipelines (tenant_id)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS deals (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            pipeline_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            contact_phone TEXT NOT NULL DEFAULT '',
            contact_name TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT 'lead',
            value NUMERIC NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'BRL',
            owner TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            probability INTEGER,
            expected_close DATE,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_deals_tenant ON deals (tenant_id, pipeline_id, stage)")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_deals_contact ON deals (tenant_id, contact_phone)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS deal_events (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            deal_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'note',
            detail TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_dealev_deal ON deal_events (tenant_id, deal_id, created_at)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            sku TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            price NUMERIC NOT NULL DEFAULT 0,
            stock INTEGER NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_products_tenant ON products (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS carts (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            contact_phone TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            discount NUMERIC NOT NULL DEFAULT 0,
            discount_type TEXT NOT NULL DEFAULT 'value',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_carts_tenant ON carts (tenant_id, contact_phone, status)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS cart_items (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            cart_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            price NUMERIC NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 1
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_cartitems_cart ON cart_items (tenant_id, cart_id)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            token TEXT NOT NULL DEFAULT '',
            contact_phone TEXT NOT NULL DEFAULT '',
            contact_name TEXT NOT NULL DEFAULT '',
            items JSONB NOT NULL DEFAULT '[]'::jsonb,
            subtotal NUMERIC NOT NULL DEFAULT 0,
            discount NUMERIC NOT NULL DEFAULT 0,
            total NUMERIC NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            payment_provider TEXT NOT NULL DEFAULT 'link',
            payment_link TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            paid_at TIMESTAMPTZ
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders (tenant_id, status)")
    await pool.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_token ON orders (token)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS cadences (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_cadences_tenant ON cadences (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS cadence_enrollments (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            cadence_id INTEGER NOT NULL,
            contact_phone TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT 'vendas',
            step_idx INTEGER NOT NULL DEFAULT 0,
            last_run TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_cadenroll_tenant ON cadence_enrollments (tenant_id, status)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS sales_goals (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            owner TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            metric TEXT NOT NULL DEFAULT 'revenue',
            target NUMERIC NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_goals_tenant ON sales_goals (tenant_id, period)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS commission_rules (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            min_value NUMERIC NOT NULL DEFAULT 0,
            rate NUMERIC NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_commrules_tenant ON commission_rules (tenant_id, active)")
    logger.info("Fase 12: tabelas de vendas & CRM garantidas.")


# ---- Pipelines --------------------------------------------------------
async def ensure_default_pipeline(default_stages):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM pipelines WHERE tenant_id = $1 ORDER BY is_default DESC, id ASC LIMIT 1", int(tid))
    if row:
        return dict(row)
    row = await pool.fetchrow(
        "INSERT INTO pipelines (tenant_id, name, stages, is_default) VALUES ($1, 'Vendas', $2::jsonb, true) RETURNING *",
        int(tid), json.dumps(default_stages))
    return dict(row) if row else None


def _load_json_field(d, field, default):
    if isinstance(d.get(field), str):
        try:
            d[field] = json.loads(d[field])
        except Exception:
            d[field] = default
    return d


async def list_pipelines():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM pipelines WHERE tenant_id = $1 ORDER BY is_default DESC, id", int(tid))
    return [_load_json_field(dict(r), "stages", []) for r in rows]


async def create_pipeline(name, stages):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO pipelines (tenant_id, name, stages) VALUES ($1, $2, $3::jsonb) RETURNING id",
        int(tid), name or "Pipeline", json.dumps(stages or []))
    return {"id": row["id"] if row else None}


# ---- Negocios (deals) -------------------------------------------------
async def list_deals(pipeline_id=None, owner="", status=""):
    tid = get_current_tenant()
    q = ["SELECT *, EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts FROM deals WHERE tenant_id = $1"]
    params = [int(tid)]
    if pipeline_id:
        params.append(int(pipeline_id))
        q.append("AND pipeline_id = $" + str(len(params)))
    if owner:
        params.append(owner)
        q.append("AND owner = $" + str(len(params)))
    if status:
        params.append(status)
        q.append("AND status = $" + str(len(params)))
    q.append("ORDER BY updated_at DESC")
    rows = await pool.fetch(" ".join(q), *params)
    return [dict(r) for r in rows]


async def get_deal(deal_id):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "SELECT *, EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts FROM deals WHERE tenant_id = $1 AND id = $2",
        int(tid), int(deal_id))
    return dict(row) if row else None


async def create_deal(pipeline_id, title, contact_phone="", contact_name="", value=0,
                      owner="", stage="lead", probability=None, expected_close=None, notes=""):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO deals (tenant_id, pipeline_id, title, contact_phone, contact_name,
             value, owner, stage, probability, expected_close, notes)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id""",
        int(tid), int(pipeline_id), title or "Negocio", contact_phone or "", contact_name or "",
        value or 0, owner or "", stage or "lead", probability, expected_close, notes or "")
    did = row["id"] if row else None
    if did:
        await add_deal_event(did, "created", "Negocio criado", owner)
    return {"id": did}


async def update_deal(deal_id, **fields):
    tid = get_current_tenant()
    allowed = ["title", "contact_phone", "contact_name", "value", "owner", "stage",
               "status", "probability", "expected_close", "notes"]
    sets = []
    params = [int(tid), int(deal_id)]
    for k in allowed:
        if k in fields and fields[k] is not None:
            params.append(fields[k])
            sets.append(k + " = $" + str(len(params)))
    if not sets:
        return {"status": "noop"}
    sets.append("updated_at = now()")
    await pool.execute("UPDATE deals SET " + ", ".join(sets) + " WHERE tenant_id = $1 AND id = $2", *params)
    return {"status": "ok"}


async def move_deal(deal_id, stage, author=""):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE deals SET stage = $3, updated_at = now() WHERE tenant_id = $1 AND id = $2",
        int(tid), int(deal_id), stage)
    await add_deal_event(deal_id, "stage", "Movido para " + str(stage), author)
    return {"status": "ok"}


async def set_deal_status(deal_id, status, author=""):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE deals SET status = $3, updated_at = now() WHERE tenant_id = $1 AND id = $2",
        int(tid), int(deal_id), status)
    await add_deal_event(deal_id, status, "Negocio marcado como " + str(status), author)
    return {"status": "ok"}


async def delete_deal(deal_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM deal_events WHERE tenant_id = $1 AND deal_id = $2", int(tid), int(deal_id))
    await pool.execute("DELETE FROM deals WHERE tenant_id = $1 AND id = $2", int(tid), int(deal_id))
    return {"status": "ok"}


async def add_deal_event(deal_id, ev_type, detail, author=""):
    tid = get_current_tenant()
    await pool.execute(
        "INSERT INTO deal_events (tenant_id, deal_id, type, detail, author) VALUES ($1, $2, $3, $4, $5)",
        int(tid), int(deal_id), ev_type, (detail or "")[:500], author or "")
    return {"status": "ok"}


async def get_deal_events(deal_id, limit=100):
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT * FROM deal_events WHERE tenant_id = $1 AND deal_id = $2 ORDER BY created_at DESC LIMIT $3",
        int(tid), int(deal_id), int(limit))
    return [dict(r) for r in rows]


# ---- Catalogo de produtos --------------------------------------------
async def list_products(active_only=False):
    tid = get_current_tenant()
    if active_only:
        rows = await pool.fetch("SELECT * FROM products WHERE tenant_id = $1 AND active = true ORDER BY name", int(tid))
    else:
        rows = await pool.fetch("SELECT * FROM products WHERE tenant_id = $1 ORDER BY name", int(tid))
    return [dict(r) for r in rows]


async def create_product(name, price, sku="", description="", stock=0, active=True):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO products (tenant_id, sku, name, description, price, stock, active)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
        int(tid), sku or "", name or "Produto", description or "", price or 0, int(stock or 0), bool(active))
    return {"id": row["id"] if row else None}


async def update_product(product_id, **fields):
    tid = get_current_tenant()
    allowed = ["sku", "name", "description", "price", "stock", "active"]
    sets = []
    params = [int(tid), int(product_id)]
    for k in allowed:
        if k in fields and fields[k] is not None:
            params.append(fields[k])
            sets.append(k + " = $" + str(len(params)))
    if not sets:
        return {"status": "noop"}
    await pool.execute("UPDATE products SET " + ", ".join(sets) + " WHERE tenant_id = $1 AND id = $2", *params)
    return {"status": "ok"}


async def delete_product(product_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM products WHERE tenant_id = $1 AND id = $2", int(tid), int(product_id))
    return {"status": "ok"}


async def get_product(product_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM products WHERE tenant_id = $1 AND id = $2", int(tid), int(product_id))
    return dict(row) if row else None


# ---- Carrinho ---------------------------------------------------------
async def get_or_create_cart(contact_phone):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "SELECT * FROM carts WHERE tenant_id = $1 AND contact_phone = $2 AND status = 'open' ORDER BY id DESC LIMIT 1",
        int(tid), contact_phone)
    if row:
        return dict(row)
    row = await pool.fetchrow(
        "INSERT INTO carts (tenant_id, contact_phone) VALUES ($1, $2) RETURNING *",
        int(tid), contact_phone)
    return dict(row) if row else None


async def get_cart_items(cart_id):
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM cart_items WHERE tenant_id = $1 AND cart_id = $2 ORDER BY id", int(tid), int(cart_id))
    return [dict(r) for r in rows]


async def add_cart_item(cart_id, product_id, qty=1):
    tid = get_current_tenant()
    prod = await get_product(product_id)
    if not prod:
        return {"status": "error", "error": "produto nao encontrado"}
    existing = await pool.fetchrow(
        "SELECT * FROM cart_items WHERE tenant_id = $1 AND cart_id = $2 AND product_id = $3",
        int(tid), int(cart_id), int(product_id))
    if existing:
        await pool.execute("UPDATE cart_items SET qty = qty + $4 WHERE tenant_id = $1 AND cart_id = $2 AND product_id = $3",
                           int(tid), int(cart_id), int(product_id), int(qty))
    else:
        await pool.execute(
            "INSERT INTO cart_items (tenant_id, cart_id, product_id, name, price, qty) VALUES ($1, $2, $3, $4, $5, $6)",
            int(tid), int(cart_id), int(product_id), prod["name"], prod["price"], int(qty))
    return {"status": "ok"}


async def remove_cart_item(item_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM cart_items WHERE tenant_id = $1 AND id = $2", int(tid), int(item_id))
    return {"status": "ok"}


async def set_cart_discount(cart_id, discount, discount_type="value"):
    tid = get_current_tenant()
    await pool.execute("UPDATE carts SET discount = $3, discount_type = $4 WHERE tenant_id = $1 AND id = $2",
                       int(tid), int(cart_id), discount or 0, discount_type)
    return {"status": "ok"}


async def close_cart(cart_id):
    tid = get_current_tenant()
    await pool.execute("UPDATE carts SET status = 'checked_out' WHERE tenant_id = $1 AND id = $2", int(tid), int(cart_id))
    return {"status": "ok"}


# ---- Pedidos ----------------------------------------------------------
async def create_order(token, contact_phone, contact_name, items, subtotal, discount, total,
                       payment_link="", payment_provider="link", owner=""):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO orders (tenant_id, token, contact_phone, contact_name, items,
             subtotal, discount, total, payment_link, payment_provider, owner)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11) RETURNING id""",
        int(tid), token, contact_phone or "", contact_name or "", json.dumps(items or []),
        subtotal or 0, discount or 0, total or 0, payment_link or "", payment_provider, owner or "")
    return {"id": row["id"] if row else None}


async def list_orders(status="", limit=200):
    tid = get_current_tenant()
    if status:
        rows = await pool.fetch("SELECT * FROM orders WHERE tenant_id = $1 AND status = $2 ORDER BY created_at DESC LIMIT $3",
                                int(tid), status, int(limit))
    else:
        rows = await pool.fetch("SELECT * FROM orders WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2", int(tid), int(limit))
    return [_load_json_field(dict(r), "items", []) for r in rows]


async def get_order_by_token(token):
    """Busca pedido por token (sem contexto de tenant - usado na pagina publica)."""
    row = await pool.fetchrow("SELECT * FROM orders WHERE token = $1", token)
    return _load_json_field(dict(row), "items", []) if row else None


async def mark_order_paid(token, external_id=""):
    row = await pool.fetchrow("SELECT tenant_id FROM orders WHERE token = $1", token)
    if not row:
        return {"status": "error"}
    await pool.execute(
        "UPDATE orders SET status = 'paid', paid_at = now(), external_id = $2 WHERE token = $1",
        token, external_id or "")
    return {"status": "ok", "tenant_id": int(row["tenant_id"])}


async def revenue_by_owner(period_prefix=""):
    """Soma de pedidos pagos por owner no periodo (YYYY-MM). Para metas/comissoes."""
    tid = get_current_tenant()
    if period_prefix:
        rows = await pool.fetch(
            """SELECT owner, COALESCE(SUM(total),0) AS revenue, COUNT(*) AS orders
               FROM orders WHERE tenant_id = $1 AND status = 'paid'
                 AND to_char(paid_at, 'YYYY-MM') = $2 GROUP BY owner""",
            int(tid), period_prefix)
    else:
        rows = await pool.fetch(
            """SELECT owner, COALESCE(SUM(total),0) AS revenue, COUNT(*) AS orders
               FROM orders WHERE tenant_id = $1 AND status = 'paid' GROUP BY owner""",
            int(tid))
    return [dict(r) for r in rows]


# ---- Cadencias --------------------------------------------------------
async def list_cadences():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM cadences WHERE tenant_id = $1 ORDER BY created_at DESC", int(tid))
    return [_load_json_field(dict(r), "steps", []) for r in rows]


async def get_cadence(cadence_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM cadences WHERE tenant_id = $1 AND id = $2", int(tid), int(cadence_id))
    return _load_json_field(dict(row), "steps", []) if row else None


async def create_cadence(name, steps, active=True):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO cadences (tenant_id, name, steps, active) VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
        int(tid), name or "Cadencia", json.dumps(steps or []), bool(active))
    return {"id": row["id"] if row else None}


async def update_cadence(cadence_id, name=None, steps=None, active=None):
    tid = get_current_tenant()
    sets = []
    params = [int(tid), int(cadence_id)]
    if name is not None:
        params.append(name); sets.append("name = $" + str(len(params)))
    if steps is not None:
        params.append(json.dumps(steps)); sets.append("steps = $" + str(len(params)) + "::jsonb")
    if active is not None:
        params.append(bool(active)); sets.append("active = $" + str(len(params)))
    if not sets:
        return {"status": "noop"}
    await pool.execute("UPDATE cadences SET " + ", ".join(sets) + " WHERE tenant_id = $1 AND id = $2", *params)
    return {"status": "ok"}


async def delete_cadence(cadence_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM cadence_enrollments WHERE tenant_id = $1 AND cadence_id = $2", int(tid), int(cadence_id))
    await pool.execute("DELETE FROM cadences WHERE tenant_id = $1 AND id = $2", int(tid), int(cadence_id))
    return {"status": "ok"}


async def enroll_cadence(cadence_id, contact_phone, session_id="vendas"):
    tid = get_current_tenant()
    existing = await pool.fetchrow(
        "SELECT id FROM cadence_enrollments WHERE tenant_id = $1 AND cadence_id = $2 AND contact_phone = $3 AND status = 'active'",
        int(tid), int(cadence_id), contact_phone)
    if existing:
        return {"status": "exists", "id": existing["id"]}
    row = await pool.fetchrow(
        """INSERT INTO cadence_enrollments (tenant_id, cadence_id, contact_phone, session_id)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        int(tid), int(cadence_id), contact_phone, session_id)
    return {"status": "ok", "id": row["id"] if row else None}


async def list_active_enrollments(limit=200):
    """Enrollments ativos de TODOS os tenants (para o loop de fundo)."""
    rows = await pool.fetch(
        """SELECT e.*, c.steps, c.active AS cadence_active,
                 EXTRACT(EPOCH FROM e.last_run)::bigint AS last_run_ts
           FROM cadence_enrollments e JOIN cadences c ON c.id = e.cadence_id AND c.tenant_id = e.tenant_id
           WHERE e.status = 'active' AND c.active = true LIMIT $1""", int(limit))
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("steps"), str):
            try:
                d["steps"] = json.loads(d["steps"])
            except Exception:
                d["steps"] = []
        out.append(d)
    return out


async def advance_enrollment(enrollment_id, tenant_id, step_idx, finished=False):
    await pool.execute(
        """UPDATE cadence_enrollments
             SET step_idx = $3, last_run = now(), status = $4
           WHERE tenant_id = $1 AND id = $2""",
        int(tenant_id), int(enrollment_id), int(step_idx), "done" if finished else "active")
    return {"status": "ok"}


async def cancel_enrollment(enrollment_id):
    tid = get_current_tenant()
    await pool.execute("UPDATE cadence_enrollments SET status = 'cancelled' WHERE tenant_id = $1 AND id = $2",
                       int(tid), int(enrollment_id))
    return {"status": "ok"}


# ---- Metas e comissoes ------------------------------------------------
async def list_goals(period=""):
    tid = get_current_tenant()
    if period:
        rows = await pool.fetch("SELECT * FROM sales_goals WHERE tenant_id = $1 AND period = $2 ORDER BY owner", int(tid), period)
    else:
        rows = await pool.fetch("SELECT * FROM sales_goals WHERE tenant_id = $1 ORDER BY period DESC, owner", int(tid))
    return [dict(r) for r in rows]


async def create_goal(owner, period, metric, target):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO sales_goals (tenant_id, owner, period, metric, target)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        int(tid), owner or "", period or "", metric or "revenue", target or 0)
    return {"id": row["id"] if row else None}


async def delete_goal(goal_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM sales_goals WHERE tenant_id = $1 AND id = $2", int(tid), int(goal_id))
    return {"status": "ok"}


async def list_commission_rules():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM commission_rules WHERE tenant_id = $1 ORDER BY min_value", int(tid))
    return [dict(r) for r in rows]


async def create_commission_rule(name, min_value, rate, active=True):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO commission_rules (tenant_id, name, min_value, rate, active)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        int(tid), name or "Regra", min_value or 0, rate or 0, bool(active))
    return {"id": row["id"] if row else None}


async def delete_commission_rule(rule_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM commission_rules WHERE tenant_id = $1 AND id = $2", int(tid), int(rule_id))
    return {"status": "ok"}


# ---- Historico 360 do contato ----------------------------------------
async def contact_360(phone):
    """Reune dados do contato: cadastro, deals, pedidos, lead score, tags."""
    tid = get_current_tenant()
    out = {"phone": phone}
    try:
        out["contact"] = await get_user(phone)
    except Exception:
        out["contact"] = None
    try:
        rows = await pool.fetch("SELECT * FROM deals WHERE tenant_id = $1 AND contact_phone = $2 ORDER BY updated_at DESC", int(tid), phone)
        out["deals"] = [dict(r) for r in rows]
    except Exception:
        out["deals"] = []
    try:
        rows = await pool.fetch("SELECT id, token, total, status, created_at FROM orders WHERE tenant_id = $1 AND contact_phone = $2 ORDER BY created_at DESC", int(tid), phone)
        out["orders"] = [dict(r) for r in rows]
    except Exception:
        out["orders"] = []
    try:
        out["lead"] = await get_lead_score(phone)
    except Exception:
        out["lead"] = None
    return out


# =====================================================================
#  FASE 13 - PRODUTIVIDADE DA EQUIPE
#  Filas & SLA, tickets, QA, gamificacao, escalas e presenca.
#  Tabelas globais (tenant_id + filtro manual, como nas Fases 9-12).
# =====================================================================
async def create_phase13_tables():
    """Fase 13: filas, tickets, QA, gamificacao, escalas e presenca."""
    if pool is None:
        return
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS queues (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT 'Atendimento',
            description TEXT NOT NULL DEFAULT '',
            sla_first_response_mins INTEGER NOT NULL DEFAULT 5,
            sla_resolution_mins INTEGER NOT NULL DEFAULT 60,
            members JSONB NOT NULL DEFAULT '[]'::jsonb,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_queues_tenant ON queues (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            queue_id INTEGER,
            contact_phone TEXT NOT NULL DEFAULT '',
            contact_name TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'open',
            assignee TEXT NOT NULL DEFAULT '',
            sla_first_state TEXT NOT NULL DEFAULT 'ok',
            sla_resolution_state TEXT NOT NULL DEFAULT 'ok',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            first_response_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_tickets_tenant ON tickets (tenant_id, status, queue_id)")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets (tenant_id, assignee)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS qa_scorecards (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT 'Avaliacao',
            criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_qacards_tenant ON qa_scorecards (tenant_id, active)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS qa_reviews (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            scorecard_id INTEGER,
            ticket_id INTEGER,
            agent TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL DEFAULT '',
            answers JSONB NOT NULL DEFAULT '{}'::jsonb,
            score INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_qarev_tenant ON qa_reviews (tenant_id, agent, created_at)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS gamification_events (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            agent TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            ref TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_gam_tenant ON gamification_events (tenant_id, agent)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS gamification_rules (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            UNIQUE (tenant_id, action)
        )"""
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS agent_shifts (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            agent TEXT NOT NULL DEFAULT '',
            weekday INTEGER NOT NULL DEFAULT 0,
            start_time TEXT NOT NULL DEFAULT '09:00',
            end_time TEXT NOT NULL DEFAULT '18:00'
        )"""
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_shifts_tenant ON agent_shifts (tenant_id, agent)")
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS agent_presence (
            tenant_id INTEGER NOT NULL,
            agent TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'offline',
            last_heartbeat TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, agent)
        )"""
    )
    logger.info("Fase 13: tabelas de produtividade da equipe garantidas.")


# ---- Filas ------------------------------------------------------------
async def list_queues(active_only=False):
    tid = get_current_tenant()
    if active_only:
        rows = await pool.fetch("SELECT * FROM queues WHERE tenant_id = $1 AND active = true ORDER BY name", int(tid))
    else:
        rows = await pool.fetch("SELECT * FROM queues WHERE tenant_id = $1 ORDER BY name", int(tid))
    return [_load_json_field(dict(r), "members", []) for r in rows]


async def get_queue(queue_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM queues WHERE tenant_id = $1 AND id = $2", int(tid), int(queue_id))
    return _load_json_field(dict(row), "members", []) if row else None


async def create_queue(name, description="", sla_first=5, sla_resolution=60, members=None):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO queues (tenant_id, name, description, sla_first_response_mins,
             sla_resolution_mins, members)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING id""",
        int(tid), name or "Fila", description or "", int(sla_first or 5),
        int(sla_resolution or 60), json.dumps(members or []))
    return {"id": row["id"] if row else None}


async def update_queue(queue_id, **fields):
    tid = get_current_tenant()
    mapping = {"name": "name", "description": "description",
               "sla_first": "sla_first_response_mins", "sla_resolution": "sla_resolution_mins",
               "active": "active"}
    sets = []
    params = [int(tid), int(queue_id)]
    for k, col in mapping.items():
        if k in fields and fields[k] is not None:
            params.append(fields[k])
            sets.append(col + " = $" + str(len(params)))
    if "members" in fields and fields["members"] is not None:
        params.append(json.dumps(fields["members"]))
        sets.append("members = $" + str(len(params)) + "::jsonb")
    if not sets:
        return {"status": "noop"}
    await pool.execute("UPDATE queues SET " + ", ".join(sets) + " WHERE tenant_id = $1 AND id = $2", *params)
    return {"status": "ok"}


async def delete_queue(queue_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM queues WHERE tenant_id = $1 AND id = $2", int(tid), int(queue_id))
    return {"status": "ok"}


# ---- Tickets ----------------------------------------------------------
def _ticket_epochs(d):
    for src, dst in [("created_at", "created_ts"), ("first_response_at", "first_response_ts"),
                     ("resolved_at", "resolved_ts")]:
        v = d.get(src)
        try:
            d[dst] = int(v.timestamp()) if v else None
        except Exception:
            d[dst] = None
    return d


async def list_tickets(status="", queue_id=None, assignee="", limit=300):
    tid = get_current_tenant()
    q = ["SELECT * FROM tickets WHERE tenant_id = $1"]
    params = [int(tid)]
    if status:
        params.append(status); q.append("AND status = $" + str(len(params)))
    if queue_id:
        params.append(int(queue_id)); q.append("AND queue_id = $" + str(len(params)))
    if assignee:
        params.append(assignee); q.append("AND assignee = $" + str(len(params)))
    params.append(int(limit))
    q.append("ORDER BY created_at DESC LIMIT $" + str(len(params)))
    rows = await pool.fetch(" ".join(q), *params)
    return [_ticket_epochs(dict(r)) for r in rows]


async def get_ticket(ticket_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM tickets WHERE tenant_id = $1 AND id = $2", int(tid), int(ticket_id))
    return _ticket_epochs(dict(row)) if row else None


async def create_ticket(queue_id, contact_phone, subject="", contact_name="", priority="normal", assignee=""):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO tickets (tenant_id, queue_id, contact_phone, contact_name, subject, priority, assignee)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
        int(tid), int(queue_id) if queue_id else None, contact_phone or "", contact_name or "",
        subject or "", priority or "normal", assignee or "")
    return {"id": row["id"] if row else None}


async def assign_ticket(ticket_id, assignee):
    tid = get_current_tenant()
    await pool.execute("UPDATE tickets SET assignee = $3, updated_at = now() WHERE tenant_id = $1 AND id = $2",
                       int(tid), int(ticket_id), assignee or "")
    return {"status": "ok"}


async def mark_ticket_first_response(ticket_id):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE tickets SET first_response_at = COALESCE(first_response_at, now()), updated_at = now() WHERE tenant_id = $1 AND id = $2",
        int(tid), int(ticket_id))
    return {"status": "ok"}


async def resolve_ticket(ticket_id):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE tickets SET status = 'resolved', resolved_at = now(), updated_at = now() WHERE tenant_id = $1 AND id = $2",
        int(tid), int(ticket_id))
    return {"status": "ok"}


async def set_ticket_sla_state(ticket_id, first_state, resolution_state):
    tid = get_current_tenant()
    await pool.execute(
        "UPDATE tickets SET sla_first_state = $3, sla_resolution_state = $4 WHERE tenant_id = $1 AND id = $2",
        int(tid), int(ticket_id), first_state, resolution_state)
    return {"status": "ok"}


async def list_open_tickets_all_tenants(limit=500):
    """Tickets abertos de TODOS os tenants, com prazos da fila (para o loop SLA)."""
    rows = await pool.fetch(
        """SELECT t.*, q.sla_first_response_mins, q.sla_resolution_mins
           FROM tickets t LEFT JOIN queues q ON q.id = t.queue_id AND q.tenant_id = t.tenant_id
           WHERE t.status = 'open' LIMIT $1""", int(limit))
    return [_ticket_epochs(dict(r)) for r in rows]


async def update_ticket_sla_state_global(ticket_id, tenant_id, first_state, resolution_state):
    await pool.execute(
        "UPDATE tickets SET sla_first_state = $3, sla_resolution_state = $4 WHERE tenant_id = $1 AND id = $2",
        int(tenant_id), int(ticket_id), first_state, resolution_state)
    return {"status": "ok"}


# ---- QA ---------------------------------------------------------------
async def list_scorecards():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM qa_scorecards WHERE tenant_id = $1 ORDER BY created_at DESC", int(tid))
    return [_load_json_field(dict(r), "criteria", []) for r in rows]


async def get_scorecard(scorecard_id):
    tid = get_current_tenant()
    row = await pool.fetchrow("SELECT * FROM qa_scorecards WHERE tenant_id = $1 AND id = $2", int(tid), int(scorecard_id))
    return _load_json_field(dict(row), "criteria", []) if row else None


async def create_scorecard(name, criteria):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO qa_scorecards (tenant_id, name, criteria) VALUES ($1, $2, $3::jsonb) RETURNING id",
        int(tid), name or "Avaliacao", json.dumps(criteria or []))
    return {"id": row["id"] if row else None}


async def delete_scorecard(scorecard_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM qa_scorecards WHERE tenant_id = $1 AND id = $2", int(tid), int(scorecard_id))
    return {"status": "ok"}


async def create_qa_review(scorecard_id, ticket_id, agent, reviewer, answers, score, notes=""):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        """INSERT INTO qa_reviews (tenant_id, scorecard_id, ticket_id, agent, reviewer, answers, score, notes)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) RETURNING id""",
        int(tid), int(scorecard_id) if scorecard_id else None, int(ticket_id) if ticket_id else None,
        agent or "", reviewer or "", json.dumps(answers or {}), int(score or 0), notes or "")
    return {"id": row["id"] if row else None}


async def list_qa_reviews(agent="", limit=100):
    tid = get_current_tenant()
    if agent:
        rows = await pool.fetch("SELECT * FROM qa_reviews WHERE tenant_id = $1 AND agent = $2 ORDER BY created_at DESC LIMIT $3",
                                int(tid), agent, int(limit))
    else:
        rows = await pool.fetch("SELECT * FROM qa_reviews WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2", int(tid), int(limit))
    return [_load_json_field(dict(r), "answers", {}) for r in rows]


async def qa_average_by_agent():
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT agent, AVG(score)::int AS avg_score, COUNT(*) AS reviews FROM qa_reviews WHERE tenant_id = $1 GROUP BY agent",
        int(tid))
    return [dict(r) for r in rows]


# ---- Gamificacao ------------------------------------------------------
async def add_gamification_event(agent, ev_type, points, ref=""):
    tid = get_current_tenant()
    await pool.execute(
        "INSERT INTO gamification_events (tenant_id, agent, type, points, ref) VALUES ($1, $2, $3, $4, $5)",
        int(tid), agent or "", ev_type or "", int(points or 0), ref or "")
    return {"status": "ok"}


async def gamification_leaderboard(limit=50):
    tid = get_current_tenant()
    rows = await pool.fetch(
        """SELECT agent, COALESCE(SUM(points),0) AS points, COUNT(*) AS events
           FROM gamification_events WHERE tenant_id = $1 GROUP BY agent ORDER BY points DESC LIMIT $2""",
        int(tid), int(limit))
    return [dict(r) for r in rows]


async def gamification_metrics(agent):
    """Metricas agregadas de um agente para calculo de conquistas."""
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT type, COUNT(*) AS n FROM gamification_events WHERE tenant_id = $1 AND agent = $2 GROUP BY type",
        int(tid), agent)
    counts = {r["type"]: int(r["n"]) for r in rows}
    return {
        "resolved": counts.get("conversa_resolvida", 0),
        "fast_responses": counts.get("primeira_resposta_rapida", 0),
        "csat_positive": counts.get("csat_positivo", 0),
        "sales": counts.get("venda", 0),
    }


async def list_gamification_rules():
    tid = get_current_tenant()
    rows = await pool.fetch("SELECT * FROM gamification_rules WHERE tenant_id = $1 ORDER BY action", int(tid))
    return [dict(r) for r in rows]


async def set_gamification_rule(action, points):
    tid = get_current_tenant()
    await pool.execute(
        """INSERT INTO gamification_rules (tenant_id, action, points) VALUES ($1, $2, $3)
           ON CONFLICT (tenant_id, action) DO UPDATE SET points = EXCLUDED.points""",
        int(tid), action, int(points or 0))
    return {"status": "ok"}


# ---- Escalas e presenca ----------------------------------------------
async def list_shifts(agent=""):
    tid = get_current_tenant()
    if agent:
        rows = await pool.fetch("SELECT * FROM agent_shifts WHERE tenant_id = $1 AND agent = $2 ORDER BY weekday, start_time", int(tid), agent)
    else:
        rows = await pool.fetch("SELECT * FROM agent_shifts WHERE tenant_id = $1 ORDER BY agent, weekday, start_time", int(tid))
    out = []
    for r in rows:
        d = dict(r)
        d["start"] = d.get("start_time")
        d["end"] = d.get("end_time")
        out.append(d)
    return out


async def add_shift(agent, weekday, start_time, end_time):
    tid = get_current_tenant()
    row = await pool.fetchrow(
        "INSERT INTO agent_shifts (tenant_id, agent, weekday, start_time, end_time) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        int(tid), agent or "", int(weekday or 0), start_time or "09:00", end_time or "18:00")
    return {"id": row["id"] if row else None}


async def delete_shift(shift_id):
    tid = get_current_tenant()
    await pool.execute("DELETE FROM agent_shifts WHERE tenant_id = $1 AND id = $2", int(tid), int(shift_id))
    return {"status": "ok"}


async def presence_heartbeat(agent, status="online"):
    tid = get_current_tenant()
    await pool.execute(
        """INSERT INTO agent_presence (tenant_id, agent, status, last_heartbeat, updated_at)
           VALUES ($1, $2, $3, now(), now())
           ON CONFLICT (tenant_id, agent)
           DO UPDATE SET status = EXCLUDED.status, last_heartbeat = now(), updated_at = now()""",
        int(tid), agent or "", status or "online")
    return {"status": "ok"}


async def list_presence():
    tid = get_current_tenant()
    rows = await pool.fetch(
        "SELECT agent, status, EXTRACT(EPOCH FROM last_heartbeat)::bigint AS heartbeat_ts FROM agent_presence WHERE tenant_id = $1",
        int(tid))
    return [dict(r) for r in rows]


async def supervision_snapshot():
    """Fotografia em tempo real para o painel de supervisao."""
    tid = get_current_tenant()
    snap = {}
    try:
        row = await pool.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status = 'open') AS open_tickets,
                      COUNT(*) FILTER (WHERE status = 'open' AND sla_first_state = 'violado') AS breached_first,
                      COUNT(*) FILTER (WHERE status = 'open' AND (sla_first_state = 'risco' OR sla_resolution_state = 'risco')) AS at_risk
               FROM tickets WHERE tenant_id = $1""", int(tid))
        snap["tickets"] = dict(row) if row else {}
    except Exception:
        snap["tickets"] = {}
    try:
        rows = await pool.fetch(
            "SELECT queue_id, COUNT(*) AS depth FROM tickets WHERE tenant_id = $1 AND status = 'open' GROUP BY queue_id",
            int(tid))
        snap["queue_depth"] = [dict(r) for r in rows]
    except Exception:
        snap["queue_depth"] = []
    try:
        snap["presence"] = await list_presence()
    except Exception:
        snap["presence"] = []
    return snap
