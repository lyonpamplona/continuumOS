# Changelog da migração SaaS

## Fase 9 — Diferenciais (concluída)

### Analytics por tenant
- `get_tenant_analytics()` (RLS-scoped): totais (contatos, ativos, mensagens,
  in/out) e série mensagens/dia numa janela configurável.
- Painel interno `GET /api/analytics` (login + `metrics:view`) e via API pública.

### API pública (REST v1)
- Autenticação por **API key** (`Authorization: Bearer ebk_...`), com escopos
  `read`/`write`, hash SHA-256 e resolução token->tenant + ativação do GUC.
- Rotas: `/api/v1/ping|contacts|messages|analytics` e `POST /api/v1/messages`.
- Gestão de keys: `GET/POST /api/apikeys`, `DELETE /api/apikeys/{id}` (auditado).
- Respeita o rate limiting por tenant (Fase 6).

### Webhooks de saída
- `webhook_endpoints` por tenant + `services/webhooks.py` com assinatura
  HMAC-SHA256 (`X-continnumOS-Signature`) e log de entregas (`webhook_deliveries`).
- Eventos: `message.received`, `message.sent`, `contact.created`.
- Emissão em background no recebimento de mensagens e no envio via API.
- CRUD: `GET/POST /api/webhooks`, `DELETE /api/webhooks/{id}` + handler de job
  `deliver_webhook` (worker).

### Marketplace de integrações
- Catálogo estático (`integrations.py`): Webhooks, API pública, Zapier, n8n,
  Google Sheets, Slack. Estado por tenant em `tenant_integrations`.
- `GET /api/integrations`, `POST /api/integrations/{key}` (auditado).

### White-label (domínio próprio por cliente)
- `update_tenant_branding()` (display_name, brand_color, logo_url, domain) +
  coluna `logo_url`. `GET /api/branding` (público, resolvido por host) e
  `POST /api/branding` (admin).
- Resolução por subdomínio (`slug.BASE_DOMAIN`) ou domínio próprio
  (`get_tenant_id_by_host`). Docs `WHITELABEL.md` e `docs/API_PUBLICA.md`.

### Infra
- Novas tabelas globais (api_keys, webhook_endpoints, webhook_deliveries,
  tenant_integrations) criadas em `create_phase9_tables` após o multi-tenancy.


## Fase 8 — Produto e go-to-market (concluída)

### Signup self-service + trial
- Página pública `GET /signup` e endpoint `POST /api/signup`.
- `create_tenant_self_service()` cria tenant em modo trial (`billing_status=trial`,
  `trial_ends_at = now() + TRIAL_DAYS`) + primeiro usuário admin do cliente.
- Provisiona a instância respeitando o enforcement de `max_instances` (Fase 7).
- Acesso do tenant via subdomínio (`slug.BASE_DOMAIN`) — resolução já suportada.
- Config: `SIGNUP_ENABLED`, `SIGNUP_DEFAULT_PLAN`, `TRIAL_DAYS`, `BASE_DOMAIN`.

### Portal de billing do cliente
- Página `GET /billing` + APIs `GET /api/billing/me`, `POST /api/billing/change-plan`,
  `POST /api/billing/checkout-session`.
- Ver plano/uso/faturas, upgrade/downgrade (`set_tenant_plan`) e checkout do gateway
  (plugável; mensagem clara quando em modo manual). Auditado.

### Onboarding guiado
- `GET /api/onboarding/status`: 3 passos (conectar WhatsApp, importar contatos,
  primeira conversa/fluxo) com progresso calculado a partir do estado real.

### Roles/permissões granulares
- Novo módulo `permissions.py` (mapa role→permissões, `has_permission`,
  `user_has_permission`, `list_roles`). Papéis extras: `manager`, `billing_admin`,
  `viewer`. Aditivo/retrocompatível; `admin` mantém acesso total.
- `GET /api/roles` expõe o catálogo.

