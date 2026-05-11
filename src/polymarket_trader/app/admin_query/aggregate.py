"""聚合所有 admin 查询子 mixin。

最终形态：``class AdminQueryMixin(<sub_mixin_1>, <sub_mixin_2>, ...): pass``。
拆分推进期间，未抽取的方法仍由 ``admin_query_mixin`` 模块的旧类承担；通过
旧类继承本聚合类，将子 mixin 提供的方法注入到 AdminService 现有的继承链中。
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
    """admin 只读查询方法聚合。

    每次抽取一个子 mixin 时，将其加入到本类的基类元组；当前为骨架，
    后续 commit 逐步填充。
    """

    pass


__all__ = ["AdminQueryMixin"]
