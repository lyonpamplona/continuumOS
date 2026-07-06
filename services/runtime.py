"""Utilitários operacionais para healthcheck, locks e métricas de tempo."""

import time
from uuid import uuid4
from contextlib import asynccontextmanager


START_TIME = time.time()


def uptime_seconds() -> int:
    """Retorna há quantos segundos este processo da aplicação está ativo."""
    return int(time.time() - START_TIME)


def format_duration(seconds: int | float | None) -> str:
    """Formata uma duração em segundos para exibição curta nos painéis."""
    total = int(seconds or 0)
    if total <= 0:
        return "--"
    minutes = total // 60
    if minutes < 1:
        return "<1 min"
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins} min"


@asynccontextmanager
async def redis_lock(redis_client, key: str, ttl_seconds: int):
    """Executa um bloco somente quando este worker conseguir adquirir o lock Redis."""
    token = str(uuid4())
    acquired = await redis_client.set(key, token, ex=ttl_seconds, nx=True)
    try:
        yield bool(acquired)
    finally:
        if acquired:
            current = await redis_client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current == token:
                await redis_client.delete(key)


async def check_redis(redis_client) -> dict:
    """Valida conectividade básica com Redis para healthcheck."""
    try:
        pong = await redis_client.ping()
        return {"ok": bool(pong)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
