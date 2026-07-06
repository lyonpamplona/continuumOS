# DR — Backups gerenciados e teste de restore (Fase 7)

Este documento descreve a estratégia de backup e recuperação de desastres (DR) do
continnumOS CRM SaaS e como **testar o restore** de forma reproduzível.

## 1. O que precisa de backup

| Camada | Conteúdo | Estratégia |
|---|---|---|
| **Postgres** | Todos os dados (tenants, mensagens, contatos, faturas…) | Backup gerenciado do provedor (PITR) + dump lógico por tenant |
| **Mídia** | Arquivos enviados/recebidos | Local: `/data/midias/tN`; S3/R2: versionamento + lifecycle do bucket |
| **Redis** | Estado efêmero (sessões, fila, locks) | Não crítico; pode ser reconstruído. Habilite AOF se quiser durabilidade |
| **Segredos** | `.env` / secret manager | Guardados no secret manager (Fase 6), fora do banco |

## 2. Backup gerenciado do Postgres (recomendado)

Use o backup gerenciado do provedor (RDS/Cloud SQL/Neon/Supabase):

- **PITR (Point-in-Time Recovery)** habilitado, retenção ≥ 7 dias.
- **Snapshots diários** automáticos.
- Se auto-hospedado, agende `pg_dump` diário + WAL archiving:

```bash
pg_dump "$DATABASE_URL" -Fc -f /data/backups/full_$(date +%F).dump
```

## 3. Backup lógico por tenant (granular)

Complementa o backup do banco e permite restaurar **um cliente** isolado (RLS):

```bash
# Um tenant
python scripts/backup_tenant.py 2 --out /data/backups --gzip
# Todos os tenants
python scripts/backup_tenant.py --all --out /data/backups --gzip
```

Agende via cron (ex.: diariamente 03:00):

```cron
0 3 * * * cd /app && python scripts/backup_tenant.py --all --out /data/backups --gzip >> /var/log/backup.log 2>&1
```

## 4. Teste de restore (obrigatório no DR)

Restaure sempre em **STAGING** (ou num tenant de teste), nunca sobrescrevendo
produção sem intenção. O script exige `--yes` para gravar.

```bash
# 1) Simulação (valida o arquivo e conta linhas, não grava)
python scripts/restore_tenant.py /data/backups/backup_t2_acme_XXES.json.gz --dry-run

# 2) Restore para um tenant de staging (ex.: id 99), limpando antes
python scripts/restore_tenant.py /data/backups/backup_t2_acme_XXES.json.gz \
    --target-tenant 99 --truncate --yes
```

### Checklist de validação pós-restore
- [ ] Contagem de linhas por tabela bate com o backup (`--dry-run` mostra os totais).
- [ ] Login de um usuário do tenant funciona.
- [ ] Mensagens/contatos aparecem no painel do tenant restaurado.
- [ ] Isolamento intacto: o tenant restaurado não vê dados de outros (teste RLS).

## 5. Objetivos de recuperação (defina com o negócio)

| Métrica | Alvo sugerido |
|---|---|
| **RPO** (perda máx. de dados) | ≤ 24h (backup diário) ou ≤ 5min (PITR) |
| **RTO** (tempo máx. p/ voltar) | ≤ 2h |
| **Frequência do teste de restore** | Mensal (staging) |

## 6. Runbook resumido de recuperação total

1. Provisionar novo Postgres e restaurar o último snapshot/PITR.
2. Restaurar segredos a partir do secret manager (Fase 6).
3. Rodar `alembic upgrade head` (garante schema na versão correta).
4. Reapontar `DATABASE_URL`/`REDIS_URL` e subir web + worker.
5. Restaurar mídia (S3 sync ou volume) se necessário.
6. Validar `/health` e o checklist da seção 4.
