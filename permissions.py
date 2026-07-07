"""Fase 8 - Roles e permissoes granulares por tenant (catalogo unificado).

Modelo aditivo e retrocompativel. Uma unica lista de "capacidades" governa
tanto acoes (enviar mensagem, gerenciar usuarios, faturamento...) quanto a
visibilidade dos modulos do painel (caixa unificada, vendas, equipe, IA...).

- ``admin`` tem todas as permissoes (curinga ``*``).
- Papeis customizados por tenant sao passados via ``custom_roles`` e tem
  prioridade sobre o mapa padrao definido em codigo.
- Papeis desconhecidos recebem o conjunto padrao de ``agent`` (leitura + envio).
"""

from __future__ import annotations

# --- Catalogo de capacidades conhecidas (use estas constantes nos endpoints) ---
# Acoes
PERM_VIEW_CHATS = "chats:view"
PERM_SEND_MESSAGE = "chats:send"
PERM_TAKEOVER = "chats:takeover"
PERM_MANAGE_CONTACTS = "contacts:manage"
PERM_IMPORT_CONTACTS = "contacts:import"
PERM_VIEW_AUDIT = "audit:view"
PERM_VIEW_REPORTS = "reports:view"
PERM_VIEW_METRICS = "metrics:view"
PERM_MANAGE_USERS = "users:manage"
PERM_MANAGE_SESSIONS = "sessions:manage"
PERM_MANAGE_EVOLUTION = "evolution:manage"
PERM_CONNECT_WHATSAPP = "evolution:manage"  # alias historico
PERM_MANAGE_SETTINGS = "settings:manage"
PERM_RUN_MAINTENANCE = "maintenance:run"
PERM_MANAGE_BILLING = "billing:manage"
PERM_MANAGE_LGPD = "lgpd:manage"
PERM_COMMUNICATION = "communication:send"
# Modulos do painel (visibilidade de secoes/ferramentas)
PERM_MODULE_OMNICHANNEL = "module:omnichannel"
PERM_MODULE_CAMPAIGNS = "module:campaigns"
PERM_MODULE_SALES = "module:sales"
PERM_MODULE_TEAM = "module:team"
PERM_MODULE_AI = "module:ai"

ALL_PERMISSIONS = {
    PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_TAKEOVER, PERM_MANAGE_CONTACTS,
    PERM_IMPORT_CONTACTS, PERM_VIEW_AUDIT, PERM_VIEW_REPORTS, PERM_VIEW_METRICS,
    PERM_MANAGE_USERS, PERM_MANAGE_SESSIONS, PERM_MANAGE_EVOLUTION,
    PERM_MANAGE_SETTINGS, PERM_RUN_MAINTENANCE, PERM_MANAGE_BILLING,
    PERM_MANAGE_LGPD, PERM_COMMUNICATION, PERM_MODULE_OMNICHANNEL,
    PERM_MODULE_CAMPAIGNS, PERM_MODULE_SALES, PERM_MODULE_TEAM, PERM_MODULE_AI,
}

# Catalogo para telas de admin: rotulos PT-BR agrupados por area.
PERMISSION_CATALOG = [
    {"key": PERM_VIEW_CHATS, "label": "Ver conversas", "group": "Atendimento"},
    {"key": PERM_SEND_MESSAGE, "label": "Enviar mensagens", "group": "Atendimento"},
    {"key": PERM_TAKEOVER, "label": "Assumir atendimentos", "group": "Atendimento"},
    {"key": PERM_COMMUNICATION, "label": "Comunicados e agenda", "group": "Atendimento"},
    {"key": PERM_MANAGE_CONTACTS, "label": "Gerenciar contatos", "group": "Contatos"},
    {"key": PERM_IMPORT_CONTACTS, "label": "Importar contatos", "group": "Contatos"},
    {"key": PERM_VIEW_REPORTS, "label": "Ver relatorios", "group": "Gestao"},
    {"key": PERM_VIEW_METRICS, "label": "Ver metricas", "group": "Gestao"},
    {"key": PERM_VIEW_AUDIT, "label": "Ver auditoria e logs", "group": "Gestao"},
    {"key": PERM_MANAGE_USERS, "label": "Gerenciar usuarios e papeis", "group": "Administracao"},
    {"key": PERM_MANAGE_SESSIONS, "label": "Gerenciar sessoes", "group": "Administracao"},
    {"key": PERM_MANAGE_EVOLUTION, "label": "Conectar/gerir WhatsApp", "group": "Administracao"},
    {"key": PERM_MANAGE_SETTINGS, "label": "Configuracoes globais", "group": "Administracao"},
    {"key": PERM_RUN_MAINTENANCE, "label": "Manutencao do sistema", "group": "Administracao"},
    {"key": PERM_MANAGE_BILLING, "label": "Faturamento e cobranca", "group": "Financeiro"},
    {"key": PERM_MANAGE_LGPD, "label": "LGPD (exportar/excluir dados)", "group": "Financeiro"},
    {"key": PERM_MODULE_OMNICHANNEL, "label": "Modulo: Caixa unificada", "group": "Modulos do painel"},
    {"key": PERM_MODULE_CAMPAIGNS, "label": "Modulo: Campanhas", "group": "Modulos do painel"},
    {"key": PERM_MODULE_SALES, "label": "Modulo: Vendas", "group": "Modulos do painel"},
    {"key": PERM_MODULE_TEAM, "label": "Modulo: Equipe", "group": "Modulos do painel"},
    {"key": PERM_MODULE_AI, "label": "Modulo: IA/Copiloto", "group": "Modulos do painel"},
]

