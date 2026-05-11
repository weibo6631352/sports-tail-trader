"""``admin_query_mixin`` 过渡 shim。

实现已完全迁到 ``polymarket_trader.app.admin_query`` 子包：

- ``AdminQueryMixin`` 直接 re-export 自 ``admin_query``。
- 测试侧曾通过老路径 import 的辅助函数（``_compute_latency_payload``、
  ``_percentile`` 等）也 re-export，避免一次性破坏外部调用方。

保留一个发布周期供下游迁移，之后可整体删除。
"""

from __future__ import annotations

from polymarket_trader.app.admin_query import AdminQueryMixin
from polymarket_trader.app.admin_query._helpers import (
    _compute_latency_payload,
    _decision_record_payload,
    _empty_latency_payload,
    _percentile,
)

__all__ = [
    "AdminQueryMixin",
    "_compute_latency_payload",
    "_decision_record_payload",
    "_empty_latency_payload",
    "_percentile",
]
