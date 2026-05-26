"""【横切】api/middleware/ —— endpoint cache / auth / rate_limit。

docs/新架构方案.md §12.4。route 层薄化的关键支撑——把性能 / 鉴权 / 缓存
共性都放这里，route 自身只做参数校验 + 调 aggregator + 序列化。

# 模块

- `cache` —— 短 TTL 内存缓存装饰器（运营查询类 100ms 窗口，agent burst 调用 dedupe）

# endpoint metrics 集成位置

不在本目录——直接在 `api/app.py:_perf_middleware` 内同步双写
`SystemPerfMonitor`（admin 内部分析）+ `MetricsRegistry`（§11.2 标准 metric
output），避免两套 middleware 重复测同一段时间。

# 待扩展

- `auth` —— 已有 `api/deps.py:get_admin_service` 内的 token 校验，可独立成 middleware
- `rate_limit` —— 已有 `api/rate_limit.py`，可整合到本目录
"""

from .cache import cached, clear_cache, ttl_cache

__all__ = ["cached", "clear_cache", "ttl_cache"]
