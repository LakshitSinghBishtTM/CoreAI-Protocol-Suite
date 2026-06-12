import json
import hashlib
from abc import ABC, abstractmethod
from typing import Optional
from datetime import datetime, timedelta

from loguru import logger


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
    """In-memory cache for development/testing"""

    def __init__(self):
        self.store: dict[str, tuple[dict, datetime]] = {}

    async def get(self, key: str) -> Optional[dict]:
        if key not in self.store:
            return None
        value, expiry = self.store[key]
        if datetime.now() > expiry:
            del self.store[key]
            return None
        return value

    async def set(self, key: str, value: dict, ttl_seconds: int = 3600):
        expiry = datetime.now() + timedelta(seconds=ttl_seconds)
        self.store[key] = (value, expiry)

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
        if not self.redis:
            await self.fallback.set(key, value, ttl_seconds)
            return
        try:
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

    def __init__(self, backend: Optional[CacheBackend] = None):
        self.backend = backend or MemoryCache()
        self.hits = 0
        self.misses = 0

    def _make_key(self, provider: str, model: str, messages: list[dict]) -> str:
        """Create cache key from provider, model, and message content"""
        msg_str = json.dumps(messages, sort_keys=True)
        msg_hash = hashlib.sha256(msg_str.encode()).hexdigest()
        return f"cache:{provider}:{model}:{msg_hash}"

    async def get(self, provider: str, model: str, messages: list[dict]) -> Optional[dict]:
        key = self._make_key(provider, model, messages)
        result = await self.backend.get(key)
        if result:
            self.hits += 1
            logger.debug(f"Cache HIT: {key}")
        else:
            self.misses += 1
        return result

    async def set(self, provider: str, model: str, messages: list[dict], response: dict, ttl_seconds: int = 3600):
        key = self._make_key(provider, model, messages)
        await self.backend.set(key, response, ttl_seconds)
        logger.debug(f"Cache SET: {key}")

    async def delete(self, provider: str, model: str, messages: list[dict]):
        key = self._make_key(provider, model, messages)
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