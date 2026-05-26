"""短 TTL 内存缓存（docs/新架构方案.md §12.3 ③）。

agent / 前端高频查询（同 endpoint 1s 内多次）的 burst dedupe——100ms 窗口内
重复请求复用上次 response。运营查询类 endpoint 用，**审计查询 + 操盘动作类
endpoint 禁止用**（前者要看实时历史，后者 must 实际执行）。

# 设计原则

- **进程内单实例**：用 dict + monotonic 时间戳，无锁（GIL + 小窗口竞争可忽略）
- **手动失效**：`clear_cache(prefix)` 可按 key 前缀失效（admin 写动作后调）
- **TTL 上限 1s**：避免缓存遮蔽真实状态变化；默认 100ms

# 用法

```python
from polymarket_trader.api.middleware import ttl_cache

@ttl_cache(ttl_ms=100)
async def portfolio_aggregator(runtime, level="summary"):
    ...
```

`ttl_cache` 把 (函数名, args, kwargs) 拼成 key——args 必须 hashable，否则
跳过缓存（不抛错）。
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 进程内单 cache：key = (func_qualname, args_tuple, kwargs_tuple)，
# value = (result, expires_at_mono)
_CACHE: dict[tuple, tuple[Any, float]] = {}

_MAX_TTL_MS: float = 1000.0
_DEFAULT_TTL_MS: float = 100.0


def _make_key(func: Callable, args: tuple, kwargs: dict) -> tuple | None:
    """拼 cache key——args/kwargs 非 hashable 时返回 None（跳过缓存）。"""

    try:
        kw_items = tuple(sorted(kwargs.items()))
        return (func.__qualname__, args, kw_items)
    except TypeError:
        # 含非 hashable 参数（dict / set / 自定义类）→ 跳过缓存
        return None


def ttl_cache(ttl_ms: float = _DEFAULT_TTL_MS) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """async function 短 TTL 缓存装饰器。

    `ttl_ms` 取 (0, 1000]——超过 1s 不被允许（避免缓存遮蔽实时变化）。
    """

    if ttl_ms <= 0 or ttl_ms > _MAX_TTL_MS:
        raise ValueError(f"ttl_ms must be (0, {_MAX_TTL_MS}], got {ttl_ms}")
    ttl_s = ttl_ms / 1000.0

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            key = _make_key(func, args, kwargs)
            now = time.monotonic()
            if key is not None:
                cached_entry = _CACHE.get(key)
                if cached_entry is not None:
                    value, expires_at = cached_entry
                    if expires_at > now:
                        return value
            result = await func(*args, **kwargs)
            if key is not None:
                _CACHE[key] = (result, now + ttl_s)
            return result

        return wrapper

    return decorator


def cached(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """默认 TTL 缓存装饰器（100ms），等价 `ttl_cache(ttl_ms=100)(func)`。"""

    return ttl_cache(_DEFAULT_TTL_MS)(func)


def clear_cache(prefix: str | None = None) -> int:
    """失效缓存。`prefix=None` 清空全部；否则按 func_qualname 前缀匹配清。

    返回清掉的条目数。admin 写动作（cancel_order / pause_market 等）后调用
    `clear_cache(prefix="...")` 让下次查询拿到最新状态。
    """

    if prefix is None:
        count = len(_CACHE)
        _CACHE.clear()
        return count
    removed = [key for key in _CACHE if key[0].startswith(prefix)]
    for key in removed:
        _CACHE.pop(key, None)
    return len(removed)
