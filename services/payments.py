"""Fase 6 — Pagamento real: verificação de webhooks de gateways de pagamento.

Esta camada é propositalmente fina e sem SDKs pesados: recebe o corpo bruto do
webhook + headers, valida a autenticidade (assinatura HMAC/token) conforme o
provedor configurado e extrai um evento normalizado que o billing entende.

Provedores suportados (verificação de assinatura):
  - ``manual``      : sem gateway; webhook exige o PAYMENT_WEBHOOK_SECRET no header.
  - ``stripe``      : header ``Stripe-Signature`` (esquema t=..,v1=..), HMAC-SHA256.
  - ``mercadopago`` : header ``x-signature`` (ts + v1), HMAC-SHA256.
  - ``asaas``       : header ``asaas-access-token`` comparado ao segredo.

O objetivo é marcar a fatura como paga e (opcionalmente) reativar o tenant. A
criação de cobranças/checkout no gateway pode ser adicionada depois; o ponto
crítico de "apto para uso real" é **confiar apenas em webhooks assinados**.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Optional

from config import settings


class PaymentWebhookError(Exception):
    """Assinatura inválida ou payload de webhook malformado."""


@dataclass
class PaymentEvent:
    """Evento de pagamento normalizado extraído de um webhook."""

    provider: str
    event_type: str  # ex.: "payment.succeeded", "payment.failed", "other"
    paid: bool = False
    invoice_id: Optional[int] = None       # id interno da fatura (metadata)
    external_id: Optional[str] = None      # id da cobrança no provedor
    amount_cents: Optional[int] = None
    raw: dict = field(default_factory=dict)


def _hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _parse_json(raw_body: bytes) -> dict:
    try:
        data = json.loads(raw_body.decode("utf-8") or "{}")
        return data if isinstance(data, dict) else {"data": data}
    except (ValueError, UnicodeDecodeError) as exc:
        raise PaymentWebhookError(f"Corpo do webhook não é JSON válido: {exc}")


def _extract_ids(payload: dict) -> tuple[Optional[int], Optional[str], Optional[int]]:
    """Tenta extrair (invoice_id interno, external_id, amount_cents) do payload.

    Procura em locais comuns: metadata.invoice_id, data.metadata.invoice_id, etc.
    O provedor deve ser configurado para enviar ``invoice_id`` no metadata da
    cobrança; assim o webhook consegue casar com a fatura interna.
    """
    invoice_id = None
    external_id = None
    amount_cents = None

    def _dig(d, *path):
        cur = d
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return None
        return cur

    for getter in (
        lambda: _dig(payload, "metadata", "invoice_id"),
        lambda: _dig(payload, "data", "metadata", "invoice_id"),
        lambda: _dig(payload, "data", "object", "metadata", "invoice_id"),
        lambda: payload.get("invoice_id"),
    ):
        val = getter()
        if val is not None:
            try:
                invoice_id = int(val)
            except (TypeError, ValueError):
                invoice_id = None
            break

    for getter in (
        lambda: _dig(payload, "data", "object", "id"),
        lambda: _dig(payload, "data", "id"),
        lambda: payload.get("id"),
        lambda: _dig(payload, "payment", "id"),
    ):
        val = getter()
        if val:
            external_id = str(val)
            break

    for getter in (
        lambda: _dig(payload, "data", "object", "amount_paid"),
        lambda: _dig(payload, "data", "object", "amount"),
        lambda: payload.get("amount_cents"),
    ):
        val = getter()
        if val is not None:
            try:
                amount_cents = int(val)
            except (TypeError, ValueError):
                amount_cents = None
            break

    return invoice_id, external_id, amount_cents


def _verify_stripe(secret: str, headers: dict, raw_body: bytes) -> None:
    sig_header = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
    if not sig_header:
        raise PaymentWebhookError("Header Stripe-Signature ausente.")
    parts = {}
    for item in sig_header.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip()
    timestamp = parts.get("t")
    v1 = parts.get("v1")
    if not timestamp or not v1:
        raise PaymentWebhookError("Assinatura Stripe malformada.")
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = _hmac_sha256_hex(secret, signed_payload)
    if not _const_eq(expected, v1):
        raise PaymentWebhookError("Assinatura Stripe inválida.")


def _verify_mercadopago(secret: str, headers: dict, raw_body: bytes) -> None:
    sig_header = headers.get("x-signature") or headers.get("X-Signature") or ""
    if not sig_header:
        raise PaymentWebhookError("Header x-signature ausente.")
    parts = {}
    for item in sig_header.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip()
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not v1:
        raise PaymentWebhookError("Assinatura Mercado Pago malformada.")
    signed_payload = f"{ts}.".encode("utf-8") + raw_body
    expected = _hmac_sha256_hex(secret, signed_payload)
    if not _const_eq(expected, v1):
        raise PaymentWebhookError("Assinatura Mercado Pago inválida.")


def _verify_asaas(secret: str, headers: dict, raw_body: bytes) -> None:
    token = headers.get("asaas-access-token") or headers.get("Asaas-Access-Token") or ""
    if not _const_eq(token, secret):
        raise PaymentWebhookError("Token Asaas inválido.")


def _verify_manual(secret: str, headers: dict, raw_body: bytes) -> None:
    token = (
        headers.get("x-webhook-token")
        or headers.get("X-Webhook-Token")
        or headers.get("authorization", "").replace("Bearer ", "")
    )
    if not _const_eq(token, secret):
        raise PaymentWebhookError("Token do webhook inválido.")


_VERIFIERS = {
    "stripe": _verify_stripe,
    "mercadopago": _verify_mercadopago,
    "asaas": _verify_asaas,
    "manual": _verify_manual,
}

_PAID_HINTS = {
    "payment_intent.succeeded", "charge.succeeded", "invoice.payment_succeeded",
    "payment.succeeded", "payment", "payment.updated", "PAYMENT_CONFIRMED",
    "PAYMENT_RECEIVED",
}


def _looks_paid(payload: dict, event_type: str) -> bool:
    if event_type in _PAID_HINTS:
        # Mercado Pago/Asaas mandam o status separado; confirme quando disponível.
        status = (
            (payload.get("data", {}) or {}).get("object", {}).get("status")
            if isinstance(payload.get("data"), dict) else None
        ) or payload.get("status")
        if status in (None, "paid", "approved", "succeeded", "CONFIRMED", "RECEIVED", "active"):
            return True
    status = payload.get("status") or ""
    return status in ("paid", "approved", "succeeded", "CONFIRMED", "RECEIVED")


def verify_and_parse(headers: dict, raw_body: bytes, provider: str = None) -> PaymentEvent:
    """Verifica a assinatura do webhook e retorna um :class:`PaymentEvent`.

    Levanta :class:`PaymentWebhookError` se a assinatura for inválida ou o
    provedor for desconhecido. Normaliza headers para lowercase.
    """
    provider = (provider or settings.PAYMENT_PROVIDER or "manual").strip().lower()
    verifier = _VERIFIERS.get(provider)
    if verifier is None:
        raise PaymentWebhookError(f"Provedor de pagamento não suportado: {provider}")

    secret = settings.PAYMENT_WEBHOOK_SECRET or ""
    if not secret:
        raise PaymentWebhookError(
            "PAYMENT_WEBHOOK_SECRET não configurado; recusando webhook por segurança."
        )

    norm_headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    verifier(secret, norm_headers, raw_body)

    payload = _parse_json(raw_body)
    event_type = (
        payload.get("type")
        or payload.get("event")
        or payload.get("action")
        or "other"
    )
    invoice_id, external_id, amount_cents = _extract_ids(payload)
    return PaymentEvent(
        provider=provider,
        event_type=str(event_type),
        paid=_looks_paid(payload, str(event_type)),
        invoice_id=invoice_id,
        external_id=external_id,
        amount_cents=amount_cents,
        raw=payload,
    )
