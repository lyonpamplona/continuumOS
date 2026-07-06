"""Testes do resolver de configuração por tenant (Fase 3/5)."""

import tenant_config as tc


def test_default_config_estrutura():
    cfg = tc._default_config()
    assert "business_hours" in cfg
    assert "role_session_map" in cfg
    assert "brand_color" in cfg
    bh = cfg["business_hours"]
    for chave in ("open", "close", "lunch_start", "lunch_end"):
        assert chave in bh


def test_merge_segundo_nivel():
    base = {"business_hours": {"open": 480, "close": 1080}, "brand_color": "#FF6600"}
    override = {"business_hours": {"close": 1200}, "brand_color": "#000000"}
    merged = tc._merge(base, override)
    # merge de segundo nível preserva open e sobrescreve close
    assert merged["business_hours"]["open"] == 480
    assert merged["business_hours"]["close"] == 1200
    assert merged["brand_color"] == "#000000"


def test_merge_override_none_mantem_base():
    base = {"a": 1}
    assert tc._merge(base, None) == {"a": 1}


def test_business_hours_tupla():
    # sem carregar tenant, cai no default cacheado -> tupla de 4 posições
    bh = tc.business_hours(None)
    assert isinstance(bh, tuple)
    assert len(bh) == 4