### Auditoria / LGPD
- `GET /api/lgpd/export?phone=` (portabilidade/acesso) e `POST /api/lgpd/delete`
  (direito ao esquecimento, exige `confirm:true`) — ambos auditados e RLS-scoped.
- Política de retenção: `DATA_RETENTION_DAYS` + `data_retention_loop` diário
  (protegido por lock, itera tenants). Docs `LGPD.md` e `DPA.md` (modelo).

### Documentação do cliente + status page
- `docs/CLIENTE.md` (guia do cliente) e status page pública `GET /status`
  (saúde de banco, Redis e storage; retorna 503 se degradado).


## Fase 7 — Confiabilidade e escala (concluída)

### Múltiplos workers/réplicas
- WebSocket/realtime já replicava via **Redis Pub/Sub** (canal `crm_channel`), então
  o estado de tempo real funciona com N réplicas.
- Novo `RUN_BACKGROUND_JOBS` (config/`.env`): os loops periódicos (SLA, follow-up,
  lembretes, limpeza, monitor, billing) rodam em **UMA** réplica/scheduler para
  não duplicar ações. Em deploy escalado, `false` nas réplicas web e `true` só no
  scheduler. `manager.listen()` continua em todas as réplicas.
- `WEB_CONCURRENCY` controla o nº de workers do uvicorn (`main.py`).

### Fila de jobs (webhooks/mídia pesada)
- Novo `services/jobqueue.py`: fila baseada em **Redis list** (RPUSH/BLPOP), sem
  dependência nova. `submit()` enfileira (modo fila) ou executa inline (padrão).
- Novo `worker.py`: consumidor separado (`python worker.py`) que reutiliza os
  handlers registrados em `main` (ex.: `process_webhook`).
- O endpoint `/webhook` passa a usar `jobqueue.submit(...)` — sem regressão quando
  `JOB_QUEUE_ENABLED=false`.

### Métricas Prometheus + tracing
- Novo `services/observability.py`: endpoint **`/metrics`** em formato de exposição
  Prometheus (Python puro, sem dependência obrigatória) com `http_requests_total`,
  histograma `http_request_duration_seconds`, uptime e contadores de negócio.
- Instrumentação no `observability_middleware` (sucesso e erro 500).
- `/metrics` protegido por `METRICS_TOKEN` opcional (Bearer).
- **OpenTelemetry** opcional (`OTEL_ENABLED`) instrumenta o FastAPI se os pacotes
  estiverem instalados; caso contrário no-op.

### Alertas (Sentry)
- Sentry opcional (`SENTRY_DSN`, via secret manager da Fase 6). `init_sentry()` no
  boot e `capture_exception()` usado na fila de jobs. No-op se ausente.

### Backups gerenciados + teste de restore (DR)
- Novo `scripts/restore_tenant.py`: contraparte do backup, restaura um tenant
  (RLS-scoped) com `--dry-run`, `--truncate` e `--yes`; permite restaurar para
  outro tenant (staging).
- Novo `DR.md`: estratégia de backup gerenciado (PITR), backup lógico por tenant,
  checklist de teste de restore, RPO/RTO e runbook de recuperação total.

### Enforcement de max_instances por plano
- `database.py`: `count_tenant_instances()` e `check_instance_limit()`.
- Provisionamento (`/api/superadmin/tenants`) recusa provisionar quando o limite
  de instâncias do plano é atingido, retornando `warning` + `provisioned=false`.

### Dependências
- Novo `requirements-optional.txt` (sentry-sdk, opentelemetry-*, prometheus-client).
  O núcleo da Fase 7 funciona sem eles.

---

## Fase 6 — Prontidão de produção / bloqueadores (concluída)

