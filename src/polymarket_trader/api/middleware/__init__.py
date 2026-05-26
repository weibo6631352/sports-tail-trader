"""【横切】api/middleware/ —— endpoint cache / auth / rate_limit。

docs/新架构方案.md §12.4。route 层薄化的关键支撑——把性能 / 鉴权 / 缓存
共性都放这里，route 自身只做参数校验 + 调 aggregator + 序列化。

# 模块

- `cache` —— 短 TTL 内存缓存装饰器（运营查询类 100ms 窗口，agent burst 调用 dedupe）
- `auth` —— admin token 鉴权 middleware（`install_admin_token_middleware`
  注册到 FastAPI app；`DEFAULT_AUTH_EXEMPT_PREFIXES` 维护豁免前缀）
- `rate_limit` —— per-endpoint token bucket（`rate_limit(endpoint, qps, burst)`
  FastAPI dependency 工厂，超限抛 429 + Retry-After）

# endpoint metrics 集成位置

不在本目录——直接在 `api/app.py:_perf_middleware` 内同步双写
`SystemPerfMonitor`（admin 内部分析）+ `MetricsRegistry`（§11.2 标准 metric
output），避免两套 middleware 重复测同一段时间。
"""

from .auth import DEFAULT_AUTH_EXEMPT_PREFIXES, install_admin_token_middleware
from .cache import cached, clear_cache, ttl_cache
from .rate_limit import rate_limit

__all__ = [
    "DEFAULT_AUTH_EXEMPT_PREFIXES",
    "cached",
    "clear_cache",
    "install_admin_token_middleware",
    "rate_limit",
    "ttl_cache",
]
