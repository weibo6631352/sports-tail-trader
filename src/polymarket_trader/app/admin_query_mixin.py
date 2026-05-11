"""AdminService 查询方法 mixin（过渡 shim）。

所有查询方法已迁到 ``polymarket_trader.app.admin_query`` 子包；本模块只剩
``AdminQueryMixin`` 的别名 + 测试侧通过老路径 import 的辅助函数 re-export。
保留一个发布周期供下游迁移，之后可删除。
"""

from __future__ import annotations

from typing import Any

from polymarket_trader.app.admin_query import AdminQueryMixin as _AggregatedAdminQueryMixin
# 重导出供外部（tests/api/test_batch1_endpoints.py 等）通过老路径 import 的辅助函数；
# 真实定义已迁到 admin_query._helpers。
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


class AdminQueryMixin(_AggregatedAdminQueryMixin):
    """admin 只读查询的过渡入口；实现完全继承自 ``admin_query.AdminQueryMixin``。"""

    runtime: Any | None  # 宿主声明真正的字段；这里只是给类型检查器看