### Testes em CI real + cobertura
- `.github/workflows/ci.yml`: adicionados serviços **Postgres 16** e **Redis 7** com
  healthchecks; passo `alembic upgrade head`; `pytest` agora roda com **cobertura**
  (`--cov`, relatório `term-missing` + `coverage.xml` como artifact). Variáveis de
  ambiente de teste (segredos, `DATABASE_URL`, `REDIS_URL`, `RUN_DB_TESTS=1`).
- `requirements-dev.txt`: `pytest-cov`, `alembic`, `psycopg2-binary`.

### Testes de isolamento RLS (2 tenants + asserts)
- `tests/test_rls_isolation.py`: teste de integração que cria tenant A e B, insere
  dados em cada contexto e garante que A nunca lê linhas de B (e vice-versa),
  incluindo o caso "sem contexto" (nenhum vazamento). Gated por `RUN_DB_TESTS=1`.
- `tests/test_payments.py` e `tests/test_ratelimit.py`: unitários (sem rede/banco).

### Migrations versionadas (Alembic)
- `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_baseline.py`
  (reutiliza o DDL de `init_db`) e `0002_invoice_gateway_fields.py` (incremental,
  reversível). URL do banco resolvida de `settings.DATABASE_URL` (asyncpg→psycopg2).
- `config.py` + `database.py`: flag `RUN_DB_MIGRATIONS_ON_BOOT` (default `true` p/
  retrocompat). Com `false`, o boot pula o DDL e delega ao `alembic upgrade head`.

### Pagamento real (gateway + webhook)
- `services/payments.py`: verificação de assinatura de webhook para **Stripe**,
  **Mercado Pago**, **Asaas** e **manual** (HMAC-SHA256 / token), com extração de
  `invoice_id`/`external_id`/valor e detecção de pagamento. Recusa sem segredo.
- `main.py`: endpoint `POST /api/billing/webhook/{provider}` — valida assinatura,
  marca a fatura como paga (por `invoice_id` ou `external_id`) e reativa o tenant.
- `database.py`: colunas `provider`/`external_id` em `invoices` (+ índice único) e
  helpers `get_invoice`, `find_invoice_by_external_id`, `set_invoice_external_ref`,
  `mark_invoice_paid_by_external_id`, `reactivate_tenant_if_paid`.

### Suspensão automática por inadimplência
- `database.py`: `mark_overdue_invoices` (open → past_due com grace period),
  `get_tenants_with_overdue`, `tenant_has_open_debt`.
- `main.py`: `billing_enforcement_loop` (registrado no lifespan, protegido por
  `redis_lock`) marca vencidas e suspende tenants ativos com débito. Configurável
  por `BILLING_ENFORCEMENT_ENABLED` e `BILLING_GRACE_DAYS`.

### Rate limiting por tenant/IP
- `services/ratelimit.py`: janela fixa em Redis (INCR+EXPIRE), fail-open, IP via
  `X-Forwarded-For`. Integrado ao middleware de observabilidade em `/api/` e
  `/webhook` (teto próprio p/ webhook, protegendo custo/abuso da Evolution).
  Config: `RATE_LIMIT_*`.

### Rotação/gestão de segredos
- `config.py`: `env_secret()` resolve segredos por precedência `*_FILE` >
  `SECRETS_DIR/{NOME}` > env, permitindo Docker/K8s secrets e secret managers sem
  segredos em `.env` plano. Aplicado aos obrigatórios (chaves, DB, Redis, JWT,
  webhook, admin) e ao `PAYMENT_WEBHOOK_SECRET`.

### Config / infra
- `.env.example`: bloco Fase 6 (segredos, migrations, pagamentos, inadimplência,
  rate limiting).

## Fase 0 — Hardening (concluída)

### Segurança / segredos
- `config.py`: removidos todos os defaults inseguros. `JWT_SECRET`, `WEBHOOK_SECRET`,
  `ADMIN_PASSWORD`, `EVO_API_URL`, `EVO_API_KEY`, `DATABASE_URL` e `REDIS_URL` agora são
  **obrigatórios**; a aplicação falha no boot (`ConfigError`) se algum estiver ausente/vazio.
