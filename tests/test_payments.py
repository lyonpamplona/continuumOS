"""Fase 6 — Testes unitários da verificação de webhooks de pagamento.

Cobrem: rejeição por assinatura inválida, aceitação com HMAC correto (Stripe),
token manual, extração de invoice_id/external_id e detecção de pagamento.
Não dependem de rede nem de banco.
"""

import hashlib
import hmac
import json

import pytest

from config import settings
from services import payments


def _sign_stripe(secret: str, timestamp: str, body: bytes) -> str:
    signed = f"{timestamp}.".encode("utf-8") + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def test_manual_valid_token(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", "s3cr3t", raising=False)
    body = json.dumps({"type": "payment.succeeded", "metadata": {"invoice_id": 42},
                       "status": "paid"}).encode()
    event = payments.verify_and_parse(
        {"X-Webhook-Token": "s3cr3t"}, body, provider="manual"
    )
    assert event.paid is True
    assert event.invoice_id == 42


def test_manual_invalid_token(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", "s3cr3t", raising=False)
    body = json.dumps({"type": "payment.succeeded"}).encode()
    with pytest.raises(payments.PaymentWebhookError):
        payments.verify_and_parse({"X-Webhook-Token": "errado"}, body, provider="manual")


def test_missing_secret_rejected(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", "", raising=False)
    with pytest.raises(payments.PaymentWebhookError):
        payments.verify_and_parse({}, b"{}", provider="manual")


def test_stripe_valid_signature(monkeypatch):
    secret = "whsec_test"
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", secret, raising=False)
    body = json.dumps({
        "type": "invoice.payment_succeeded",
        "data": {"object": {"id": "pi_123", "status": "succeeded",
                             "metadata": {"invoice_id": 7}, "amount_paid": 9900}},
    }).encode()
    header = _sign_stripe(secret, "1700000000", body)
    event = payments.verify_and_parse({"Stripe-Signature": header}, body, provider="stripe")
    assert event.paid is True
    assert event.invoice_id == 7
    assert event.external_id == "pi_123"
    assert event.amount_cents == 9900


def test_stripe_invalid_signature(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", "whsec_test", raising=False)
    body = b'{"type":"invoice.payment_succeeded"}'
    bad = _sign_stripe("other_secret", "1700000000", body)
    with pytest.raises(payments.PaymentWebhookError):
        payments.verify_and_parse({"Stripe-Signature": bad}, body, provider="stripe")


def test_unsupported_provider(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", "x", raising=False)
    with pytest.raises(payments.PaymentWebhookError):
        payments.verify_and_parse({}, b"{}", provider="foobar")


def test_non_paid_event(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_WEBHOOK_SECRET", "s3cr3t", raising=False)
    body = json.dumps({"type": "payment.failed", "status": "failed"}).encode()
    event = payments.verify_and_parse({"X-Webhook-Token": "s3cr3t"}, body, provider="manual")
    assert event.paid is False
