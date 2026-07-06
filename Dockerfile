FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fase 5: diretórios de dados/logs/backups (montados como volume em produção).
RUN mkdir -p /data/midias /data/backups /app/logs

# Fase 5: healthcheck para orquestradores (compose/k8s) reiniciarem o container
# quando o endpoint /health parar de responder.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else 1)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]