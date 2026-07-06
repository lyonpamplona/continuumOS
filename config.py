"""Centraliza as configurações da aplicação via variáveis de ambiente.

Fase 0 (Hardening): segredos não têm mais valores padrão inseguros. As
variáveis sensíveis são obrigatórias e a aplicação falha imediatamente no
boot caso alguma esteja ausente, evitando subir em produção com credenciais
conhecidas.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Erro de configuração: variável de ambiente obrigatória ausente."""


def env_bool(name: str, default: bool = False) -> bool:
    """Interpreta flags booleanas do `.env` aceitando valores comuns."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def env_str(name: str, default=None, required: bool = False):
    """Lê uma variável de ambiente, exigindo valor quando `required=True`."""
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise ConfigError(
            f"Variável de ambiente obrigatória ausente ou vazia: {name}. "
            f"Defina-a no arquivo .env antes de iniciar a aplicação."
        )
    return value


def env_list(name: str, default=""):
    """Lê uma lista separada por vírgulas, descartando itens vazios."""
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


# Diretório de segredos montado (Docker/K8s secrets). Ex.: /run/secrets.
SECRETS_DIR = os.getenv("SECRETS_DIR", "").strip()


def _read_secret_file(path: str):
    """Lê o conteúdo de um arquivo de segredo, se existir e for legível."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, IOError):
        return None


def env_secret(name: str, default=None, required: bool = False):
    """Fase 6 (gestão de segredos): resolve um segredo com precedência segura.

    Ordem de resolução (a primeira não-vazia vence):
      1. ``{NAME}_FILE`` apontando para um arquivo (convenção Docker/K8s secrets).
      2. ``SECRETS_DIR/{NAME}`` quando ``SECRETS_DIR`` está definido.
      3. Variável de ambiente ``{NAME}`` (compat. com .env atual).

    Assim, em produção os segredos podem vir de um secret manager (montados como
    arquivos) sem ficarem em texto plano no ambiente/.env, e a rotação é apenas a
    troca do arquivo/secret — sem alterar código.
    """
    file_hint = os.getenv(f"{name}_FILE", "").strip()
    if file_hint:
        value = _read_secret_file(file_hint)
        if value:
            return value
    if SECRETS_DIR:
        value = _read_secret_file(os.path.join(SECRETS_DIR, name))
        if value:
            return value
    return env_str(name, default=default, required=required)


