"""Rate limiting por janela deslizante (Redis em produção, memória em dev)."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._redis = None
        if settings.redis_url:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def hit(self, key: str, limit: int, window: int = 60) -> tuple[bool, int]:
        """Retorna (permitido, restantes)."""
        if self._redis is not None:
            try:
                bucket = f"rl:{key}:{int(time.time() // window)}"
                n = await self._redis.incr(bucket)
                if n == 1:
                    await self._redis.expire(bucket, window + 1)
                return n <= limit, max(limit - n, 0)
            except Exception:  # Redis fora do ar não deve derrubar a API (fail-open)
                return True, limit
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False, 0
        q.append(now)
        if len(self._hits) > 50_000:  # proteção de memória
            self._hits.clear()
        return True, limit - len(q)

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()
