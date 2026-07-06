# API Pública & Webhooks de Saída — continnumOS CRM

A API pública permite que sistemas do cliente leiam contatos/mensagens, enviem
mensagens e consumam analytics do seu tenant. Os webhooks de saída notificam seus
sistemas em tempo real quando eventos acontecem.

## Autenticação

Todas as rotas `/api/v1/*` exigem uma **API key** no cabeçalho:

```
Authorization: Bearer ebk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Gerando uma API key

Como admin do tenant (permissão `settings:manage`):

```
POST /api/apikeys
{ "name": "integração-erp", "scopes": "read,write" }
```

A resposta traz o campo `key` **uma única vez** — guarde com segurança. Liste com
`GET /api/apikeys` e revogue com `DELETE /api/apikeys/{id}`.

Escopos: `read` (somente leitura) e `write` (envio de mensagens). O rate limiting
por tenant (Fase 6) também se aplica às chamadas de API.

## Endpoints REST (v1)

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/v1/ping` | Testa a autenticação. |
| GET | `/api/v1/contacts?limit=&offset=` | Lista contatos (paginado). |
| GET | `/api/v1/messages?phone=&limit=` | Histórico de um contato. |
| GET | `/api/v1/analytics?days=30` | Analytics do tenant. |
| POST | `/api/v1/messages` | Envia mensagem (`write`). |

### Enviar mensagem

```
POST /api/v1/messages
Authorization: Bearer <key>
{ "phone": "5511999999999", "text": "Olá!", "session_id": "vendas" }
```

## Analytics

`GET /api/v1/analytics?days=30` retorna:

```json
{
  "window_days": 30,
  "totals": {"contacts": 0, "active_contacts": 0, "messages": 0, "inbound": 0, "outbound": 0},
  "messages_per_day": [{"day": "2026-01-01", "total": 0}]
}
```

O mesmo dado alimenta o painel interno em `GET /api/analytics` (requer login +
permissão `metrics:view`).

## Webhooks de saída

Registre endpoints que receberão eventos via HTTP POST:

```
POST /api/webhooks
{ "url": "https://seu-sistema/webhook", "events": "message.received,message.sent", "secret": "seg redo" }
```

- `events`: lista separada por vírgula ou `*` para todos.
- Liste com `GET /api/webhooks`; remova com `DELETE /api/webhooks/{id}`.

### Eventos disponíveis

| Evento | Quando |
|--------|--------|
| `message.received` | Mensagem recebida de um contato. |
| `message.sent` | Mensagem enviada via API pública. |
| `contact.created` | Novo contato criado. |

### Formato e assinatura

Corpo (JSON):

```json
{ "event": "message.received", "tenant_id": 1, "data": { "phone": "...", "text": "..." }, "sent_at": 0 }
```

Se o endpoint tiver `secret`, enviamos a assinatura HMAC-SHA256 do corpo cru:

```
X-continnumOS-Signature: sha256=<hex>
X-continnumOS-Event: message.received
```

Valide recomputando o HMAC do corpo recebido com seu `secret` e comparando de
forma constante. Entregas são registradas em `webhook_deliveries` (status/erro).

## Marketplace de integrações

- `GET /api/integrations`: catálogo + estado (habilitado/config) do tenant.
- `POST /api/integrations/{key}`: `{ "enabled": true, "config": { ... } }`.

Integrações atuais: Webhooks, API pública, Zapier, n8n, Google Sheets, Slack.
