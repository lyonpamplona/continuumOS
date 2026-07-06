# Pol&#237;tica de Seguran&#231;a

## Vers&#245;es suportadas

Recebe corre&#231;&#245;es de seguran&#231;a a vers&#227;o em produ&#231;&#227;o (branch `main`).

## Como reportar uma vulnerabilidade

N&#227;o abra uma *issue* p&#250;blica para falhas de seguran&#231;a. Em vez disso:

1. Envie um relato privado ao respons&#225;vel pelo reposit&#243;rio (contato do time / DPO).
2. Inclua passos de reprodu&#231;&#227;o, impacto e, se poss&#237;vel, uma sugest&#227;o de corre&#231;&#227;o.
3. D&#234; um prazo razo&#225;vel para corre&#231;&#227;o antes de qualquer divulga&#231;&#227;o.

## Boas pr&#225;ticas j&#225; adotadas

- **Isolamento multi-tenant** por Row-Level Security (RLS) &#8212; testado automaticamente.
- **Segredos** fora do c&#243;digo: suporte a `*_FILE` / `SECRETS_DIR` (secret manager) e `.env` no `.gitignore`.
- **Sess&#245;es** assinadas por JWT, cookies `Secure`, TTL configur&#225;vel e op&#231;&#227;o de sess&#227;o &#250;nica.
- **Webhook** autenticado por `Bearer` (`WEBHOOK_SECRET`).
- **Rate limiting** por tenant/IP.
- **LGPD**: auditoria, reten&#231;&#227;o, exporta&#231;&#227;o e exclus&#227;o por titular.

## Antes de ir a produ&#231;&#227;o

- Troque **todos** os segredos de exemplo do `.env`.
- Ative HTTPS e mantenha `COOKIE_SECURE=true`.
- Rotacione `JWT_SECRET` e `WEBHOOK_SECRET` periodicamente.
