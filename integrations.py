"""Fase 9: catálogo do marketplace de integrações.

Catálogo estático (sem estado). O estado por tenant (habilitado/config) vive na
tabela `tenant_integrations` (ver database.py). Cada item descreve a integração
e quais campos de configuração ela aceita, para a UI/portal renderizar.
"""

# key -> metadados. `config_fields` guia o formulário de configuração do tenant.
INTEGRATIONS = {
    "webhooks": {
        "name": "Webhooks de saída",
        "category": "Automação",
        "description": "Receba eventos (mensagens, contatos) via HTTP POST assinado (HMAC).",
        "config_fields": [],
        "docs": "/docs/API_PUBLICA.md",
    },
    "public_api": {
        "name": "API pública",
        "category": "Desenvolvedores",
        "description": "Acesse contatos, mensagens e analytics via REST autenticada por API key.",
        "config_fields": [],
        "docs": "/docs/API_PUBLICA.md",
    },
    "zapier": {
        "name": "Zapier",
        "category": "Automação",
        "description": "Conecte a 6000+ apps via Zapier usando a API pública e webhooks.",
        "config_fields": [
            {"key": "api_key", "label": "API Key", "type": "password", "required": True},
        ],
        "docs": "https://zapier.com",
    },
    "n8n": {
        "name": "n8n",
        "category": "Automação",
        "description": "Fluxos self-hosted via webhooks de saída e API pública.",
        "config_fields": [
            {"key": "webhook_url", "label": "URL do webhook n8n", "type": "url", "required": True},
        ],
        "docs": "https://n8n.io",
    },
    "google_sheets": {
        "name": "Google Sheets",
        "category": "Produtividade",
        "description": "Exporte contatos e métricas para uma planilha automaticamente.",
        "config_fields": [
            {"key": "spreadsheet_id", "label": "ID da planilha", "type": "text", "required": True},
        ],
        "docs": "https://developers.google.com/sheets",
    },
    "slack": {
        "name": "Slack",
        "category": "Notificações",
        "description": "Notifique um canal do Slack a cada novo lead ou mensagem.",
        "config_fields": [
            {"key": "incoming_webhook", "label": "Incoming Webhook URL", "type": "url", "required": True},
        ],
        "docs": "https://api.slack.com/messaging/webhooks",
    },
}


def list_catalog():
    """Retorna o catálogo como lista, incluindo a `key` de cada item."""
    return [{"key": k, **v} for k, v in INTEGRATIONS.items()]


def get_integration(key):
    """Retorna os metadados de uma integração ou None."""
    item = INTEGRATIONS.get(key)
    return ({"key": key, **item} if item else None)


def is_valid(key):
    return key in INTEGRATIONS