- `auth.py`: `SECRET_KEY` deixou de ter o fallback hardcoded
  `continnumOS@SecureToken2026-CRM`; usa diretamente `settings.JWT_SECRET`.
- Removido o default `admin123` de `ADMIN_PASSWORD`.

### CORS
- `main.py`: removido o fallback `["*"]`. Agora usa `settings.ALLOWED_ORIGINS`
  (lista explícita). `config.py` nunca mais devolve wildcard, eliminando a combinação
  inválida `allow_origins=["*"]` + `allow_credentials=True`.

### Webhook / secret
- Corrigida a inconsistência: a Evolution API envia `WEBHOOK_GLOBAL_AUTHORIZATION`
  (= `WEBHOOK_SECRET`), mas o `main.py` validava contra `EVO_API_KEY`. Agora o webhook
  valida contra `WEBHOOK_SECRET` (e aceita `EVO_API_KEY` por compatibilidade).
- `docker-compose.yaml`: removido o default inseguro em
  `WEBHOOK_GLOBAL_AUTHORIZATION=Bearer ${WEBHOOK_SECRET}`.

### Dependências
- `requirements.txt` e `requirements-dev.txt`: todas as versões **fixadas** (pinned).
- Adicionado `pytest-asyncio` para futuros testes assíncronos (Fase 5).

### Limpeza de legado
- Removidos `import_contacts.py` e `distribute_contacts.py` (usavam SQLite
  `/data/bot_atendimento.db`, incompatível com o PostgreSQL atual).
- Removido `services/__pycache__`.

> **Ação necessária antes de subir:** garanta que o arquivo `.env` define
> `JWT_SECRET`, `WEBHOOK_SECRET`, `ADMIN_PASSWORD`, `EVO_API_URL`, `EVO_API_KEY`,
> `DATABASE_URL`, `REDIS_URL` e `ALLOWED_ORIGINS`. Veja `.env.example`.

---

## Fase 1 — Multi-tenancy de dados (concluída)

### Estratégia
Isolamento por **coluna `tenant_id` + Row-Level Security (RLS) do PostgreSQL**,
ativado por uma variável de sessão (`GUC` `app.current_tenant`). O tenant atual vive
em um `contextvar` por requisição e é aplicado à conexão no hook `setup` do pool.
Resultado: **todas as queries já existentes ficam automaticamente escopadas ao tenant**,
sem reescrever as ~100 funções de acesso a dados.

### Novos arquivos
- `tenant_context.py`: `contextvar` do tenant (`set/get/reset_current_tenant`) e
  `DEFAULT_TENANT_ID = 1`.

### `database.py`
- Nova tabela **`tenants`** (`id, slug, display_name, status, evolution_instance,`
  `brand_color, domain, settings JSONB, created_at`) — **sem RLS** (é o catálogo global).
- Tenant padrão **continnumOS (id=1)** semeado; dados legados migrados para ele.
- Coluna **`tenant_id`** adicionada nas **18 tabelas** de dados, com backfill = 1,
  `NOT NULL`, `DEFAULT` derivado da GUC e índice composto `(tenant_id, <col quente>)`.
- **RLS habilitado + `FORCE`** e policy `tenant_isolation` (USING + WITH CHECK) em cada
  tabela: `tenant_id = current_setting('app.current_tenant')`.
- Hook `setup=_apply_tenant_guc` no pool: aplica `set_config('app.current_tenant', ...)`
  em cada `acquire` (inclui `pool.fetch/execute/...`).
- Helpers `get_tenant_id_by_host()` e `resolve_request_tenant_id()` (resolução por
  domínio exato ou subdomínio).
- Migração `_migrate_multitenant()` roda no boot sob o mesmo advisory lock (idempotente).