# Mapa grupo administrativo -> capacidade (retrocompat com can_admin_action).
GROUP_TO_PERMISSION = {
    "communication": PERM_COMMUNICATION,
    "contacts": PERM_MANAGE_CONTACTS,
    "audit": PERM_VIEW_AUDIT,
    "reports": PERM_VIEW_REPORTS,
    "users": PERM_MANAGE_USERS,
    "sessions": PERM_MANAGE_SESSIONS,
    "evolution": PERM_MANAGE_EVOLUTION,
    "settings": PERM_MANAGE_SETTINGS,
    "maintenance": PERM_RUN_MAINTENANCE,
}

# Conjunto padrao para um agente/vendedor.
_AGENT = {
    PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_TAKEOVER, PERM_MANAGE_CONTACTS,
    PERM_MODULE_SALES, PERM_MODULE_OMNICHANNEL,
}

DEFAULT_ROLE_PERMISSIONS = {
    "admin": {"*"},
    "manager": {
        PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_TAKEOVER, PERM_MANAGE_CONTACTS,
        PERM_IMPORT_CONTACTS, PERM_VIEW_METRICS, PERM_VIEW_REPORTS,
        PERM_COMMUNICATION, PERM_MANAGE_EVOLUTION, PERM_MODULE_OMNICHANNEL,
        PERM_MODULE_SALES, PERM_MODULE_TEAM, PERM_MODULE_CAMPAIGNS, PERM_MODULE_AI,
    },
    "vendas_1": set(_AGENT),
    "vendas_2": set(_AGENT),
    "financeiro": {
        PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_COMMUNICATION,
        PERM_MANAGE_CONTACTS, PERM_VIEW_AUDIT, PERM_VIEW_REPORTS,
        PERM_VIEW_METRICS, PERM_MANAGE_BILLING, PERM_MODULE_SALES,
    },
    "engenharia": {PERM_VIEW_CHATS, PERM_SEND_MESSAGE, PERM_MODULE_TEAM},
    "billing_admin": {PERM_MANAGE_BILLING, PERM_VIEW_METRICS, PERM_VIEW_REPORTS},
    "viewer": {PERM_VIEW_CHATS, PERM_VIEW_METRICS, PERM_VIEW_REPORTS},
}

# Papeis padrao que nao podem ser removidos/renomeados pela UI.
SYSTEM_ROLES = {"admin", "vendas_1", "vendas_2", "financeiro", "engenharia"}


def _expand(perms: set) -> set:
    """Expande o curinga ``*`` para o conjunto completo de permissoes."""
    if "*" in perms:
        return set(ALL_PERMISSIONS)
    return set(perms)


def permissions_for(role: str, custom_roles: dict | None = None) -> set:
    """Conjunto de permissoes de um papel.

    Prioridade: papel customizado do tenant > mapa padrao em codigo > _AGENT.
    ``custom_roles`` e o mapa {role_key: set(perms)} vindo do banco.
    """
    role = (role or "").strip().lower()
    if role == "admin":
        return set(ALL_PERMISSIONS)
    if custom_roles and role in custom_roles:
        return _expand(set(custom_roles[role]))
    perms = DEFAULT_ROLE_PERMISSIONS.get(role)
    if perms is None:
        return set(_AGENT)
    return _expand(perms)


def has_permission(role: str, permission: str, custom_roles: dict | None = None) -> bool:
    """True se o papel possui a permissao (admin sempre True)."""
    role = (role or "").strip().lower()
    if role == "admin":
        return True
    return permission in permissions_for(role, custom_roles)


def user_has_permission(user: dict, permission: str) -> bool:
    """Checa a permissao a partir do dict de usuario.

    Usa ``user['_perms']`` (permissoes efetivas ja resolvidas com overlay do
    tenant) quando presente; caso contrario cai no mapa padrao pelo campo role.
    """
    if not user:
        return False
    perms = user.get("_perms")
    if isinstance(perms, (set, list, tuple)):
        if user.get("role") == "admin":
            return True
        return permission in perms
    return has_permission(user.get("role", ""), permission)


def group_permission(group: str) -> str:
    """Converte um grupo administrativo legado na capacidade correspondente."""
    return GROUP_TO_PERMISSION.get(group, "")


def list_roles(custom_roles_detail: list | None = None) -> list:
    """Lista papeis (padrao + customizados) com suas permissoes, para a UI."""
    out = []
    for role in DEFAULT_ROLE_PERMISSIONS:
        out.append({
            "role": role,
            "label": role,
            "permissions": sorted(permissions_for(role)),
            "system": True,
        })
    for cr in (custom_roles_detail or []):
        out.append({
            "role": cr.get("role_key"),
            "label": cr.get("label") or cr.get("role_key"),
            "permissions": sorted(cr.get("permissions") or []),
            "system": False,
            "is_active": cr.get("is_active", 1),
            "base_role": cr.get("base_role", ""),
        })
    return out
