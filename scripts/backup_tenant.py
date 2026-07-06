"""Backup lógico por tenant (Fase 5 — Escala e operação).

Exporta todos os dados de UM tenant para um arquivo JSON (opcionalmente
compactado em .gz), respeitando o isolamento por RLS: a exportação roda com o
GUC ``app.current_tenant`` setado, então só enxerga as linhas daquele cliente.

Uso:
    python scripts/backup_tenant.py <tenant_id> [--out DIR] [--gzip]
    python scripts/backup_tenant.py --all [--out DIR] [--gzip]

O backup de mídia (arquivos) é separado: no backend local a mídia do tenant já
fica isolada em ``/data/midias/tN``; no S3/R2 use replicação/lifecycle do bucket
ou ``aws s3 sync s3://bucket/tN ...``.

Este script foi desenhado para rodar via cron/CI (ex.: diariamente por tenant) e
é idempotente: cada execução gera um arquivo com timestamp.
"""

import argparse
import asyncio
import gzip
import json
import os
import sys
from datetime import datetime, date
from decimal import Decimal

import asyncpg

# Permite importar config/database quando executado da raiz do projeto.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from database import TENANT_TABLES  # noqa: E402


def _default(value):
    """Serializa tipos não-JSON nativos (datas, decimais, bytes)."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


async def _fetch_tenant_meta(conn, tenant_id):
    row = await conn.fetchrow(
        "SELECT id, slug, display_name, status, plan FROM tenants WHERE id = $1",
        tenant_id,
    )
    return dict(row) if row else None


async def backup_tenant(conn, tenant_id: int) -> dict:
    """Exporta um tenant e retorna o dicionário de backup."""
    meta = await _fetch_tenant_meta(conn, tenant_id)
    if not meta:
        raise SystemExit(f"Tenant {tenant_id} não encontrado.")

    # Ativa o escopo do tenant para toda a sessão (RLS enxerga apenas este tenant).
    await conn.execute("SELECT set_config('app.current_tenant', $1, false)", str(tenant_id))

    export = {
        "tenant": meta,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "tables": {},
    }
    for table in TENANT_TABLES:
        try:
            rows = await conn.fetch(f'SELECT * FROM "{table}"')
            export["tables"][table] = [dict(r) for r in rows]
        except asyncpg.PostgresError as exc:
            export["tables"][table] = {"__error__": str(exc)}
    return export


def _write_backup(export: dict, out_dir: str, use_gzip: bool) -> str:
    os.makedirs(out_dir, exist_ok=True)
    tenant_id = export["tenant"]["id"]
    slug = export["tenant"].get("slug", "tenant")
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = f"backup_t{tenant_id}_{slug}_{stamp}.json"
    payload = json.dumps(export, default=_default, ensure_ascii=False, indent=2)
    if use_gzip:
        path = os.path.join(out_dir, base + ".gz")
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(payload)
    else:
        path = os.path.join(out_dir, base)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    return path


async def _run(args):
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        if args.all:
            tenant_ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants ORDER BY id")]
        else:
            tenant_ids = [args.tenant_id]
        written = []
        for tid in tenant_ids:
            export = await backup_tenant(conn, tid)
            path = _write_backup(export, args.out, args.gzip)
            total = sum(len(v) for v in export["tables"].values() if isinstance(v, list))
            print(f"[OK] tenant={tid} linhas={total} -> {path}")
            written.append(path)
        return written
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backup lógico por tenant (RLS-scoped).")
    parser.add_argument("tenant_id", nargs="?", type=int, help="ID do tenant a exportar.")
    parser.add_argument("--all", action="store_true", help="Exporta todos os tenants.")
    parser.add_argument("--out", default=getattr(settings, "BACKUP_DIR", "/data/backups"), help="Diretório de saída.")
    parser.add_argument("--gzip", action="store_true", help="Compacta o backup em .gz.")
    args = parser.parse_args()
    if not args.all and args.tenant_id is None:
        parser.error("informe um tenant_id ou use --all")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