### `auth.py` / `main.py`
- `get_current_user`: resolve o tenant da requisição, ativa o contexto e guarda
  `request.state.tenant_id` — a busca do usuário já fica escopada por RLS.
- Handler de **login**: resolve/ativa o tenant antes de `verify_user`.

### Atenção operacional
- Rode a nova migração **primeiro numa cópia de staging** do banco: ela adiciona colunas,
  faz backfill e ativa RLS `FORCE`. É idempotente, mas mexe em todas as tabelas.
- O papel do banco na `DATABASE_URL` é dono das tabelas; com `FORCE ROW LEVEL SECURITY`
  o isolamento vale também para ele.
- **Tarefas de fundo, `/ws` e webhook** ainda operam no tenant padrão (1). O roteamento
  por instância→tenant do webhook entra na **Fase 2**.

### Pendências conhecidas (refinar ao onboardar o 2º tenant — Fase 2/3)
Chaves naturais hoje **globais** que precisarão virar compostas com `tenant_id` para
permitir valores repetidos entre empresas (ex.: `system_users.username`, `users.phone`,
`bot_settings.id`, uniques de `metrics`/`contact_lists`). Com RLS, o acesso já é isolado;
falta apenas permitir a **colisão do mesmo texto** entre tenants distintos (envolve
recriar PK/uniques e ajustar cláusulas `ON CONFLICT`).

---

## Fase 2 — Instâncias WhatsApp por tenant (concluída)

### Estratégia
Cada tenant tem sua própria instância Evolution (campo `tenants.evolution_instance`).
O nome da instância deixa de ser fixo (`"vendas"`) e passa a ser resolvido a partir
do tenant.

### `evolution_service.py` (novas funções de provisionamento)
- `get_connection_state(instance)`: consulta bruta do estado da instância.
- `create_instance(instance, token)`: **provisiona** a instância (idempotente: 403/409
  = "já existe" é tratado como sucesso).
- `ensure_instance(instance)`: garante a existência, criando automaticamente se faltar.
- `logout_instance(instance)`: desconecta a instância.

### Roteamento de webhook por `instance` -> `tenant`
- `database.get_tenant_id_by_instance(instance)`: resolve o tenant dono da instância.
- `main.process_webhook`: logo após ler `data['instance']`, resolve o tenant e chama
  `set_current_tenant(...)`. Como o webhook roda em **background task**, esse é o ponto
  correto para ativar o contexto — sem isso, todas as gravações cairiam no tenant padrão.
- Fallback: instância desconhecida -> tenant padrão (1).

### QR por cliente
- `database.get_tenant_instance(tenant_id)`: retorna a instância do tenant.
- `GET /api/admin/evo/status`, `POST /api/admin/evo/connect`, `DELETE /api/admin/evo/logout`:
  a instância agora é **derivada do tenant da requisição** (`request.state.tenant_id`),
  nunca mais do corpo/param enviado pelo cliente — impede um tenant de mexer na
  instância de outro. O fluxo de `connect` já cria a instância se ela não existir
  (auto-provisionamento no primeiro QR).

### Follow-up encadeado na Fase 3 (envios pela UI)
Vários envios iniciados pela interface ainda escolhem a instância por
`ROLE_SESSION_MAP` (papel -> instância, hardcoded) ou por `client_data['session_id']`,
e alguns alertas ao admin usam `"vendas"` fixo (ex.: `main.py` linhas ~1270, 1318,
1701, 2347, 2608, 3257, 3484+). Isso funciona para o tenant 1, mas para novos tenants
precisa passar a resolver a instância via `get_tenant_instance(tenant_id)`. O ajuste
foi deixado para a **Fase 3**, pois depende diretamente dos **papéis/setores dinâmicos
por tenant** (que substituem o `ROLE_SESSION_MAP` fixo) e do `admin_phone` configurável.

## Fase 3 — Configuração e marca por tenant (fim do hardcode)

