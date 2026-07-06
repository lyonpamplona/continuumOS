"""Configuração comum dos testes (Fase 5).

Define as variáveis de ambiente obrigatórias ANTES de qualquer import de
``config``/``database``/``storage``, já que o boot do ``config`` falha de
propósito quando segredos obrigatórios estão ausentes (Fase 0).
"""

import os
import sys

# Garante que a raiz do projeto esteja no sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REQUIRED_ENV = {
    "EVO_API_URL": "http://localhost:8080",
    "EVO_API_KEY": "test-key",
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/testdb",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "test-jwt-secret",
    "WEBHOOK_SECRET": "test-webhook-secret",
    "ADMIN_PASSWORD": "test-admin-pass",
}

for _key, _value in _REQUIRED_ENV.items():
    os.environ.setdefault(_key, _value)
