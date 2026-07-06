"""Fase 6 — Testes do rate limiter (Redis) com um fake in-memory.

Validam: contagem por janela, bloqueio ao exceder o teto, fail-open quando o
Redis falha e extração de IP via X-Forwarded-For. Sem rede/servidor real.
"""

import pytest

from services import ratelimit


class FakeRedis:
    def __init__(self, fail=False):
        self.store = {}
        self.ttls = {}
        self.fail = fail

    async def incr(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, -1)


@pytest.mark.asyncio
async def test_allows_under_limit():
    r = FakeRedis()
    for _ in range(3):
        res = await ratelimit.check_rate_limit(r, "api", 1, "1.2.3.4", 5, 60)
        assert res.allowed is True


@pytest.mark.asyncio
async def test_blocks_over_limit():
    r = FakeRedis()
    results = [await ratelimit.check_rate_limit(r, "api", 1, "1.2.3.4", 3, 60)
               for _ in range(5)]
    assert results[0].allowed and results[1].allowed and results[2].allowed
    assert results[3].allowed is False
    assert results[4].allowed is False
    assert results[3].retry_after > 0


@pytest.mark.asyncio
async def test_fail_open_on_redis_error():
    r = FakeRedis(fail=True)
    res = await ratelimit.check_rate_limit(r, "api", 1, "1.2.3.4", 1, 60)
    assert res.allowed is True


@pytest.mark.asyncio
async def test_disabled_when_limit_zero():
    r = FakeRedis()
    res = await ratelimit.check_rate_limit(r, "api", 1, "1.2.3.4", 0, 60)
    assert res.allowed is True


@pytest.mark.asyncio
async def test_separate_keys_per_tenant_ip():
    r = FakeRedis()
    await ratelimit.check_rate_limit(r, "api", 1, "1.1.1.1", 2, 60)
    await ratelimit.check_rate_limit(r, "api", 1, "1.1.1.1", 2, 60)
    # tenant 2 começa do zero mesmo com mesmo IP
    res = await ratelimit.check_rate_limit(r, "api", 2, "1.1.1.1", 2, 60)
    assert res.allowed is True


class _FakeReq:
    def __init__(self, headers, host="9.9.9.9"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_client_ip_prefers_forwarded_for():
    req = _FakeReq({"x-forwarded-for": "203.0.113.5, 10.0.0.1"})
    assert ratelimit.client_ip_from_request(req) == "203.0.113.5"


def test_client_ip_falls_back_to_peer():
    req = _FakeReq({})
    assert ratelimit.client_ip_from_request(req) == "9.9.9.9"
