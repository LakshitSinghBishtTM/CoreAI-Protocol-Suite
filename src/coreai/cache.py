import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from copy import deepcopy
from typing import Callable, Optional

from loguru import logger


def _validate_ttl(ttl_seconds: int) -> None:
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds < 0
    ):
        raise ValueError("ttl_seconds must be a non-negative integer")


class CacheBackend(ABC):
    @abstractmethod
    async def get(self, key: str) -> Optional[dict]:
        pass

    @abstractmethod
    async def set(self, key: str, value: dict, ttl_seconds: int = 3600):
        pass

    @abstractmethod
    async def delete(self, key: str):
        pass

    @abstractmethod
    async def clear(self):
        pass


class MemoryCache(CacheBackend):
    """
    In-memory LRU cache with a hard size limit.

    Entries expire after their TTL and, when the cache is full, the least
    recently used entry is evicted. (Before this had a limit, a long-running
    server would grow without bound, because expired entries were only removed
    when the same key was read again.)
    """

    def __init__(
        self, max_entries: int = 1000, clock: Callable[[], float] = time.monotonic
    ):
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self.clock = clock
        self.store: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self.evictions = 0

    async def get(self, key: str) -> Optional[dict]:
        if key not in self.store:
            return None
        value, expiry = self.store[key]
        if self.clock() >= expiry:
            del self.store[key]
            return None
        self.store.move_to_end(key)
        return deepcopy(value)

    async def set(self, key: str, value: dict, ttl_seconds: int = 3600):
        _validate_ttl(ttl_seconds)
        if ttl_seconds == 0:
            self.store.pop(key, None)
            return
        expiry = self.clock() + ttl_seconds
        self.store[key] = (deepcopy(value), expiry)
        self.store.move_to_end(key)
        if len(self.store) > self.max_entries:
            self.purge_expired()
        while len(self.store) > self.max_entries:
            self.store.popitem(last=False)
            self.evictions += 1

    def purge_expired(self) -> int:
        """Drop every expired entry; returns how many were removed."""
        now = self.clock()
        expired = [k for k, (_, exp) in self.store.items() if now >= exp]
        for k in expired:
            del self.store[k]
        return len(expired)

    async def delete(self, key: str):
        self.store.pop(key, None)

    async def clear(self):
        self.store.clear()


class RedisCache(CacheBackend):
    """Redis-backed cache for production"""

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        try:
            import redis.asyncio as redis

            self.redis = redis.from_url(redis_url)
        except ImportError:
            logger.warning("redis not installed, falling back to MemoryCache")
            self.fallback = MemoryCache()
            self.redis = None

    async def get(self, key: str) -> Optional[dict]:
        if not self.redis:
            return await self.fallback.get(key)
        try:
            value = await self.redis.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error(f"Redis get failed: {e}")
            return None

    async def set(self, key: str, value: dict, ttl_seconds: int = 3600):
        _validate_ttl(ttl_seconds)
        if not self.redis:
            await self.fallback.set(key, value, ttl_seconds)
            return
        try:
            if ttl_seconds == 0:
                await self.redis.delete(key)
                return
            await self.redis.setex(key, ttl_seconds, json.dumps(value))
        except Exception as e:
            logger.error(f"Redis set failed: {e}")

    async def delete(self, key: str):
        if not self.redis:
            await self.fallback.delete(key)
            return
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.error(f"Redis delete failed: {e}")

    async def clear(self):
        if not self.redis:
            await self.fallback.clear()
            return
        try:
            await self.redis.flushdb()
        except Exception as e:
            logger.error(f"Redis clear failed: {e}")


class ResponseCache:
    """Cache responses by (provider, model, messages hash)"""

    def __init__(self, backend: Optional[CacheBackend] = None, max_entries: int = 1000):
        self.backend = backend or MemoryCache(max_entries=max_entries)
        self.hits = 0
        self.misses = 0

    def _make_key(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        options: Optional[dict] = None,
    ) -> str:
        """Cache key from provider, model, messages and every other
        answer-affecting option (system_prompt, temperature, max_tokens...)."""
        msg_str = json.dumps(
            {"messages": messages, "options": options or {}},
            sort_keys=True,
            default=str,
        )
        msg_hash = hashlib.sha256(msg_str.encode()).hexdigest()
        return f"cache:{provider}:{model}:{msg_hash}"

    async def get(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        options: Optional[dict] = None,
    ) -> Optional[dict]:
        key = self._make_key(provider, model, messages, options)
        result = await self.backend.get(key)
        if result:
            self.hits += 1
            logger.debug(f"Cache HIT: {key}")
        else:
            self.misses += 1
        return result

    async def set(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        response: dict,
        ttl_seconds: int = 3600,
        options: Optional[dict] = None,
    ):
        key = self._make_key(provider, model, messages, options)
        await self.backend.set(key, response, ttl_seconds)
        logger.debug(f"Cache SET: {key}")

    async def delete(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        options: Optional[dict] = None,
    ):
        key = self._make_key(provider, model, messages, options)
        await self.backend.delete(key)

    async def clear(self):
        await self.backend.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict:
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "total_requests": total,
            "hit_rate_percent": round(hit_rate, 2),
        }