class Config:
    """Objeto de configuração com parâmetros de integração e operação."""

    # --- Integrações obrigatórias (falham se ausentes) ---
    # Fase 6: segredos resolvidos via env_secret (arquivo/secret manager > env).
    EVO_API_URL = env_str("EVO_API_URL", required=True)
    EVO_API_KEY = env_secret("EVO_API_KEY", required=True)
    DATABASE_URL = env_secret("DATABASE_URL", required=True)
    REDIS_URL = env_secret("REDIS_URL", required=True)

    # --- Segredos obrigatórios (sem fallback hardcoded) ---
    JWT_SECRET = env_secret("JWT_SECRET", required=True)
    WEBHOOK_SECRET = env_secret("WEBHOOK_SECRET", required=True)
    ADMIN_PASSWORD = env_secret("ADMIN_PASSWORD", required=True)

    # --- Parâmetros operacionais ---
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(24 * 60 * 60)))
    ENFORCE_SINGLE_SESSION = env_bool("ENFORCE_SINGLE_SESSION", False)
    COOKIE_SECURE = env_bool("COOKIE_SECURE", True)
    MAX_MEDIA_UPLOAD_MB = int(os.getenv("MAX_MEDIA_UPLOAD_MB", "32"))
    ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

    # Fase 4: token do painel super-admin (gestão de clientes). Vazio = desabilitado.
    SUPER_ADMIN_TOKEN = os.getenv("SUPER_ADMIN_TOKEN", "")

    # --- Fase 5: storage de mídia (object storage) ---
    # "local" (padrão) grava em disco; "s3" usa bucket S3/R2 compatível.
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
    S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")
    S3_BUCKET = os.getenv("S3_BUCKET", "")
    S3_REGION = os.getenv("S3_REGION", "")
    S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
    S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
    # Base pública para servir os objetos (CDN/endpoint). Ex.: https://cdn.exemplo.com
    S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "")

    # --- Fase 5: observabilidade ---
    # Quando LOG_JSON=true, os logs saem em JSON (ideal p/ Loki/Datadog/ELK).
    LOG_JSON = env_bool("LOG_JSON", False)
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))
    # Diretório de backups por tenant (Fase 5).
    BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")

    # --- Fase 6: migrations versionadas (Alembic) ---
    # Quando true, o boot ainda aplica o schema (compat.). Em produção com Alembic,
    # defina RUN_DB_MIGRATIONS_ON_BOOT=false e rode `alembic upgrade head` no deploy.
    RUN_DB_MIGRATIONS_ON_BOOT = env_bool("RUN_DB_MIGRATIONS_ON_BOOT", True)

    # --- Fase 6: pagamento real (gateway + webhook) ---
    # Provedor: manual | stripe | mercadopago | asaas.
    PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "manual").strip().lower()
    # Segredo de assinatura do webhook do provedor (verifica autenticidade).
    PAYMENT_WEBHOOK_SECRET = env_secret("PAYMENT_WEBHOOK_SECRET", default="")
    # Reativa automaticamente o tenant quando uma fatura é paga via webhook.
    BILLING_REACTIVATE_ON_PAYMENT = env_bool("BILLING_REACTIVATE_ON_PAYMENT", True)

    # --- Fase 6: suspensão automática por inadimplência ---
    # Liga o loop que marca faturas vencidas (past_due) e suspende tenants.
    BILLING_ENFORCEMENT_ENABLED = env_bool("BILLING_ENFORCEMENT_ENABLED", True)
    # Dias de complência após o vencimento antes de suspender (grace period).
    BILLING_GRACE_DAYS = int(os.getenv("BILLING_GRACE_DAYS", "5"))

    # --- Fase 6: rate limiting por tenant/IP ---
    RATE_LIMIT_ENABLED = env_bool("RATE_LIMIT_ENABLED", True)
    # Janela (segundos) e teto de requisições por (tenant, ip) na janela.
    RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "600"))
    # Teto específico para o webhook (protege custo/abuso da Evolution).
    RATE_LIMIT_WEBHOOK_MAX = int(os.getenv("RATE_LIMIT_WEBHOOK_MAX", "1200"))

    # --- Fase 7: múltiplos workers/réplicas ---
    # Número de workers do servidor (informativo; gunicorn/uvicorn usam via CLI).
    WEB_CONCURRENCY = int(os.getenv("WEB_CONCURRENCY", "1"))
    # Loops periódicos (SLA, follow-up, lembretes, limpeza) devem rodar em UMA
    # única réplica/scheduler. Em deploy com N réplicas web, deixe true apenas em
    # uma (ou num processo scheduler dedicado) e false nas demais.
    RUN_BACKGROUND_JOBS = env_bool("RUN_BACKGROUND_JOBS", True)

    # --- Fase 7: fila de jobs (webhooks/mídia pesada) ---
    # Quando true, o processamento pesado é enfileirado no Redis e consumido pelo
    # worker (worker.py). Quando false, roda inline via BackgroundTasks (padrão).
    JOB_QUEUE_ENABLED = env_bool("JOB_QUEUE_ENABLED", False)
    JOB_QUEUE_KEY = os.getenv("JOB_QUEUE_KEY", "jobq:default")
    # Tempo (s) de espera do worker em cada BLPOP antes de reavaliar saúde.
    JOB_QUEUE_BLOCK_SECONDS = int(os.getenv("JOB_QUEUE_BLOCK_SECONDS", "5"))

    # --- Fase 7: métricas Prometheus + tracing ---
    METRICS_ENABLED = env_bool("METRICS_ENABLED", True)
    # Se definido, o /metrics exige Authorization: Bearer <token> (scrape privado).
    METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")
    OTEL_ENABLED = env_bool("OTEL_ENABLED", False)
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "continnumos")
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    # --- Fase 7: alertas (Sentry) ---
    SENTRY_DSN = env_secret("SENTRY_DSN", default="")
    SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production")
    SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))

    # --- Fase 8: signup self-service + trial ---
    # Habilita a página pública /signup e o endpoint de criação de conta.
    SIGNUP_ENABLED = env_bool("SIGNUP_ENABLED", True)
    # Plano inicial e duração do trial (dias) para novas contas self-service.
    SIGNUP_DEFAULT_PLAN = os.getenv("SIGNUP_DEFAULT_PLAN", "free")
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
    # Domínio base para montar a URL do tenant (slug.BASE_DOMAIN) após o signup.
    BASE_DOMAIN = os.getenv("BASE_DOMAIN", "")

    # --- Fase 8: auditoria / LGPD ---
    # Retenção de mensagens (dias). 0 ou negativo desativa a limpeza automática.
    DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", "0"))
    # Contato do DPO / encarregado para a página de privacidade.
    DPO_CONTACT_EMAIL = os.getenv("DPO_CONTACT_EMAIL", "")

    def validate_storage(self):
        """Fase 5: valida a configuração de storage quando o backend é s3.

        Falha cedo (no boot) se o backend for s3 mas faltar bucket/credenciais,
        evitando subir em produção com uploads quebrados.
        """
        if (self.STORAGE_BACKEND or "local").strip().lower() == "s3":
            faltando = [
                nome
                for nome, valor in (
                    ("S3_BUCKET", self.S3_BUCKET),
                    ("S3_ACCESS_KEY_ID", self.S3_ACCESS_KEY_ID),
                    ("S3_SECRET_ACCESS_KEY", self.S3_SECRET_ACCESS_KEY),
                )
                if not (valor and str(valor).strip())
            ]
            if faltando:
                raise ConfigError(
                    "STORAGE_BACKEND=s3 exige as variáveis: " + ", ".join(faltando)
                )

    # CORS: sem wildcard. Quando vazio, nenhuma origem cross-site é permitida.
    ALLOWED_ORIGINS = env_list("ALLOWED_ORIGINS", "")

    DEPARTAMENTOS = {
        "vendas_1": os.getenv("VENDAS_1", "Vendas 1"),
        "vendas_2": os.getenv("VENDAS_2", "Vendas 2"),
        "financeiro": os.getenv("FINANCEIRO", "Financeiro"),
        "engenharia": os.getenv("ENGENHARIA", "Engenharia"),
    }


settings = Config()
