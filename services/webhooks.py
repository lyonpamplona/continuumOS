"""Fase 9: entrega de webhooks de saída (outbound) por tenant.

- `emit(event, payload)` enfileira a entrega no job queue (Fase 7) quando disponível,
  ou entrega inline como fallback. Deve ser chamada DENTRO do contexto do tenant.
- `deliver(job)` é o handler registrado no job queue: resolve endpoints ativos,
  assina o corpo com HMAC-SHA256 e faz POST, registrando o resultado.

O payload assinado segue o mesmo padrão do gateway de pagamento (Fase 6):
cabeçalho `X-continnumOS-Signature: sha256=<hex>` sobre o corpo JSON cru.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

import database as db

logger = logging.getLogger("webhooks")

EVENT_MESSAGE_RECEIVED = "message.received"
EVENT_MESSAGE_SENT = "message.sent"
EVENT_CONTACT_CREATED = "contact.created"


def _sign(secret: str, body: bytes) -> str:
    return hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()


async def emit(tenant_id, event: str, data: dict):
    """Enfileira (ou entrega) um evento de webhook para o tenant informado.

    Seguro para chamar sempre: se não houver endpoints, o handler simplesmente
    não faz nada. Nunca levanta exceção para o chamador.
    """
    try:
        job = {"tenant_id": int(tenant_id), "event": event, "data": data, "ts": time.time()}
        # Entrega em background para não bloquear o caminho de processamento da
        # mensagem. O handler `deliver` também está registrado no job queue
        # (deliver_webhook) para quando a emissão vier do worker.
        asyncio.create_task(deliver(job))
    except Exception as e:
        logger.warning("Falha ao emitir webhook %s: %s", event, e)


async def deliver(job: dict):
    """Handler de entrega: resolve endpoints, assina e faz POST. Idempotente-safe."""
    tenant_id = job.get("tenant_id")
    event = job.get("event")
    data = job.get("data", {})
    if tenant_id is None or not event:
        return
    endpoints = await db.get_active_webhooks_for_event(tenant_id, event)
    if not endpoints:
        return
    body_obj = {"event": event, "tenant_id": int(tenant_id), "data": data, "sent_at": time.time()}
    body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
    async with httpx.AsyncClient(timeout=10.0) as client:
        for ep in endpoints:
            headers = {
                "Content-Type": "application/json",
                "X-continnumOS-Event": event,
            }
            if ep.get("secret"):
                headers["X-continnumOS-Signature"] = "sha256=" + _sign(ep["secret"], body)
            status = "error"
            status_code = None
            error = None
            try:
                resp = await client.post(ep["url"], content=body, headers=headers)
                status_code = resp.status_code
                status = "ok" if 200 <= resp.status_code < 300 else "failed"
            except Exception as e:
                error = str(e)
            try:
                await db.record_webhook_delivery(
                    tenant_id, ep.get("id"), event, status, status_code, error
                )
            except Exception:
                pass
