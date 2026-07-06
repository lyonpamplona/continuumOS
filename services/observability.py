"""Fase 7 — Observabilidade: métricas Prometheus, tracing e alertas.

Projetado para funcionar **sem dependências obrigatórias**:
- Métricas: exposição no formato texto do Prometheus é gerada em Python puro
  (contadores + histograma de latência), então o endpoint ``/metrics`` funciona
  mesmo sem o pacote ``prometheus_client``. O Grafana consome direto do Prometheus.
- Sentry (alertas de erro) e OpenTelemetry (tracing) são **opcionais**: só são
  ativados se os pacotes estiverem instalados e configurados; caso contrário viram
  no-op com um log informativo. Assim o boot nunca quebra por falta de lib.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict

from loguru import logger

from config import settings

# --------------------------------------------------------------------------
# Métricas (Prometheus text exposition, Python puro)
# --------------------------------------------------------------------------

_LOCK = threading.Lock()
_LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]

# http_requests_total{method,path,status}
_req_total: dict = defaultdict(int)
# http_request_duration_seconds (histograma) por (method,path)
_lat_buckets: dict = defaultdict(lambda: defaultdict(int))
_lat_sum: dict = defaultdict(float)
_lat_count: dict = defaultdict(int)
# gauges/counters genéricos de negócio
_counters: dict = defaultdict(int)
_gauges: dict = {}

_started_at = time.time()

_ID_RE = re.compile(r"/\d+")
_HEX_RE = re.compile(r"/[0-9a-f]{8,}", re.IGNORECASE)


def normalize_path(path: str) -> str:
    """Reduz cardinalidade: troca ids numéricos/hex por ``/:id``."""
    if not path:
        return "/"
    p = _ID_RE.sub("/:id", path)
    p = _HEX_RE.sub("/:id", p)
    return p[:80]


def record_request(method: str, path: str, status: int, elapsed_s: float) -> None:
    """Registra uma requisição HTTP nas métricas."""
    if not settings.METRICS_ENABLED:
        return
    method = (method or "GET").upper()
    path = normalize_path(path)
    key = (method, path, str(status))
    lkey = (method, path)
    with _LOCK:
        _req_total[key] += 1
        _lat_sum[lkey] += elapsed_s
        _lat_count[lkey] += 1
        for b in _LATENCY_BUCKETS:
            if elapsed_s <= b:
                _lat_buckets[lkey][b] += 1


def inc(counter_name: str, value: int = 1) -> None:
    """Incrementa um contador de negócio (ex.: jobs processados, webhooks)."""
    with _LOCK:
        _counters[counter_name] += value


def set_gauge(name: str, value: float) -> None:
    with _LOCK:
        _gauges[name] = value


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def render_metrics() -> str:
    """Gera o corpo do endpoint /metrics no formato de exposição do Prometheus."""
    lines: list = []
    with _LOCK:
        lines.append("# HELP app_uptime_seconds Uptime do processo em segundos.")
        lines.append("# TYPE app_uptime_seconds gauge")
        lines.append(f"app_uptime_seconds {time.time() - _started_at:.1f}")

        lines.append("# HELP http_requests_total Total de requisições HTTP.")
        lines.append("# TYPE http_requests_total counter")
        for (method, path, status), val in _req_total.items():
            lines.append(
                f'http_requests_total{{method="{_esc(method)}",path="{_esc(path)}",'
                f'status="{_esc(status)}"}} {val}'
            )

        lines.append("# HELP http_request_duration_seconds Latência das requisições.")
        lines.append("# TYPE http_request_duration_seconds histogram")
        for (method, path), buckets in _lat_buckets.items():
            cumulative = 0
            m, p = _esc(method), _esc(path)
            for b in _LATENCY_BUCKETS:
                cumulative = buckets.get(b, 0)
                lines.append(
                    f'http_request_duration_seconds_bucket{{method="{m}",path="{p}",'
                    f'le="{b}"}} {cumulative}'
                )
            total = _lat_count[(method, path)]
            lines.append(
                f'http_request_duration_seconds_bucket{{method="{m}",path="{p}",le="+Inf"}} {total}'
            )
            lines.append(
                f'http_request_duration_seconds_sum{{method="{m}",path="{p}"}} {_lat_sum[(method, path)]:.6f}'
            )
            lines.append(
                f'http_request_duration_seconds_count{{method="{m}",path="{p}"}} {total}'
            )

        if _counters:
            lines.append("# HELP app_events_total Contadores de eventos de negócio.")
            lines.append("# TYPE app_events_total counter")
            for name, val in _counters.items():
                lines.append(f'app_events_total{{event="{_esc(name)}"}} {val}')

        for name, val in _gauges.items():
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {val}")

    return "\n".join(lines) + "\n"


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


# --------------------------------------------------------------------------
# Sentry (alertas de erro) — opcional
# --------------------------------------------------------------------------

def init_sentry() -> bool:
    """Inicializa o Sentry se ``SENTRY_DSN`` e o pacote estiverem disponíveis."""
    dsn = settings.SENTRY_DSN
    if not dsn:
        return False
    try:
        import sentry_sdk  # type: ignore
    except Exception:
        logger.warning("[SENTRY] SENTRY_DSN definido mas 'sentry-sdk' não instalado; ignorando.")
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.SENTRY_ENVIRONMENT,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        )
        logger.info("[SENTRY] Inicializado (env={}).", settings.SENTRY_ENVIRONMENT)
        return True
    except Exception as exc:
        logger.warning("[SENTRY] Falha ao inicializar: {}", exc)
        return False


def capture_exception(exc: Exception) -> None:
    """Envia uma exceção ao Sentry, se disponível (no-op caso contrário)."""
    try:
        import sentry_sdk  # type: ignore
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


# --------------------------------------------------------------------------
# OpenTelemetry (tracing) — opcional
# --------------------------------------------------------------------------

def init_tracing(app) -> bool:
    """Instrumenta o FastAPI com OpenTelemetry se habilitado e disponível."""
    if not settings.OTEL_ENABLED:
        return False
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore
    except Exception:
        logger.warning("[OTEL] OTEL_ENABLED=true mas pacotes OpenTelemetry ausentes; ignorando.")
        return False
    try:
        resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT or None
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        logger.info("[OTEL] Tracing habilitado (service={}).", settings.OTEL_SERVICE_NAME)
        return True
    except Exception as exc:
        logger.warning("[OTEL] Falha ao inicializar tracing: {}", exc)
        return False
