"""
tests/test_cache.py

Unit tests for coreai.cache — ResponseCache, MemoryCache, RedisCache.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coreai.cache import CacheBackend, MemoryCache, RedisCache, ResponseCache

# ---------------------------------------------------------------------------
# MemoryCache
# ---------------------------------------------------------------------------


class TestMemoryCache:

    @pytest.fixture
    def cache(self):
        return MemoryCache()

    @pytest.mark.asyncio
    async def test_set_and_get(self, cache):
        await cache.set("key:1", {"content": "hello"}, ttl_seconds=60)
        result = await cache.get("key:1")
        assert result == {"content": "hello"}

    @pytest.mark.asyncio
    async def test_get_missing_key_returns_none(self, cache):
        result = await cache.get("nonexistent:key")
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_entry_returns_none(self, cache):
        # Manually plant an already-expired entry
        cache.store["key:expired"] = (
            {"data": "old"},
            datetime.now() - timedelta(seconds=1),
        )
        result = await cache.get("key:expired")
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_entry_is_evicted(self, cache):
        cache.store["key:expired"] = (
            {"data": "old"},
            datetime.now() - timedelta(seconds=1),
        )
        await cache.get("key:expired")
        assert "key:expired" not in cache.store

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, cache):
        await cache.set("key:del", {"x": 1})
        await cache.delete("key:del")
        assert await cache.get("key:del") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_is_noop(self, cache):
        await cache.delete("key:doesnt_exist")  # should not raise

    @pytest.mark.asyncio
    async def test_clear_empties_store(self, cache):
        await cache.set("k1", {"a": 1})
        await cache.set("k2", {"b": 2})
        await cache.clear()
        assert len(cache.store) == 0

    @pytest.mark.asyncio
    async def test_overwrite_existing_key(self, cache):
        await cache.set("key:x", {"v": 1})
        await cache.set("key:x", {"v": 2})
        result = await cache.get("key:x")
        assert result == {"v": 2}


# ---------------------------------------------------------------------------
# RedisCache — fallback path (redis not available)
# ---------------------------------------------------------------------------


class TestRedisCacheFallback:

    @pytest.fixture
    def cache(self):
        from coreai.cache import RedisCache, MemoryCache

        c = RedisCache.__new__(RedisCache)
        c.redis = None
        c.fallback = MemoryCache()
        return c

    @pytest.mark.asyncio
    async def test_get_delegates_to_fallback(self, cache):
        await cache.fallback.set("key:fb", {"fallback": True})
        result = await cache.get("key:fb")
        assert result == {"fallback": True}

    @pytest.mark.asyncio
    async def test_set_delegates_to_fallback(self, cache):
        await cache.set("key:fb2", {"x": 99})
        result = await cache.fallback.get("key:fb2")
        assert result == {"x": 99}

    @pytest.mark.asyncio
    async def test_delete_delegates_to_fallback(self, cache):
        await cache.fallback.set("key:fb3", {"y": 1})
        await cache.delete("key:fb3")
        assert await cache.fallback.get("key:fb3") is None

    @pytest.mark.asyncio
    async def test_clear_delegates_to_fallback(self, cache):
        await cache.fallback.set("key:fb4", {"z": 0})
        await cache.clear()
        assert len(cache.fallback.store) == 0


# ---------------------------------------------------------------------------
# ResponseCache
# ---------------------------------------------------------------------------

MESSAGES_A = [{"role": "user", "content": "What is 2+2?"}]
MESSAGES_B = [{"role": "user", "content": "What is the capital of France?"}]
RESPONSE_A = {
    "content": "4",
    "model": "gpt-4o-mini",
    "provider": "openai",
    "input_tokens": 14,
    "output_tokens": 3,
    "cost_usd": 0.0000021,
    "latency_ms": 312.4,
    "cached": False,
}


class TestResponseCache:

    @pytest.fixture
    def rc(self):
        return ResponseCache(backend=MemoryCache())

    @pytest.mark.asyncio
    async def test_miss_returns_none(self, rc):
        result = await rc.get("openai", "gpt-4o-mini", MESSAGES_A)
        assert result is None

    @pytest.mark.asyncio
    async def test_set_then_get_returns_response(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        result = await rc.get("openai", "gpt-4o-mini", MESSAGES_A)
        assert result["content"] == "4"
        assert result["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_different_messages_do_not_collide(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        result = await rc.get("openai", "gpt-4o-mini", MESSAGES_B)
        assert result is None

    @pytest.mark.asyncio
    async def test_different_providers_do_not_collide(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        result = await rc.get("anthropic", "claude-haiku-4-5", MESSAGES_A)
        assert result is None

    @pytest.mark.asyncio
    async def test_hit_increments_counter(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        await rc.get("openai", "gpt-4o-mini", MESSAGES_A)
        assert rc.hits == 1

    @pytest.mark.asyncio
    async def test_miss_increments_miss_counter(self, rc):
        await rc.get("openai", "gpt-4o-mini", MESSAGES_A)
        assert rc.misses == 1

    @pytest.mark.asyncio
    async def test_stats_hit_rate(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        await rc.get("openai", "gpt-4o-mini", MESSAGES_A)  # hit
        await rc.get("openai", "gpt-4o-mini", MESSAGES_B)  # miss
        stats = rc.stats()
        assert stats["hit_rate_percent"] == 50.0
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 1

    @pytest.mark.asyncio
    async def test_stats_empty(self, rc):
        stats = rc.stats()
        assert stats["hit_rate_percent"] == 0
        assert stats["total_requests"] == 0

    @pytest.mark.asyncio
    async def test_clear_resets_counters(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        await rc.get("openai", "gpt-4o-mini", MESSAGES_A)
        await rc.clear()
        assert rc.hits == 0
        assert rc.misses == 0

    @pytest.mark.asyncio
    async def test_delete_invalidates_entry(self, rc):
        await rc.set("openai", "gpt-4o-mini", MESSAGES_A, RESPONSE_A)
        await rc.delete("openai", "gpt-4o-mini", MESSAGES_A)
        result = await rc.get("openai", "gpt-4o-mini", MESSAGES_A)
        assert result is None

    def test_make_key_is_deterministic(self, rc):
        k1 = rc._make_key("openai", "gpt-4o-mini", MESSAGES_A)
        k2 = rc._make_key("openai", "gpt-4o-mini", MESSAGES_A)
        assert k1 == k2

    def test_make_key_differs_by_content(self, rc):
        k1 = rc._make_key("openai", "gpt-4o-mini", MESSAGES_A)
        k2 = rc._make_key("openai", "gpt-4o-mini", MESSAGES_B)
        assert k1 != k2
