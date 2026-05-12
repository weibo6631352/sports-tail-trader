"""聚合所有 admin 查询子 mixin。

``class AdminQueryMixin(<sub_mixin_1>, <sub_mixin_2>, ...): pass``——AdminService
直接继承本聚合类拿到所有 admin 查询方法。
"""

from __future__ import annotations

from polymarket_trader.app.admin_query.analytics import AdminAnalyticsQueryMixin
from polymarket_trader.app.admin_query.market import AdminMarketQueryMixin
from polymarket_trader.app.admin_query.reconcile_decisions import (
    AdminReconcileDecisionsQueryMixin,
)
from polymarket_trader.app.admin_query.runtime import AdminRuntimeQueryMixin
from polymarket_trader.app.admin_query.sports import AdminSportsQueryMixin
from polymarket_trader.app.admin_query.timeline import AdminTimelineQueryMixin
from polymarket_trader.app.admin_query.trading import AdminTradingQueryMixin


class AdminQueryMixin(
    AdminRuntimeQueryMixin,
    AdminMarketQueryMixin,
    AdminTradingQueryMixin,
    AdminTimelineQueryMixin,
    AdminReconcileDecisionsQueryMixin,
    AdminAnalyticsQueryMixin,
    AdminSportsQueryMixin,
):
    """admin 只读查询方法聚合。"""

    pass


__all__ = ["AdminQueryMixin"]
