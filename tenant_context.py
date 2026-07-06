"""Contexto de tenant por requisição (Fase 1 — Multi-tenancy).

Mantém o tenant atual em um `contextvar`, propagado pela task assíncrona da
requisição. O pool `asyncpg` lê esse valor no hook `setup` e aplica a GUC
`app.current_tenant` em cada conexão adquirida, ativando o isolamento por
Row-Level Security (RLS) no PostgreSQL. Assim, todas as queries ficam
automaticamente restritas ao tenant corrente, sem reescrever cada consulta.
"""

import contextvars

# Tenant padrão = continnumOS (dados legados migrados para o tenant 1).
DEFAULT_TENANT_ID = 1

_current_tenant_id: contextvars.ContextVar = contextvars.ContextVar(
    "current_tenant_id", default=DEFAULT_TENANT_ID
)


def set_current_tenant(tenant_id):
    """Define o tenant atual para a task/requisição corrente. Retorna o token."""
    try:
        tid = int(tenant_id)
    except (TypeError, ValueError):
        tid = DEFAULT_TENANT_ID
    return _current_tenant_id.set(tid)


def get_current_tenant() -> int:
    """Retorna o id do tenant atual (ou o padrão quando não definido)."""
    return _current_tenant_id.get()


def reset_current_tenant(token) -> None:
    """Restaura o valor anterior do contextvar, ignorando tokens inválidos."""
    try:
        _current_tenant_id.reset(token)
    except (ValueError, LookupError):
        pass
