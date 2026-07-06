"""Restore lógico por tenant (Fase 7 — DR / teste de restore).

Contraparte de ``backup_tenant.py``: lê um arquivo de backup JSON (ou .gz) gerado
por aquele script e reimporta os dados de UM tenant, respeitando o isolamento por
RLS (ativa ``app.current_tenant`` na sessão). Projetado para validar DR de forma
reproduzível — idealmente contra um banco/tenant de STAGING.

Uso:
    # Simulação (não grava), só valida o arquivo e conta linhas:
    python scripts/restore_tenant.py backup_t2_acme_...json.gz --dry-run

    # Restaura para o MESMO tenant do backup:
    python scripts/restore_tenant.py backup.json --yes

    # Restaura para OUTRO tenant (ex.: staging id=99):
    python scripts/restore_tenant.py backup.json --target-tenant 99 --yes

Segurança:
- Exige ``--yes`` para gravar (evita restore acidental em produção).
- Por padrão faz *upsert best-effort* por tabela; use ``--truncate`` para limpar as
  tabelas do tenant alvo antes (CUIDADO: destrutivo, apenas no escopo do tenant).
"""

import argparse
import asyncio
import gzip
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from database import TENANT_TABLES  # noqa: E402


def _load_backup(path: str) -> dict:
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def _restore(conn, export: dict, target_tenant: int, truncate: bool, dry_run: bool) -> dict:
    # Ativa o escopo do tenant alvo (RLS grava/apaga apenas neste tenant).
    await conn.execute("SELECT set_config('app.current_tenant', $1, false)", str(target_tenant))

    tables = export.get("tables", {})
    summary = {"restored": {}, "skipped": {}, "errors": {}}

    for table in TENANT_TABLES:
        rows = tables.get(table)
        if not isinstance(rows, list):
            summary["skipped"][table] = "sem dados no backup"
            continue
        if dry_run:
            summary["restored"][table] = len(rows)
            continue
        try:
            async with conn.transaction():
                if truncate:
                    await conn.execute(f'DELETE FROM "{table}"')
                count = 0
                for row in rows:
                    if not isinstance(row, dict) or not row:
                        continue
                    data = dict(row)
                    # Força o tenant_id do alvo quando a coluna existir.
                    if "tenant_id" in data:
                        data["tenant_id"] = target_tenant
                    cols = list(data.keys())
                    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
                    collist = ", ".join(f'"{c}"' for c in cols)
                    sql = (
                        f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders}) '
                        f"ON CONFLICT DO NOTHING"
                    )
                    await conn.execute(sql, *[data[c] for c in cols])
                    count += 1
                summary["restored"][table] = count
        except asyncpg.PostgresError as exc:
            summary["errors"][table] = str(exc)
    return summary


async def _run(args):
    export = _load_backup(args.backup_file)
    src_meta = export.get("tenant", {})
    target_tenant = args.target_tenant if args.target_tenant is not None else src_meta.get("id")
    if target_tenant is None:
        raise SystemExit("Não foi possível determinar o tenant alvo (use --target-tenant).")

    print(f"[INFO] Backup de tenant={src_meta.get('id')} slug={src_meta.get('slug')} "
          f"-> alvo={target_tenant} dry_run={args.dry_run} truncate={args.truncate}")
    if not args.dry_run and not args.yes:
        raise SystemExit("Gravação requer --yes (ou use --dry-run para simular).")

    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        summary = await _restore(conn, export, int(target_tenant), args.truncate, args.dry_run)
    finally:
        await conn.close()

    total = sum(summary["restored"].values()) if summary["restored"] else 0
    print(f"[OK] linhas processadas={total}")
    if summary["errors"]:
        print(f"[ERROS] {summary['errors']}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Restore lógico por tenant (RLS-scoped).")
    parser.add_argument("backup_file", help="Arquivo de backup (.json ou .json.gz).")
    parser.add_argument("--target-tenant", type=int, default=None,
                        help="Tenant alvo (padrão: o mesmo do backup).")
    parser.add_argument("--truncate", action="store_true",
                        help="Apaga as tabelas do tenant alvo antes de restaurar (destrutivo).")
    parser.add_argument("--dry-run", action="store_true", help="Simula, sem gravar.")
    parser.add_argument("--yes", action="store_true", help="Confirma a gravação.")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
