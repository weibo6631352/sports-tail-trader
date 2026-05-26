"""Lightweight per-endpoint token bucket rate limiter.

只服务 operator 进程内的 FastAPI——没引入 Redis / 分布式，所有状态都在 asyncio loop
内单进程持有。在多 worker 部署时每 worker 各自计数，不影响安全语义（限的是
昂贵的单进程 CPU / 外部 API 配额）。

设计：
- 每个 endpoint 一个独立桶；FastAPI dependency 注入 helper ``rate_limit("name", qps, burst)``。
- 漏桶 / 令牌桶：每秒补 ``qps`` 个令牌，桶容量 ``burst``，请求来时拿走 1 个；空了返回 429 + Retry-After。
- 按客户端 IP 分（或匿名共享）：默认 per-IP，对前端单实例足够；如要更细可扩展 key_fn。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Callable

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


class _TokenBucket:
    __slots__ = ("capacity", "refill_per_sec", "tokens", "last_refill")

    def __init__(self, *, capacity: float, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        elapsed = max(now - self.last_refill, 0.0)
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after_seconds(self) -> float:
        if self.refill_per_sec <= 0:
            return 60.0
        deficit = 1.0 - self.tokens
        return max(math.ceil(deficit / self.refill_per_sec * 10) / 10, 0.1)


class _LimiterRegistry:
    """全 process 单例，按 (endpoint_name, client_key) → bucket 索引。

    清理方式：被动 LRU——dict 增长超过 ``_MAX_KEYS`` 时丢弃最老的一半。绝大多数
    情况下 client IP 是有限集，bucket 不会爆。
    """

    _MAX_KEYS = 4096

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._lock = asyncio.Lock()

    def get_or_create(
        self,
        *,
        endpoint: str,
        client_key: str,
        qps: float,
        burst: float,
    ) -> _TokenBucket:
        # 不用 asyncio.Lock 守 dict 写——Python GIL 保证 dict ops 原子；并发
        # 在同一 key 上的"双创建"概率极低（同一 IP 同一 endpoint 同时第一次访问），
        # 即便发生也只是稍稍丢失少量 token，无安全后果。
        key = (endpoint, client_key)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(capacity=burst, refill_per_sec=qps)
            self._buckets[key] = bucket
            if len(self._buckets) > self._MAX_KEYS:
                self._prune()
        return bucket

    def _prune(self) -> None:
        # 简化版 LRU：直接砍掉前一半。生产可以换成真正的 LRU 链表，但当前规模
        # 下没意义——bucket 是小对象，4k 个也才几十 KB。
        to_drop = list(self._buckets.keys())[: self._MAX_KEYS // 2]
        for key in to_drop:
            self._buckets.pop(key, None)
        logger.info("rate_limit registry pruned %d entries (cap=%d)", len(to_drop), self._MAX_KEYS)


_REGISTRY = _LimiterRegistry()


def _client_key(request: Request) -> str:
    """优先 X-Forwarded-For（如果前置反代信任），其次 client.host。"""

    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # 反代链中第一个 IP 是原始 client
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "anonymous"


def rate_limit(
    *,
    endpoint: str,
    qps: float,
    burst: float | None = None,
) -> Callable[[Request], None]:
    """FastAPI dependency 工厂。

    用法：

        @router.get("/expensive")
        async def handler(_: None = Depends(rate_limit(endpoint="expensive", qps=1.0, burst=3))):
            ...

    超限时抛 HTTP 429 + ``Retry-After`` header。
    """

    effective_burst = burst if burst is not None else max(qps, 1.0)

    def _dep(request: Request) -> None:
        client_key = _client_key(request)
        bucket = _REGISTRY.get_or_create(
            endpoint=endpoint,
            client_key=client_key,
            qps=qps,
            burst=effective_burst,
        )
        if bucket.take():
            return
        retry_after = bucket.retry_after_seconds()
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limit_exceeded", "endpoint": endpoint, "retry_after_s": retry_after},
            headers={"Retry-After": str(int(retry_after) or 1)},
        )

    return _dep
