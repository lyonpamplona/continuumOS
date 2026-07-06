"""Testes dos utilitários operacionais (Fase 5)."""

from services.runtime import format_duration, uptime_seconds


def test_format_duration():
    assert format_duration(0) == "--"
    assert format_duration(None) == "--"
    assert format_duration(30) == "<1 min"
    assert format_duration(90) == "1 min"
    assert format_duration(3660) == "1h 01m"


def test_uptime_positivo():
    assert uptime_seconds() >= 0
