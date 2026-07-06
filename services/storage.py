"""Camada de storage de mídia (Fase 5 — Escala e operação).

Abstrai o armazenamento de arquivos para permitir dois backends sem alterar as
rotas que consomem o serviço:

- ``local`` (padrão): grava em disco (``MEDIA_ROOT``, por padrão ``/data/midias``)
  e devolve um caminho web servido pelo mount ``/midias`` — comportamento
  histórico, 100%% compatível.
- ``s3``: grava em um bucket S3/R2 (qualquer provedor compatível com S3) e devolve
  a URL pública do objeto, permitindo escalar horizontalmente vários workers/
  máquinas sem volume compartilhado.

Os arquivos são namespaceados por tenant (``tN/arquivo``), o que habilita backup,
expurgo e auditoria de mídia por cliente. Caminhos legados sem prefixo de tenant
(``/midias/arquivo``) continuam sendo lidos normalmente.
"""

import os
import asyncio

import aiofiles
import httpx
from loguru import logger

from config import settings

# Raiz física do backend local e prefixo web usado pelo mount estático.
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/data/midias")
WEB_PREFIX = "/midias"

_s3_client = None


def backend() -> str:
    """Retorna o backend de storage ativo (``local`` ou ``s3``)."""
    return (getattr(settings, "STORAGE_BACKEND", "local") or "local").strip().lower()


def tenant_prefix(tenant_id) -> str:
    """Prefixo de namespace por tenant (ex.: ``t1``). Sem tenant vira ``t0``."""
    try:
        return f"t{int(tenant_id)}"
    except (TypeError, ValueError):
        return "t0"


def _get_s3():
    """Cria (lazy) o client boto3 apontando para o endpoint S3/R2 configurado."""
    global _s3_client
    if _s3_client is None:
        import boto3  # import tardio: só necessário quando STORAGE_BACKEND=s3

        _s3_client = boto3.client(
            "s3",
            endpoint_url=getattr(settings, "S3_ENDPOINT_URL", None) or None,
            region_name=getattr(settings, "S3_REGION", None) or None,
            aws_access_key_id=getattr(settings, "S3_ACCESS_KEY_ID", None) or None,
            aws_secret_access_key=getattr(settings, "S3_SECRET_ACCESS_KEY", None) or None,
        )
    return _s3_client


def _public_url(key: str) -> str:
    """Monta a URL pública de um objeto no bucket (backend s3)."""
    base = (getattr(settings, "S3_PUBLIC_BASE_URL", "") or "").rstrip("/")
    if base:
        return f"{base}/{key}"
    endpoint = (getattr(settings, "S3_ENDPOINT_URL", "") or "").rstrip("/")
    bucket = getattr(settings, "S3_BUCKET", "") or ""
    return f"{endpoint}/{bucket}/{key}"


async def save_media(tenant_id, filename: str, data: bytes) -> str:
    """Persiste ``data`` e retorna a referência web/URL para gravar no banco.

    A referência retornada é o que fica salvo no corpo da mensagem e usado pelo
    frontend como ``src``; por isso deve ser resolúvel por :func:`read_media`.
    """
    key = f"{tenant_prefix(tenant_id)}/{filename}"
    if backend() == "s3":
        client = _get_s3()
        await asyncio.to_thread(
            client.put_object,
            Bucket=getattr(settings, "S3_BUCKET", ""),
            Key=key,
            Body=data,
        )
        return _public_url(key)

    path = os.path.join(MEDIA_ROOT, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiofiles.open(path, "wb") as buffer:
        await buffer.write(data)
    return f"{WEB_PREFIX}/{key}"


def _key_from_reference(reference: str) -> str:
    """Extrai a key relativa (``tN/arquivo``) de uma referência armazenada."""
    ref = reference.strip()
    if ref.startswith(WEB_PREFIX):
        return ref[len(WEB_PREFIX):].lstrip("/")
    # URL absoluta (s3): usa o trecho após o bucket, se identificável.
    bucket = getattr(settings, "S3_BUCKET", "") or ""
    if bucket and f"/{bucket}/" in ref:
        return ref.split(f"/{bucket}/", 1)[1]
    return ref.lstrip("/")


async def read_media(reference: str) -> bytes:
    """Lê os bytes de uma referência (caminho ``/midias/...`` ou URL absoluta).

    Suporta caminhos locais (inclusive legados sem prefixo de tenant) e, quando o
    backend for s3, baixa o objeto pela URL/httpx ou pelo client boto3.
    """
    ref = (reference or "").strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        if backend() == "s3":
            key = _key_from_reference(ref)
            client = _get_s3()
            obj = await asyncio.to_thread(
                client.get_object, Bucket=getattr(settings, "S3_BUCKET", ""), Key=key
            )
            return await asyncio.to_thread(obj["Body"].read)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(ref)
            resp.raise_for_status()
            return resp.content

    if backend() == "s3":
        key = _key_from_reference(ref)
        client = _get_s3()
        obj = await asyncio.to_thread(
            client.get_object, Bucket=getattr(settings, "S3_BUCKET", ""), Key=key
        )
        return await asyncio.to_thread(obj["Body"].read)

    # Backend local: aceita tanto "/midias/..." quanto caminho relativo/legado.
    key = _key_from_reference(ref)
    path = os.path.join(MEDIA_ROOT, key)
    async with aiofiles.open(path, "rb") as f:
        return await f.read()


async def delete_tenant_media(tenant_id) -> int:
    """Remove toda a mídia de um tenant (usado em offboarding/expurgo).

    Retorna a quantidade de objetos removidos. Seguro de chamar mesmo quando o
    tenant não possui mídia.
    """
    prefix = tenant_prefix(tenant_id)
    removed = 0
    if backend() == "s3":
        client = _get_s3()
        bucket = getattr(settings, "S3_BUCKET", "")
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": f"{prefix}/"}
            if token:
                kwargs["ContinuationToken"] = token
            resp = await asyncio.to_thread(client.list_objects_v2, **kwargs)
            for item in resp.get("Contents", []) or []:
                await asyncio.to_thread(
                    client.delete_object, Bucket=bucket, Key=item["Key"]
                )
                removed += 1
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return removed

    folder = os.path.join(MEDIA_ROOT, prefix)
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError as exc:
                logger.warning(f"Falha ao remover mídia {name} do tenant {tenant_id}: {exc}")
    return removed
