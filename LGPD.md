# Política de Privacidade e Conformidade LGPD

Este documento descreve como o continnumOS CRM trata dados pessoais em conformidade
com a Lei nº 13.709/2018 (LGPD).

## 1. Papéis (LGPD)

- **Controlador:** o cliente (tenant) que opera o CRM e decide as finalidades do
  tratamento dos dados dos seus contatos.
- **Operador:** o provedor da plataforma continnumOS CRM, que trata os dados em nome
  do controlador conforme o DPA (ver `DPA.md`).
- **Titular:** a pessoa física (contato/cliente final) cujos dados são tratados.

## 2. Dados tratados

- Identificação e contato: nome, telefone (WhatsApp).
- Conteúdo de conversas: mensagens, mídias, eventos de agenda, tags e listas.
- Metadados operacionais: horários, status de atendimento, métricas.

## 3. Isolamento multi-tenant

Cada cliente (tenant) tem seus dados isolados por **Row Level Security (RLS)** no
PostgreSQL. Um tenant nunca acessa dados de outro. Testes automatizados de
isolamento validam essa garantia (ver `tests/test_rls_isolation.py`).

## 4. Direitos do titular

O controlador (admin do tenant, com permissão `lgpd:manage`) pode atender aos
direitos do titular diretamente pela plataforma:

### 4.1 Confirmação e acesso / portabilidade (exportação)

```
GET /api/lgpd/export?phone=<telefone>
```

Retorna, em JSON, todos os registros do titular no tenant corrente (usuário,
mensagens, eventos de agenda, tags e listas). A operação é registrada em auditoria.

### 4.2 Eliminação (direito ao esquecimento)

```
POST /api/lgpd/delete
{ "phone": "<telefone>", "confirm": true }
```

Apaga definitivamente os dados do titular no tenant corrente e retorna a contagem
por tabela. A operação exige `confirm: true` e é registrada em auditoria.

## 5. Retenção

- Configurável por `DATA_RETENTION_DAYS` (dias). Com valor > 0, um job diário
  remove mensagens mais antigas que o limite, em todos os tenants.
- Com `DATA_RETENTION_DAYS=0` (padrão), a limpeza automática fica desativada e a
  retenção passa a ser responsabilidade do controlador.

## 6. Auditoria

Ações sensíveis (exportação/exclusão LGPD, mudança de plano, etc.) são gravadas em
`audit_logs`, tabela também isolada por tenant via RLS, contendo autor, ação,
telefone/alvo, detalhes e data.

## 7. Segurança

- Senhas com hash **bcrypt**; sessões via **JWT** em cookie HTTP-only.
- Segredos gerenciados fora do código (ver gestão de segredos da Fase 6).
- Comunicação recomendada sob TLS/HTTPS.
- Backups gerenciados e teste de restore documentados (ver `DR.md`).

## 8. Encarregado (DPO)

Contato do encarregado configurável via `DPO_CONTACT_EMAIL`. Recomenda-se publicar
este contato ao titular para o exercício de direitos.
