"""量化决策主类——装配 discovery / universe / trading / recovery / tracking 子模块。"""

from __future__ import annotations

import logging
from typing import Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent

from polymarket_trader.runtime.lifecycle_bus import LifecycleEnvelope as _LifecycleEnvelope, LifecycleEvent as _LifecycleEvent
from polymarket_trader.domain.decisions import AccountSnapshotView, DecisionContext, DecisionKind, QuantDecision, UniverseDecision
from polymarket_trader.domain.sports_live import LiveStateMatch
from polymarket_trader.pipeline.ingest.market_discovery.discovery_runner import DiscoveryQuery
from polymarket_trader.runtime.runtime_ports import RuntimePorts

from polymarket_trader.workflow.config import TradingWorkflowConfig, load_workflow_config
from polymarket_trader.workflow.discovery import (
    build_configured_discovery_queries,
    build_live_event_discovery_queries,
)
from polymarket_trader.workflow.live_state import (
    build_live_state_match,
    candidate_live_events_for_market,
    _market_sport_codes,
    _ensure_utc,
)
from polymarket_trader.workflow.outcomes import describe_sports_market, SportsMarketFamily
from polymarket_trader.workflow.tracking import build_filtered_tracking_market, should_keep_tracking
from polymarket_trader.workflow.trading.helpers import enrich_decision

logger = logging.getLogger(__name__)


class TradingWorkflow:
    """量化决策主对象——所有 family 走同一份决策路径（quant_decider）。

    薄装配层：
    - 配置定义在 ``config.py``
    - 市场筛选定义在 ``universe.py``
    - 决策（含入场/退出）定义在 ``quant_decider.py``
    - 恢复定义在 ``recovery.py``
    - 过滤后继续跟踪的规则定义在 ``tracking.py``
    - 量化信号 primitive 在 ``quant_signal.py``——所有量化信号统一从这里接入
    """

    def __init__(
        self,
        *,
        config: TradingWorkflowConfig,
        ports: RuntimePorts | None = None,
    ) -> None:
        self._config = config
        self._ports = ports or RuntimePorts()
        self._live_event_filter_cache_events_id: int | None = None
        self._live_event_filter_cache: dict[tuple[tuple[str, ...], str | None], tuple[LiveEvent, ...]] = {}
        self._live_state_no_feasible_source: bool = False
        from polymarket_trader.workflow.quant_decider import QuantDecider
        self._quant_decider = QuantDecider(config=config, ports=self._ports)
        if self._ports.lifecycle is not None:
            try:
                self._ports.lifecycle.subscribe(
                    _LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE,
                    self._on_live_state_no_feasible_source,
                )
            except Exception:
                logger.warning("workflow.lifecycle_subscribe_failed", exc_info=True)
    @property
    def config(self) -> TradingWorkflowConfig:
        return self._config

    @property
    def league_source_affinity(self) -> Mapping[str, tuple[str, ...]] | None:
        return self._config.league_source_affinity or None

    @property
    def ports(self) -> RuntimePorts:
        return self._ports

    def select_market(self, market: Market) -> UniverseDecision:
        from polymarket_trader.workflow.universe import select_market as _select_market
        return _select_market(self._config, market)

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]:
        return build_configured_discovery_queries(self._config)

    def discovery_queries_for_live_events(
        self,
        events: tuple[LiveEvent, ...],
    ) -> tuple[DiscoveryQuery, ...]:
        return build_live_event_discovery_queries(self._config, events)

    def quant_decide(self, context: DecisionContext) -> QuantDecision:
        """量化决策器——按 trigger_kind 分派内部子流程。

        ``context.quant_trigger_kind`` ∈ {"market_tick", "reconcile_cycle"}；
        reconcile 路径下还会处理 single_game live source 全断 → pause_trading
        的全局降级信号。
        """
        # 全源不可用时，single_game 市场主动暂停交易（仅 reconcile_cycle 触发时有意义）。
        if (
            context.quant_trigger_kind == "reconcile_cycle"
            and self._live_state_no_feasible_source
        ):
            descriptor = describe_sports_market(context.market) if context.market else None
            family = descriptor.market_family if descriptor is not None else None
            if family == SportsMarketFamily.SINGLE_GAME:
                return QuantDecision(
                    reason="sports_live_state_no_source",
                    actions=(),
                    pause_trading=True,
                    pause_reason="sports_live_state_no_source",
                )
        result = self._quant_decider.decide(context)
        if not result.actions:
            return result
        enriched = tuple(
            enrich_decision(action, default_kind=DecisionKind.EXIT)
            for action in result.actions
        )
        return QuantDecision(
            reason=result.reason,
            actions=enriched,
            pause_trading=result.pause_trading,
            pause_reason=result.pause_reason,
        )

    def should_keep_tracking(
        self,
        market: Market,
        account_snapshot: AccountSnapshotView | None,
    ) -> bool:
        return should_keep_tracking(market, account_snapshot)

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return build_filtered_tracking_market(
            candidate_market,
            existing_market=existing_market,
            reason=reason,
        )

    # --- market 分类（供 ingest / aggregators 调用）---

    def market_family_label(self, market: Market) -> str | None:
        descriptor = describe_sports_market(market)
        if not descriptor.accepted:
            return None
        return descriptor.market_family.value

    # --- 直播状态匹配（供 LiveSourceMatcher 通过 match_hook 注入）---

    def match_live_state(
        self,
        market: Market,
        events: tuple[LiveEvent, ...],
    ) -> "LiveStateMatch | None":
        descriptor = describe_sports_market(market)
        if not descriptor.accepted or descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
            return None
        candidate_events = self._candidate_live_events_for_market(market, events)
        return build_live_state_match(market, candidate_events)

    def _candidate_live_events_for_market(
        self,
        market: Market,
        events: tuple[LiveEvent, ...],
    ) -> tuple[LiveEvent, ...]:
        events_id = id(events)
        if self._live_event_filter_cache_events_id != events_id:
            self._live_event_filter_cache_events_id = events_id
            self._live_event_filter_cache.clear()
        sport_codes = tuple(sorted(_market_sport_codes(market)))
        market_start = _ensure_utc(market.game_start_time)
        cache_key = (sport_codes, None if market_start is None else market_start.isoformat())
        cached = self._live_event_filter_cache.get(cache_key)
        if cached is not None:
            return cached
        filtered = candidate_live_events_for_market(market, events)
        self._live_event_filter_cache[cache_key] = filtered
        return filtered

    async def _on_live_state_no_feasible_source(self, envelope: _LifecycleEnvelope) -> None:
        statuses = envelope.payload.get("source_statuses") or ()
        no_feasible = True
        for status in statuses:
            health = status.get("health") if isinstance(status, dict) else None
            if health in {"success_with_live_data", "cached"}:
                no_feasible = False
                break
        self._live_state_no_feasible_source = no_feasible

def build_workflow(
    *,
    ports: RuntimePorts | None = None,
    config_path: str | None = None,
) -> "TradingWorkflow":
    return TradingWorkflow(
        config=load_workflow_config(config_path),
        ports=ports,
    )
