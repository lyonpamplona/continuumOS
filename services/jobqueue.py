"""Fase 7 — Fila de jobs simples baseada em Redis (sem dependências novas).

Objetivo: tirar processamento pesado (webhooks, mídia) do caminho da requisição.
Usa uma lista Redis (RPUSH/BLPOP) como broker — já temos Redis no stack, então
não precisamos de Celery/RQ/Arq para o caso básico. O contrato é miníimo e pode
ser trocado por Arq/Celery depois sem mudar os call sites (submit/register).

Modos:
- ``JOB_QUEUE_ENABLED=true``: ``submit`` enfileira no Redis; o ``worker.py`` consome.
- ``JOB_QUEUE_ENABLED=false`` (padrão): ``submit`` executa inline (BackgroundTasks
  do FastAPI quando disponível, senão cria uma task asyncio).

Os handlers são registrados por nome (``register``) tanto na app quanto no worker,
para que o payload serializado saiba qual função chamar.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

from loguru import logger

from config import settings

try:  # métricas são best-effort
    from services import observability
except Exception:  # pragma: no cover
    observability = None

# Registro de handlers: nome -> coroutine(payload: dict)
_HANDLERS: dict = {}


def register(name: str, handler: Callable[[dict], Awaitable]) -> None:
    """Registra um handler assíncrono para um tipo de job."""
    _HANDLERS[name] = handler


def _metric(event: str) -> None:
    if observability is not None:
        try:
            observability.inc(event)
        except Exception:
            pass


async def enqueue(redis_client, name: str, payload: dict) -> bool:
    """Empurra um job para a fila Redis. Retorna True se enfileirou."""
    if redis_client is None:
        return False
    body = json.dumps({"task": name, "payload": payload}, ensure_ascii=False)
    await redis_client.rpush(settings.JOB_QUEUE_KEY, body)
    _metric("job_enqueued")
    return True


async def _run_handler(name: str, payload: dict) -> None:
    handler = _HANDLERS.get(name)
    if handler is None:
        logger.error("[JOBQUEUE] Handler não registrado para task '{}'.", name)
        _metric("job_no_handler")
        return
    try:
        await handler(payload)
        _metric("job_done")
    except Exception as exc:
        logger.exception("[JOBQUEUE] Falha no job '{}': {}", name, exc)
        _metric("job_failed")
        if observability is not None:
            observability.capture_exception(exc)


async def submit(
    redis_client,
    name: str,
    payload: dict,
    background_tasks=None,
) -> str:
    """Enfileira (modo fila) ou executa inline (padrão) um job.

    Retorna "queued" | "inline" | "inline_task" para observabilidade/testes.
    """
    if settings.JOB_QUEUE_ENABLED and redis_client is not None:
        try:
            await enqueue(redis_client, name, payload)
            return "queued"
        except Exception as exc:
            logger.warning("[JOBQUEUE] Falha ao enfileirar '{}', caindo para inline: {}", name, exc)

    # Fallback inline (comportamento atual, sem regressão).
    if background_tasks is not None:
        background_tasks.add_task(_run_handler, name, payload)
        return "inline"
    asyncio.create_task(_run_handler(name, payload))
    return "inline_task"


async def run_worker(redis_client, stop_event: Optional[asyncio.Event] = None) -> None:
    """Loop do worker: consome a fila Redis e despacha para os handlers.

    Usa BLPOP com timeout para permitir checagens periódicas de parada.
    """
    if redis_client is None:
        raise RuntimeError("run_worker requer um redis_client.")
    key = settings.JOB_QUEUE_KEY
    block = max(1, int(settings.JOB_QUEUE_BLOCK_SECONDS))
    logger.info("[JOBQUEUE] Worker iniciado (fila='{}', handlers={}).", key, list(_HANDLERS))
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("[JOBQUEUE] Worker encerrando (stop_event).")
            return
        try:
            item = await redis_client.blpop(key, timeout=block)
            if not item:
                continue
            # item = (key, value); value pode ser bytes
            _, raw = item
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            msg = json.loads(raw)
            await _run_handler(msg.get("task", ""), msg.get("payload", {}) or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[JOBQUEUE] Erro no loop do worker: {}", exc)
            await asyncio.sleep(1)
