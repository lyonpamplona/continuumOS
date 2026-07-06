"""Fase 7 — Worker de jobs (consumidor da fila Redis).

Executa separado do servidor web (``python worker.py``). Reutiliza os handlers
registrados em ``main`` (ex.: ``process_webhook``), inicializa o pool do banco e
o config de tenant padrão, e consome a fila até receber SIGINT/SIGTERM.

Deploy sugerido: 1+ réplicas deste worker com ``JOB_QUEUE_ENABLED=true``; as
réplicas web ficam só servindo HTTP (mais responsivas).
"""

from __future__ import annotations

import asyncio
import signal

from loguru import logger

# Importar main registra os handlers (process_webhook etc.) e o redis_client.
import main
import database as db
import tenant_config as tc
from config import settings
from services import jobqueue
from tenant_context import DEFAULT_TENANT_ID


async def _run() -> None:
    logger.info("[WORKER] Inicializando (fila='{}')...", settings.JOB_QUEUE_KEY)
    await db.init_db()
    try:
        await tc.load_tenant_config(DEFAULT_TENANT_ID)
    except Exception as exc:
        logger.warning("[WORKER] Não foi possível carregar config do tenant padrão: {}", exc)

    stop_event = asyncio.Event()

    def _stop(*_):
        logger.info("[WORKER] Sinal de parada recebido.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # ex.: Windows
            pass

    try:
        await jobqueue.run_worker(main.redis_client, stop_event=stop_event)
    finally:
        await db.close_db()
        try:
            await main.redis_client.close()
        except Exception:
            pass
        logger.info("[WORKER] Encerrado.")


if __name__ == "__main__":
    asyncio.run(_run())
