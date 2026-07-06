"""Configuração e marca por tenant (Fase 3).

Resolve configurações que antes eram hardcoded/env — papel->instância, setores,
telefone do admin, horário comercial, textos do bot e marca (nome/cor/logo) —
a partir do registro do tenant (coluna JSONB `tenants.settings`), com fallback
para os padrões de ambiente.

Mantém um cache em memória por tenant para permitir leitura SÍNCRONA rápida em
funções como `is_business_hours()`, evitando reescrever dezenas de call sites.
O cache é aquecido (async) no fluxo de requisição/login/webhook e no startup.
"""

import time
from datetime import datetime, timezone, timedelta

from config import settings
from tenant_context import get_current_tenant

BR_TIMEZONE = timezone(timedelta(hours=-3))

_CACHE = {}            # tenant_id -> {"data": dict, "ts": float}
_CACHE_TTL = 30.0      # segundos


def _default_config() -> dict:
    """Padrões (compatíveis com o comportamento hardcoded do tenant continnumOS)."""
    return {
        "display_name": "continnumOS",
        "brand_color": "#FF6600",
        "logo_url": "",
        "admin_phone": settings.ADMIN_PHONE,
        "evolution_instance": "vendas",
        "role_session_map": {
            "vendas_1": "vendas",
            "vendas_2": "vendas",
            "financeiro": "financeiro",
            "engenharia": "engenharia",
        },
        "departamentos": dict(getattr(settings, "DEPARTAMENTOS", {})),
        "business_hours": {
            "open": 8 * 60,
            "close": 18 * 60,
            "lunch_start": 12 * 60,
            "lunch_end": 13 * 60,
        },
        "morning_followup_message": (
            "Bom dia! Estamos online novamente. Seu atendimento foi registrado, "
            "o vendedor já foi notificado e ele irá entrar em contato."
        ),
    }


def _merge(base: dict, override) -> dict:
    """Merge raso com merge de segundo nível para dicts conhecidos (ex.: business_hours)."""
    result = dict(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            merged = dict(result[key])
            merged.update(value)
            result[key] = merged
        elif value is not None:
            result[key] = value
    return result


def get_cached_config(tenant_id=None) -> dict:
    """Retorna a config em cache do tenant (ou os padrões, se ainda não aquecida)."""
    tid = int(tenant_id) if tenant_id is not None else get_current_tenant()
    entry = _CACHE.get(tid)
    if entry:
        return entry["data"]
    return _default_config()


async def load_tenant_config(tenant_id=None, force: bool = False) -> dict:
    """Carrega (e cacheia) a config do tenant a partir do banco."""
    import database as db  # import tardio para evitar ciclo de import

    tid = int(tenant_id) if tenant_id is not None else get_current_tenant()
    entry = _CACHE.get(tid)
    if entry and not force and (time.time() - entry["ts"] < _CACHE_TTL):
        return entry["data"]

    data = _default_config()
    try:
        row = await db.get_tenant_record(tid)
    except Exception:
        row = None
    if row:
        top = {
            "display_name": row.get("display_name"),
            "brand_color": row.get("brand_color"),
            "evolution_instance": row.get("evolution_instance"),
        }
        data = _merge(data, {k: v for k, v in top.items() if v})
        data = _merge(data, row.get("settings") or {})

    _CACHE[tid] = {"data": data, "ts": time.time()}
    return data


def invalidate(tenant_id=None) -> None:
    """Invalida o cache (de um tenant ou de todos)."""
    if tenant_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(int(tenant_id), None)


# --------------------------------------------------------------------------
# Helpers síncronos (leem do cache; caem nos padrões se ainda não aquecido)
# --------------------------------------------------------------------------

def role_session_map(tenant_id=None) -> dict:
    return dict(get_cached_config(tenant_id).get("role_session_map", {}))


def departamentos(tenant_id=None) -> dict:
    return dict(get_cached_config(tenant_id).get("departamentos", {}))


def admin_phone(tenant_id=None) -> str:
    return get_cached_config(tenant_id).get("admin_phone", "") or ""


def morning_followup_message(tenant_id=None) -> str:
    return get_cached_config(tenant_id).get("morning_followup_message", "")


def brand(tenant_id=None) -> dict:
    cfg = get_cached_config(tenant_id)
    return {
        "display_name": cfg.get("display_name", ""),
        "brand_color": cfg.get("brand_color", "#FF6600"),
        "logo_url": cfg.get("logo_url", ""),
    }


def business_hours(tenant_id=None):
    """Retorna (open_min, close_min, lunch_start_min, lunch_end_min)."""
    bh = get_cached_config(tenant_id).get("business_hours", {})
    return (
        int(bh.get("open", 8 * 60)),
        int(bh.get("close", 18 * 60)),
        int(bh.get("lunch_start", 12 * 60)),
        int(bh.get("lunch_end", 13 * 60)),
    )


def is_business_hours(now: datetime = None, tenant_id=None) -> bool:
    agora = now or datetime.now(BR_TIMEZONE)
    minutos = agora.hour * 60 + agora.minute
    open_m, close_m, _, _ = business_hours(tenant_id)
    return open_m <= minutos <= close_m
