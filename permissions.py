"""Fase 8 — Roles e permissões granulares por tenant.

Modelo aditivo e retrocompatível: os papéis existentes (admin, vendas_1,
vendas_2, financeiro, engenharia) continuam funcionando. Aqui centralizamos o
mapa role -> permissões e helpers para checagem, permitindo controlar ações de
forma fina sem espalhar `if role == 'admin'` pelo código.

- ``admin`` tem todas as permissões (curinga ``*``).
- Papeis desconhecidos recebem o conjunto padrão de ``agent`` (leitura + envio).
"""

from __future__ import annotations

# Catálogo de permissões conhecidas (use estas constantes nos endpoints).
PERM_VIEW_CHATS = "chats:view"
PERM_SEND_MESSAGE = "chats:send"
PERM_MANAGE_CONTACTS = "contacts:manage"
PERM_IMPORT_CONTACTS = "contacts:import"
PERM_MANAGE_USERS = "users:manage"
PERM_MANAGE_BILLING = "billing:manage"
PERM_VIEW_METRICS = "metrics:view"
PERM_MANAGE_SETTINGS = "settings:manage"
PERM_CONNECT_WHATSAPP = "whatsapp:connect"
PERM_MANAGE_LGPD = "lgpd:manage"
PERM_TAKEOVER = "chats:takeover"

ALL_PERMISSIONS = {
    PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_MANAGE_CONTACTS, PERM_IMPORT_CONTACTS,
    PERM_MANAGE_USERS, PERM_MANAGE_BILLING, PERM_VIEW_METRICS, PERM_MANAGE_SETTINGS,
    PERM_CONNECT_WHATSAPP, PERM_MANAGE_LGPD, PERM_TAKEOVER,
}

# Conjunto padrão para um agente/vendedor.
_AGENT = {
    PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_MANAGE_CONTACTS, PERM_TAKEOVER,
}

ROLE_PERMISSIONS = {
    "admin": {"*"},
    "manager": {
        PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_MANAGE_CONTACTS, PERM_IMPORT_CONTACTS,
        PERM_VIEW_METRICS, PERM_TAKEOVER, PERM_CONNECT_WHATSAPP,
    },
    "vendas_1": set(_AGENT),
    "vendas_2": set(_AGENT),
    "financeiro": {PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_VIEW_METRICS},
    "engenharia": {PERM_VIEW_CHATS, PERM_SEND_MESSAGE},
    "billing_admin": {PERM_MANAGE_BILLING, PERM_VIEW_METRICS},
    "viewer": {PERM_VIEW_CHATS, PERM_VIEW_METRICS},
}


def permissions_for(role: str) -> set:
    """Retorna o conjunto de permissões de um papel (curinga vira todas)."""
    role = (role or "").strip().lower()
    perms = ROLE_PERMISSIONS.get(role)
    if perms is None:
        return set(_AGENT)
    if "*" in perms:
        return set(ALL_PERMISSIONS)
    return set(perms)


def has_permission(role: str, permission: str) -> bool:
    """True se o papel possui a permissão (admin sempre True)."""
    role = (role or "").strip().lower()
    if role == "admin":
        return True
    return permission in permissions_for(role)


def user_has_permission(user: dict, permission: str) -> bool:
    """Helper para checar a permissão a partir do dict de usuário (campo role)."""
    if not user:
        return False
    return has_permission(user.get("role", ""), permission)


def list_roles() -> list:
    """Lista os papéis disponíveis com suas permissões (para telas de admin)."""
    out = []
    for role in ROLE_PERMISSIONS:
        out.append({"role": role, "permissions": sorted(permissions_for(role))})
    return out
