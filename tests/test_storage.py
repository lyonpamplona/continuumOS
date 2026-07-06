"""Testes da camada de storage local (Fase 5).

Exercitam o backend local (padrão), garantindo roundtrip de gravação/leitura,
namespaces por tenant, compatibilidade com caminhos legados e expurgo por tenant.
O backend S3 não é testado aqui (exige credenciais/serviço externo).
"""

import os

import pytest

from services import storage


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "midias"
    monkeypatch.setattr(storage, "MEDIA_ROOT", str(root))
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "local")
    return str(root)


def test_tenant_prefix():
    assert storage.tenant_prefix(1) == "t1"
    assert storage.tenant_prefix(None) == "t0"
    assert storage.tenant_prefix("x") == "t0"


@pytest.mark.asyncio
async def test_save_and_read_roundtrip(media_root):
    ref = await storage.save_media(7, "foto.jpg", b"conteudo-binario")
    assert ref == "/midias/t7/foto.jpg"
    # arquivo físico foi criado no namespace do tenant
    assert os.path.exists(os.path.join(media_root, "t7", "foto.jpg"))
    data = await storage.read_media(ref)
    assert data == b"conteudo-binario"


@pytest.mark.asyncio
async def test_read_legacy_path(media_root):
    # simula arquivo legado gravado direto na raiz (sem prefixo de tenant)
    os.makedirs(media_root, exist_ok=True)
    with open(os.path.join(media_root, "antigo.pdf"), "wb") as f:
        f.write(b"legado")
    data = await storage.read_media("/midias/antigo.pdf")
    assert data == b"legado"


@pytest.mark.asyncio
async def test_delete_tenant_media(media_root):
    await storage.save_media(9, "a.jpg", b"a")
    await storage.save_media(9, "b.jpg", b"b")
    removed = await storage.delete_tenant_media(9)
    assert removed == 2
    assert not os.listdir(os.path.join(media_root, "t9"))
