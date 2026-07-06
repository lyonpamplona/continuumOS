"""Fase 6 — Rate limiting por (tenant, IP) usando Redis.

Janela fixa incremental (INCR + EXPIRE) por chave ``rl:{scope}:{tenant}:{ip}``.
Simples, distribuído (funciona com múltiplos workers/réplicas — Fase 7) e barato.

Protege endpoints de API e, principalmente, o ``/webhook`` (custo/abuso da
Evolution). Falha "aberto" (permite) se o Redis estiver indisponível, para não
derrubar o produto por causa do limitador.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    limit: int
    retry_after: int


async def check_rate_limit(
    redis_client,
    scope: str,
    tenant_id,
    client_ip: str,
    limit: int,
    window_seconds: int,
) -> RateLimitResult:
    """Incrementa o contador da janela e decide se a requisição é permitida.

    Retorna sempre um :class:`RateLimitResult`. Em caso de erro no Redis,
    permite a requisição (fail-open) e loga o problema.
    """
    if limit <= 0 or window_seconds <= 0 or redis_client is None:
        return RateLimitResult(True, -1, limit, 0)

    key = f"rl:{scope}:{tenant_id}:{client_ip}"
    try:
        current = await redis_client.incr(key)
        if current == 1:
            await redis_client.expire(key, window_seconds)
        if current > limit:
            ttl = await redis_client.ttl(key)
            retry_after = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
            return RateLimitResult(False, 0, limit, retry_after)
        return RateLimitResult(True, max(0, limit - current), limit, 0)
    except Exception as exc:  # fail-open: nunca derrubar o produto pelo limitador
        logger.warning(f"[RATE_LIMIT] Redis indisponível, permitindo requisição: {exc}")
        return RateLimitResult(True, -1, limit, 0)


def client_ip_from_request(request) -> str:
    """Extrai o IP do cliente respeitando X-Forwarded-For (atrás de proxy/TLS)."""
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"