**Objetivo:** tirar do código (config.py/main.py) tudo que era fixo para o tenant
"continnumOS" e passar a resolver por tenant, com fallback seguro para os padrões
de ambiente. A configuração vive na coluna JSONB `tenants.settings` (criada na
Fase 1), mesclada sobre os defaults.

### Novo módulo `tenant_config.py`
- Resolver central de configuração por tenant, com **cache em memória por tenant**
  (TTL 30s) que permite **leitura síncrona** em funções como `is_business_hours()`,
  evitando reescrever dezenas de call sites como `async`.
- `_default_config()`: padrões compatíveis com o comportamento antigo (nome, cor
  `#FF6600`, `admin_phone` do env, mapa papel->instância, setores, expediente
  08:00–18:00 com almoço 12:00–13:00, texto do follow-up matinal).
- `load_tenant_config(tenant_id)` (async): lê `tenants` + `settings` do banco e
  aquece o cache. `invalidate()` limpa o cache após updates.
- Helpers síncronos: `role_session_map()`, `departamentos()`, `admin_phone()`,
  `morning_followup_message()`, `brand()`, `business_hours()`, `is_business_hours()`.

### `database.py`
- `get_tenant_record(tenant_id)`: retorna o registro do tenant com `settings`
  JSONB já parseado.
- `update_tenant_settings(tenant_id, patch)`: faz merge do patch no JSONB
  `settings` (via operador `||`).

### `main.py` — fim dos hardcodes
- **Horário comercial dinâmico:** `is_business_hours`, `seconds_until_business_hours`,
  `get_seller_auto_status` e `next_business_morning_timestamp` agora leem
  `tc.business_hours()` (abertura/fechamento/almoço) do tenant.
- **Papéis/setores dinâmicos:** todos os `ROLE_SESSION_MAP.get(...)` (linhas ~2608,
  2672, 3257, 3746) passaram a usar `tc.role_session_map()` com fallback para a
  instância do tenant (`get_tenant_instance`), **resolvendo o follow-up da Fase 2**.
- **admin_phone configurável:** os 3 pontos de alerta ao admin (disjuntor global,
  supressão de contato, monitor de sistema) usam `tc.admin_phone()` e enviam pela
  **instância do tenant** (não mais `"vendas"` fixo).
- **Textos do bot:** o follow-up matinal usa `tc.morning_followup_message()`.
- **Aquecimento do cache:** `load_tenant_config` é chamado no startup (tenant
  padrão), no login, no `process_webhook` (por evento) e no `get_current_user`.

### Endpoints novos
- `GET /api/tenant/branding` (público): nome, cor e logo do tenant resolvido pelo
  host — para a tela de login aplicar a marca antes do auth.
- `POST /api/admin/tenant/config` (somente admin): atualiza `display_name`,
  `brand_color`, `logo_url`, `admin_phone`, `role_session_map`, `departamentos`,
  `business_hours` e `morning_followup_message` do tenant atual (whitelist de campos).

### `auth.py`
- `get_current_user` aquece a config do tenant após ativar o contexto, garantindo
  que os handlers tenham horário/marca corretos por requisição.

### `evolution_service.py`
- `send_status(..., background_color="#FF6600")`: cor do status agora é parametrizável
  pela marca do tenant (fallback laranja continnumOS).

### Pendências deixadas para depois
- **Aplicar a marca (logo/cores) nos templates HTML/CSS** faz parte da trilha de
  redesign visual; o backend já expõe os dados via `/api/tenant/branding`.
- Painel de administração dessas configurações (UI) será entregue junto do
  **super-admin da Fase 4**.
- `bot_settings` (greeting/menu/off_hours) já é tenant-scoped via RLS; migrar esses
  textos para o resolver unificado é opcional.

## Fase 4 — Billing e super-admin

