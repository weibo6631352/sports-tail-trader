"""``admin_query`` 子包：把 AdminService 的只读查询方法按职责拆成子 mixin。

外部只暴露 ``AdminQueryMixin``（聚合所有子 mixin）。子 mixin 之间通过共享的
``_helpers`` 纯函数和 ``_protocol.AdminQueryHost`` 类型契约协作，运行时仍依赖
AdminService 的多重继承装配实际 helper。
"""

from __future__ import annotations

from polymarket_trader.app.admin_query.aggregate import AdminQueryMixin

__all__ = ["AdminQueryMixin"]
