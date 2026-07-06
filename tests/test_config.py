"""Testes da camada de configuração (Fase 0/5)."""

import os

import pytest

import config as config_mod
from config import ConfigError, env_bool, env_list, env_str


def test_env_bool_variantes(monkeypatch):
    for truthy in ["1", "true", "YES", "on", "sim"]:
        monkeypatch.setenv("FLAG_X", truthy)
        assert env_bool("FLAG_X") is True
    for falsy in ["0", "false", "nao", "off"]:
        monkeypatch.setenv("FLAG_X", falsy)
        assert env_bool("FLAG_X") is False
    monkeypatch.delenv("FLAG_X", raising=False)
    assert env_bool("FLAG_X", default=True) is True


def test_env_list_descarta_vazios(monkeypatch):
    monkeypatch.setenv("ORIGINS", "a.com, ,b.com,")
    assert env_list("ORIGINS") == ["a.com", "b.com"]


def test_env_str_required_falha(monkeypatch):
    monkeypatch.delenv("OBRIGATORIA", raising=False)
    with pytest.raises(ConfigError):
        env_str("OBRIGATORIA", required=True)


def test_validate_storage_local_ok():
    cfg = config_mod.Config()
    cfg.STORAGE_BACKEND = "local"
    # backend local nunca deve exigir credenciais S3
    cfg.validate_storage()


def test_validate_storage_s3_exige_credenciais():
    cfg = config_mod.Config()
    cfg.STORAGE_BACKEND = "s3"
    cfg.S3_BUCKET = ""
    cfg.S3_ACCESS_KEY_ID = ""
    cfg.S3_SECRET_ACCESS_KEY = ""
    with pytest.raises(ConfigError):
        cfg.validate_storage()


def test_validate_storage_s3_completo_ok():
    cfg = config_mod.Config()
    cfg.STORAGE_BACKEND = "s3"
    cfg.S3_BUCKET = "meu-bucket"
    cfg.S3_ACCESS_KEY_ID = "AKIA..."
    cfg.S3_SECRET_ACCESS_KEY = "secret"
    cfg.validate_storage()