**Objetivo:** transformar os tenants em clientes gerenciáveis: planos com limites,
cobrança (ledger de faturas), suspensão/reativação e um painel de super-admin para
gestão da carteira de clientes.

### Schema (migração idempotente)
- `tenants` ganhou: `plan` (default `free`), `plan_overrides` (JSONB, limites
  customizados por tenant), `billing_status` e `trial_ends_at`.
- Nova tabela global **`plans`** (SEM RLS): `code`, `name`, `price_cents`,
  `max_users`, `max_contacts`, `max_messages_month`, `max_instances`, `features`.
  Seed inicial: **Free** (2 usuários / 500 contatos / 2.000 msgs/mês / 1 instância),
  **Pro** (10 / 20.000 / 100.000 / 3) e **Enterprise** (ilimitado, 5 instâncias).
- Nova tabela global **`invoices`** (ledger, SEM RLS): `tenant_id`, período,
  `amount_cents`, `status` (open/paid), `due_date`, `paid_at`.

### `database.py` — helpers de billing
- `get_tenant_status`, `get_tenant_route_by_instance` (roteamento + status),
  `get_all_plans`, `get_plan`, `list_tenants`, `create_tenant`,
  `set_tenant_status`, `set_tenant_plan`, `get_tenant_limits` (plano + overrides),
  `count_scoped` (contagem por tenant respeitando RLS via GUC), `get_tenant_usage`,
  `check_tenant_limit`, `list_invoices`, `create_invoice`, `mark_invoice_paid`.

### Suspensão (enforcement)
- **Login** bloqueado para tenant suspenso (mensagem "Conta suspensa").
- **`get_current_user`** retorna `None` se o tenant estiver suspenso (derruba sessão).
- **Webhook** passa a rotear por instância via `get_tenant_route_by_instance` e
  **ignora eventos de tenants suspensos** (antes cairia no tenant padrão).

### Limites (enforcement)
- Criação de **usuário do sistema** (`/api/admin/users`) e de **contato**
  (`/api/contacts/new`) checam `check_tenant_limit` e retornam **HTTP 402** com
  mensagem de upgrade quando o limite do plano é atingido. `-1` = ilimitado.

### Super-admin (painel de gestão de clientes)
- Autenticação por **token** (`SUPER_ADMIN_TOKEN` no `.env`; vazio = desabilitado)
  via header `X-Superadmin-Token`, comparado com `hmac.compare_digest`.
- Página `GET /superadmin`: painel HTML autocontido (lista clientes, cria cliente
  + provisiona instância, muda plano, suspende/ativa, mostra uso e faturas).
- API: `GET/POST /api/superadmin/tenants`, `POST .../plan`, `.../suspend`,
  `.../activate`, `GET .../usage`, `GET/POST .../invoices`,
  `POST /api/superadmin/invoices/{id}/pay`.
- Criar tenant já **provisiona a instância Evolution** (`ensure_instance`).

### `config.py`
- Novo `SUPER_ADMIN_TOKEN` (opcional). Sem token, os endpoints de super-admin
  ficam desligados por padrão (fail-safe).

### Pendências / notas
- **Cobrança real (gateway Stripe/Pagar.me/Asaas)** não foi integrada por exigir
  chaves/credenciais externas; o ledger de faturas está pronto para plugar o webhook
  de pagamento depois. Marcar fatura como paga hoje é manual (super-admin).
- Enforcement de `max_instances` fica natural na Fase 5 (múltiplas instâncias por
  tenant); hoje o provisionamento cria 1 instância por tenant.
- Suspensão automática por inadimplência (past_due -> suspended) pode ser ligada a
  um loop de verificação de faturas vencidas.

## Fase 5 — Escala e operação

**Objetivo:** preparar o sistema para operar em escala e com múltiplos workers/
máquinas: storage de mídia desacoplado do disco, observabilidade centralizável,
CI/CD, testes automatizados e backups por tenant.

