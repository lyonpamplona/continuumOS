# continnumOS CRM — Guia do Cliente

Bem-vindo(a)! Este guia cobre o essencial para começar a usar o continnumOS CRM.

## 1. Criar sua conta (self-service)

1. Acesse **`/signup`**.
2. Informe **nome da empresa**, **seu nome**, **e-mail** (será seu login) e **senha** (mín. 8 caracteres).
3. Sua conta é criada em **trial gratuito** (padrão: 14 dias) e você recebe a URL de acesso.
   - Se o operador configurou um domínio base, sua conta fica em `suaempresa.SEU-DOMINIO/login`.

## 2. Onboarding guiado

Após o primeiro login, complete os 3 passos (acompanhe em **`/api/onboarding/status`**):

1. **Conectar o WhatsApp** — escaneie o QR Code em *Configurações → WhatsApp*.
2. **Importar contatos** — use a importação em massa na tela de contatos.
3. **Primeira conversa/fluxo** — envie/receba a primeira mensagem para validar o bot.

## 3. Papéis e permissões

Cada usuário do seu time tem um **papel** (role) que define o que pode fazer:

| Papel | Resumo |
|-------|--------|
| `admin` | Acesso total (inclui billing, usuários, LGPD). |
| `manager` | Operação + métricas + conectar WhatsApp. |
| `vendas_1` / `vendas_2` | Atendimento: ver/enviar mensagens, contatos, takeover. |
| `financeiro` | Ver conversas + métricas. |
| `billing_admin` | Gerenciar assinatura/faturas. |
| `viewer` | Somente leitura + métricas. |

Consulte o catálogo completo em **`/api/roles`**.

## 4. Assinatura e faturas (billing)

Acesse **`/billing`** (requer permissão de billing):

- Ver **plano atual**, status e uso.
- **Upgrade/downgrade** de plano.
- **Trocar cartão / pagar** via gateway (quando configurado).
- Histórico de **faturas**.

## 5. Privacidade e LGPD

- **Exportar dados de um titular:** `GET /api/lgpd/export?phone=<telefone>`
- **Excluir dados de um titular:** `POST /api/lgpd/delete` com `{ "phone": "...", "confirm": true }`
- **Retenção automática:** mensagens antigas são removidas conforme a política do operador.

Veja **LGPD.md** e **DPA.md** para detalhes de conformidade.

## 6. Status do serviço

A página pública **`/status`** mostra a saúde de banco de dados, Redis e storage em tempo real.

## 7. Suporte

Em caso de incidente, verifique **`/status`** e contate o suporte do seu provedor.
