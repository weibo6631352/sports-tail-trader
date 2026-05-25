"""Entry / event 的 metadata provider 工厂。

把 ``trading_decision_worker.entry_metadata_provider`` 和
``reconcile_worker.entry_metadata_provider`` 所需的两个 callable 从 main.py
拆出来——它们承载实际业务（exposure 按 market family 聚合 + entry_metadata
查询），不属于 composition root 职责。

两个函数都返回 callable（closure），让 main.py 拿到后直接传给 worker。
closure 捕获 registry / strategy / entry_metadata_store 三个长生命周期对象，
其它输入由 worker 调用时每次传入。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Mapping

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import current_exposure_usdc

if TYPE_CHECKING:
    from polymarket_trader.quant.workflow import TradingWorkflow
    from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
    from polymarket_trader.runtime.registry import MarketRegistry


def build_entry_metadata_for_event_provider(
    *,
    registry: "MarketRegistry",
    entry_metadata_store: "EntryMetadataStore",
    workflow: "TradingWorkflow",
) -> Callable[[Any, AccountSnapshot | None], Mapping[str, Any]]:
    """构造 ``trading_decision_worker`` 所用的 entry_metadata_provider。

    输入：事件 (DomainEvent) + account_snapshot。
    输出：base entry metadata 叠加按 market family 聚合的 outright/series 敞口。

    敞口聚合（``_portfolio_exposure_metadata``）跑在 entry_metadata 热路径
    上，仅做内存遍历 + O(1) registry 查询，无 IO；跳过当前 market 自身持仓
    （risk check 用 proposed_amount 对比），只汇总其他 outright/series 市场。
    """

    def _portfolio_exposure_metadata(
        snapshot: AccountSnapshot | None,
        current_condition_id: str | None,
        current_event_slug: str | None,
    ) -> dict[str, str]:
        if snapshot is None:
            return {}
        positions = snapshot.positions
        open_orders = snapshot.open_orders
        if not positions and not open_orders:
            return {}

        orders_by_condition: dict[str, list] = {}
        for order in open_orders:
            orders_by_condition.setdefault(order.condition_id, []).append(order)

        outright_total = Decimal("0")
        outright_event = Decimal("0")
        series_total = Decimal("0")
        series_event = Decimal("0")

        for position in positions:
            if position.condition_id == current_condition_id:
                continue
            pos_market = registry.get_by_condition_id(position.condition_id)
            if pos_market is None:
                continue
            family_label = workflow.market_family_label(pos_market)
            pos_orders = orders_by_condition.get(position.condition_id, ())
            exposure = current_exposure_usdc(position, pos_orders)
            if family_label == "outright":
                outright_total += exposure
                if current_event_slug and pos_market.event_slug == current_event_slug:
                    outright_event += exposure
            elif family_label == "series":
                series_total += exposure
                if current_event_slug and pos_market.event_slug == current_event_slug:
                    series_event += exposure

        result: dict[str, str] = {}
        if outright_total:
            result["outright_total_exposure_usdc"] = str(outright_total)
            result["outright_event_exposure_usdc"] = str(outright_event)
        if series_total:
            result["series_total_exposure_usdc"] = str(series_total)
            result["series_event_exposure_usdc"] = str(series_event)
        return result

    def entry_metadata_for_event(event, snapshot: AccountSnapshot | None):
        market = None
        if event.condition_id is not None:
            market = registry.get_by_condition_id(event.condition_id)
        if market is None and event.token_id is not None:
            market = registry.get_by_token_id(event.token_id)
        if market is None and event.market_slug is not None:
            market = registry.get_by_slug(event.market_slug)
        base = entry_metadata_store.metadata_for_event(event, market=market)
        exposure = _portfolio_exposure_metadata(
            snapshot,
            current_condition_id=market.condition_id if market else event.condition_id,
            current_event_slug=market.event_slug if market else event.event_slug,
        )
        if exposure:
            return {**base, **exposure}
        return base

    return entry_metadata_for_event


def build_entry_metadata_for_market_provider(
    *,
    entry_metadata_store: "EntryMetadataStore",
) -> Callable[[Any], Mapping[str, Any]]:
    """构造 ``reconcile_worker`` 所用的 entry_metadata_provider。

    输入：market 对象。输出：``entry_metadata_store.metadata_for`` 直接查询结果，
    不做敞口聚合（reconcile 路径不需要）。
    """

    def entry_metadata_for_market(market):
        return entry_metadata_store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )

    return entry_metadata_for_market