### Object storage (S3/R2) — `services/storage.py` (novo)
- Camada única de storage com dois backends: **`local`** (padrão, 100%% compatível
  com o comportamento atual) e **`s3`** (qualquer provedor compatível com S3:
  AWS S3, Cloudflare R2, MinIO, Backblaze B2).
- API: `save_media(tenant_id, filename, data)`, `read_media(reference)`,
  `delete_tenant_media(tenant_id)`. A referência gravada no banco é um caminho
  `/midias/tN/...` (local) ou uma URL pública (s3).
- **Mídia namespaceada por tenant** (`tN/arquivo`) — habilita backup, expurgo e
  auditoria por cliente. Caminhos legados (`/midias/arquivo`) continuam legíveis.
- Os **5 pontos de mídia** do `main.py` (avatar, chat interno, envio ao cliente,
  encaminhamento e download via webhook) passaram a usar a camada de storage,
  removendo a dependência direta de `/data/midias` e de disco compartilhado.
- `boto3` adicionado ao `requirements.txt` (import tardio: só carrega no backend s3).
- Novas envs: `STORAGE_BACKEND`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION`,
  `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_PUBLIC_BASE_URL`. Boot valida a
  config s3 (`validate_storage`) e falha cedo se faltar bucket/credenciais.

### Observabilidade
- **Logging estruturado** controlável por env: `LOG_JSON` (JSON serializado p/
  Loki/Datadog/ELK), `LOG_LEVEL` e `LOG_RETENTION_DAYS`.
- **Middleware de request-id + access-log**: gera/propaga `X-Request-ID` e registra
  método, rota, status, latência e tenant de cada requisição de API/webhook/login.
- O endpoint `/health` (já existente) continua expondo DB/Redis/Evolution + CPU/
  memória/disco/uptime e agora é usado como **healthcheck** do container.

### CI/CD — `.github/workflows/ci.yml` (novo)
- Pipeline em push/PR: instala deps, roda `compileall` (compile check), executa
  `pytest` e, em seguida, faz o **build da imagem Docker**.

### Testes automatizados — `tests/` (novo)
- `pytest` + `pytest-asyncio` (modo auto via `pytest.ini`).
- Cobertura: validação de config (env obrigatórias + storage s3), storage local
  (roundtrip, namespace por tenant, caminho legado, expurgo), merge do resolver de
  config por tenant e utilitários de runtime.
- `tests/conftest.py` injeta as envs obrigatórias antes dos imports.

### Backups por tenant — `scripts/backup_tenant.py` (novo)
- Exporta os dados de um tenant (ou `--all`) para JSON (com `--gzip`), **respeitando
  o isolamento por RLS** (roda com `app.current_tenant` setado, então só enxerga o
  cliente alvo). Ideal para rodar por cron/CI. Saída em `BACKUP_DIR`.
- Backup de arquivos de mídia: no local a mídia por tenant já fica em
  `/data/midias/tN`; no s3 use lifecycle/replicação do bucket ou `aws s3 sync`.

### Infra
- **Dockerfile**: cria `/data/midias`, `/data/backups`, `/app/logs` e adiciona
  `HEALTHCHECK` batendo em `/health`.
- **docker-compose**: healthcheck do serviço `bot`.

### Pendências / notas
- Os testes **não puderam ser executados no ambiente de geração** (sem acesso à
  internet para instalar `pytest`/`aiofiles`); a lógica pura (config, storage,
  tenant_config, runtime) foi validada manualmente e todos os arquivos passam em
  `py_compile`. Rode `pip install -r requirements-dev.txt && pytest` localmente/CI.
- Migração da mídia legada existente para S3 (se adotado) exige um job de
  `sync` único do conteúdo atual de `/data/midias` para o bucket.
- Métricas Prometheus (`/metrics`) e tracing distribuído (OpenTelemetry) podem ser
  plugados sobre o middleware de observabilidade já introduzido.
