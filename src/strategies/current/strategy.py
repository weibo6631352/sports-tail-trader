"""当前默认策略的装配入口。

这个文件把 discovery、universe、trading、recovery、tracking 这些子模块
组装成一个完整的 ``BusinessExtension`` 实现。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from polymarket_trader.extension_api.live_state import SeriesState

from polymarket_trader.extension_api.lifecycle import LifecycleEnvelope as _LifecycleEnvelope, LifecycleEvent as _LifecycleEvent
from polymarket_trader.extension_api import (
    AccountSnapshotView,
    BusinessExtension,
    DecisionKind,
    DiscoveryQuery,
    EntrySizing,
    ExtensionSpec,
    LiveStateHooks,
    LiveStateMatch,
    RecoveryDecision,
    ExtensionContext,
    ExtensionDecision,
    ExtensionPorts,
    UniverseDecision,
)

from strategies.current.config import CurrentStrategyConfig, load_current_strategy_config
from strategies.current.identity import STRATEGY_ID
from strategies.current.discovery import (
    build_configured_discovery_queries,
    build_live_event_discovery_queries,
)
from strategies.current.live_state import (
    build_live_state_match,
    candidate_live_events_for_market,
    _market_sport_codes,
    _ensure_utc,
)
from strategies.current.trading.tail_bypass import market_tail_window_bypass_reason
from strategies.current.outcomes import describe_sports_market, SportsMarketFamily
from strategies.current.outright import (
    decide_outright_entry,
    resolve_outright_reject_label,
    size_outright_entry,
)
from strategies.current.outright.match import season_odds_from_metadata
from strategies.current.outright.team_resolver import resolve_market_team_debug
from strategies.current.parameter_overrides import active_ports_scope
from strategies.current.recovery import decide_recovery
from strategies.current.series import (
    decide_series_entry,
    resolve_series_reject_label,
    resolve_series_sub_type_label,
    series_state_from_metadata,
    size_series_entry,
)
from strategies.current.series.classifier import classify_series_sub_type
from strategies.current.series.types import SeriesSubType
from strategies.current.tracking import build_filtered_tracking_market, should_keep_tracking
from strategies.current.trading import decide_entry, decide_exit, decide_follow_up, size_entry
from strategies.current.trading.helpers import enrich_decision

logger = logging.getLogger(__name__)


_METRIC_OUTRIGHT_DECISION = "strategy_outright_decision_total"
_METRIC_OUTRIGHT_REJECT = "strategy_outright_reject_total"
_METRIC_SERIES_DECISION = "strategy_series_decision_total"
_METRIC_SERIES_REJECT = "strategy_series_reject_total"


class CurrentStrategy:
    """当前默认策略的主对象。

    这个类本身尽量保持"薄"：
    - 配置定义在 ``config.py``
    - 市场筛选定义在 ``universe.py``
    - 分配、入场、退出定义在 ``trading/``
    - 恢复定义在 ``recovery.py``
    - 过滤后继续跟踪的规则定义在 ``tracking.py``
    - outright / series 各自的 sizing + decide 定义在各自子包
    """

    def __init__(
        self,
        *,
        config: CurrentStrategyConfig,
        ports: ExtensionPorts | None = None,
    ) -> None:
        self._config = config
        self._ports = ports or ExtensionPorts()
        self._live_event_filter_cache_events_id: int | None = None
        self._live_event_filter_cache: dict[tuple[tuple[str, ...], str | None], tuple[LiveEvent, ...]] = {}
        self._live_state_no_feasible_source: bool = False
        # 把策略配置注册给 parameter store，让 GET /parameters 能返回 strategy.* 字段
        # 的当前 default_value。框架因此无需读策略私有属性。
        if self._ports.parameter is not None:
            self._ports.parameter.register_strategy_defaults(config)
        if self._ports.lifecycle is not None:
            try:
                self._ports.lifecycle.subscribe(
                    _LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE,
                    self._on_live_state_no_feasible_source,
                )
            except Exception:
                logger.warning("strategy.lifecycle_subscribe_failed", exc_info=True)
        self._spec = ExtensionSpec(
            strategy_id=STRATEGY_ID,
            name="current",
            version="1",
            description="Current runtime strategy implementation",
            config_type=CurrentStrategyConfig,
            capabilities=(
                "universe",
                "sizing",
                "entry",
                "exit",
                "recovery",
                "tracking",
            ),
        )

    @property
    def spec(self) -> ExtensionSpec:
        return self._spec

    @property
    def hooks(self) -> "CurrentStrategy":
        return self

    @property
    def live_state_hooks(self) -> "LiveStateHooks":
        return self

    @property
    def config(self) -> CurrentStrategyConfig:
        return self._config

    @property
    def league_source_affinity(self) -> Mapping[str, tuple[str, ...]] | None:
        return self._config.league_source_affinity or None

    def validate_config(self, settings: Any) -> tuple[Any, ...]:
        """在框架启动期校验策略侧配置是否足以正常运行。"""

        from polymarket_trader.config import ConfigIssue

        issues: list[ConfigIssue] = []

        if not self._config.discovery_title_searches and not self._config.discovery_tag_slugs:
            issues.append(
                ConfigIssue(
                    field="discovery_title_searches",
                    code="empty_strategy_discovery",
                    message="策略未配置任何 discovery 搜索词或 tag slug，远端 discovery 将拿不到候选市场",
                )
            )

        if not self._config.tail_enabled_market_types:
            issues.append(
                ConfigIssue(
                    field="tail_enabled_market_types",
                    code="empty_enabled_market_types",
                    message="策略未启用任何体育盘口类型，体育扫尾入场将永远 SKIP",
                )
            )

        auto_permissions = {
            "totals": self._config.tail_totals_execution_permission,
            "moneyline": self._config.tail_moneyline_execution_permission,
            "spreads": self._config.tail_spreads_execution_permission,
        }
        any_auto = any(
            permission.value == "auto_execute" for permission in auto_permissions.values()
        )
        if any_auto and settings.portfolio_budget_usdc <= Decimal("0"):
            issues.append(
                ConfigIssue(
                    field="portfolio_budget_usdc",
                    code="auto_execute_requires_portfolio_budget",
                    message="策略至少有一类盘口为 auto_execute，但 PORTFOLIO_BUDGET_USDC 不大于 0",
                )
            )

        if (
            self._config.tail_scale_in_max_buy_fills > 1
            and self._config.tail_scale_in_budget_fraction <= Decimal("0")
        ):
            issues.append(
                ConfigIssue(
                    field="tail_scale_in_budget_fraction",
                    code="invalid_scale_in_budget",
                    message="允许加仓但 tail_scale_in_budget_fraction <= 0，加仓预算永远为 0",
                )
            )

        if self._config.tail_outright_budget_usdc < Decimal("0"):
            issues.append(
                ConfigIssue(
                    field="tail_outright_budget_usdc",
                    code="negative_outright_budget",
                    message="tail_outright_budget_usdc 不能为负数",
                )
            )
        if self._config.tail_outright_min_edge_bps < 0 or self._config.tail_outright_min_edge_bps > 10000:
            issues.append(
                ConfigIssue(
                    field="tail_outright_min_edge_bps",
                    code="invalid_outright_min_edge",
                    message="tail_outright_min_edge_bps 必须在 [0, 10000] 范围内（0~100%）",
                )
            )
        if self._config.tail_outright_max_entry_price <= Decimal("0") or self._config.tail_outright_max_entry_price >= Decimal("1"):
            issues.append(
                ConfigIssue(
                    field="tail_outright_max_entry_price",
                    code="invalid_outright_max_entry_price",
                    message="tail_outright_max_entry_price 必须严格落在 (0, 1) 区间",
                )
            )
        if self._config.tail_outright_max_per_market_usdc <= Decimal("0"):
            issues.append(
                ConfigIssue(
                    field="tail_outright_max_per_market_usdc",
                    code="invalid_outright_per_market_cap",
                    message="tail_outright_max_per_market_usdc 必须大于 0",
                )
            )
        if self._config.tail_outright_season_odds_ttl_seconds > self._config.tail_outright_max_season_odds_age_seconds:
            issues.append(
                ConfigIssue(
                    field="tail_outright_season_odds_ttl_seconds",
                    code="ttl_exceeds_max_age",
                    message="tail_outright_season_odds_ttl_seconds 不能大于 tail_outright_max_season_odds_age_seconds，否则缓存命中即过期",
                )
            )
        if self._config.tail_outright_max_hold_horizon_days <= 0:
            issues.append(
                ConfigIssue(
                    field="tail_outright_max_hold_horizon_days",
                    code="invalid_outright_horizon",
                    message="tail_outright_max_hold_horizon_days 必须大于 0",
                )
            )

        outright_permission = self._config.tail_outright_execution_permission.value
        outright_budget = self._config.tail_outright_budget_usdc
        if outright_permission == "auto_execute" and outright_budget > Decimal("0"):
            if not settings.sports_season_state_enabled:
                issues.append(
                    ConfigIssue(
                        field="sports_season_state_enabled",
                        code="outright_auto_execute_requires_season_state",
                        message=(
                            "tail_outright_execution_permission=AUTO_EXECUTE 且 budget>0，"
                            "必须同时启用 SPORTS_SEASON_STATE_ENABLED"
                        ),
                    )
                )
            api_key = settings.sports_season_odds_api_key
            if api_key is None:
                issues.append(
                    ConfigIssue(
                        field="sports_season_odds_api_key",
                        code="outright_auto_execute_requires_odds_key",
                        message=(
                            "tail_outright_execution_permission=AUTO_EXECUTE 且 budget>0，"
                            "必须配置 SPORTS_SEASON_ODDS_API_KEY 以提供反向定价基准"
                        ),
                    )
                )
            if outright_budget < self._config.tail_outright_max_per_market_usdc:
                issues.append(
                    ConfigIssue(
                        field="tail_outright_budget_usdc",
                        code="outright_budget_below_per_market_cap",
                        message=(
                            "tail_outright_budget_usdc 小于 tail_outright_max_per_market_usdc，"
                            "AUTO_EXECUTE 模式下任何入场都会被 total_budget_exhausted 拒绝"
                        ),
                    )
                )

        return tuple(issues)

    @property
    def ports(self) -> ExtensionPorts:
        return self._ports

    def select_market(self, market: Market) -> UniverseDecision:
        from strategies.current.universe import select_market as _select_market
        return _select_market(self._config, market)

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]:
        return build_configured_discovery_queries(self._config)

    def discovery_queries_for_live_events(
        self,
        events: tuple[LiveEvent, ...],
    ) -> tuple[DiscoveryQuery, ...]:
        return build_live_event_discovery_queries(self._config, events)

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        """outright / series family 各自走独立预算包络，与 single_game 互不挤占。"""
        descriptor = describe_sports_market(context.market) if context.market else None
        if descriptor is not None and descriptor.market_family == SportsMarketFamily.OUTRIGHT:
            return size_outright_entry(self._config, context, self._ports)
        if descriptor is not None and descriptor.market_family == SportsMarketFamily.SERIES:
            return size_series_entry(self._config, context, self._ports)
        with active_ports_scope(self._ports):
            return size_entry(self._config, context)

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        """按 ``descriptor.market_family`` 分派到对应决策模块。"""
        descriptor = describe_sports_market(context.market) if context.market else None
        if descriptor is not None and descriptor.market_family == SportsMarketFamily.OUTRIGHT:
            decision = enrich_decision(
                decide_outright_entry(self._config, context, self._ports),
                default_kind=DecisionKind.ENTRY,
            )
            self._record_outright_decision_metric(decision)
            return decision
        if descriptor is not None and descriptor.market_family == SportsMarketFamily.SERIES:
            decision = enrich_decision(
                decide_series_entry(self._config, context, self._ports),
                default_kind=DecisionKind.ENTRY,
            )
            self._record_series_decision_metric(decision)
            return decision
        with active_ports_scope(self._ports):
            return enrich_decision(decide_entry(self._config, context), default_kind=DecisionKind.ENTRY)

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        with active_ports_scope(self._ports):
            return enrich_decision(decide_exit(self._config, context), default_kind=DecisionKind.EXIT)

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return decide_follow_up(self._config, context)

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        """额外路径：全源不可用时，single_game 市场主动暂停交易。"""
        if self._live_state_no_feasible_source:
            descriptor = describe_sports_market(context.market) if context.market else None
            family = descriptor.market_family if descriptor is not None else None
            if family == SportsMarketFamily.SINGLE_GAME:
                return RecoveryDecision(
                    reason="sports_live_state_no_source",
                    actions=(),
                    pause_trading=True,
                    pause_reason="sports_live_state_no_source",
                )
        recovery = decide_recovery(self._config, context)
        if not recovery.actions:
            return recovery
        enriched = tuple(enrich_decision(action, default_kind=DecisionKind.RECOVERY) for action in recovery.actions)
        return RecoveryDecision(
            reason=recovery.reason,
            actions=enriched,
            pause_trading=recovery.pause_trading,
            pause_reason=recovery.pause_reason,
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

    # --- MarketClassificationHooks implementation ---

    def is_outright_market(self, market: Market) -> bool:
        descriptor = describe_sports_market(market)
        return descriptor.accepted and descriptor.market_family == SportsMarketFamily.OUTRIGHT

    def is_series_winner_market(self, market: Market) -> bool:
        descriptor = describe_sports_market(market)
        if not descriptor.accepted or descriptor.market_family != SportsMarketFamily.SERIES:
            return False
        return classify_series_sub_type(market) == SeriesSubType.WINNER

    def sport_key_for_season_odds(self, market: Market) -> str | None:
        text = " ".join(filter(None, (market.category or "", *(market.tags or ())))).lower()
        if "nba" in text or "basketball" in text:
            return "basketball_nba"
        if "nhl" in text or "hockey" in text:
            return "icehockey_nhl"
        if "nfl" in text or "american football" in text:
            return "americanfootball_nfl"
        if "mlb" in text or "baseball" in text:
            return "baseball_mlb"
        if "epl" in text or "premier league" in text:
            return "soccer_epl"
        return None

    def sport_key_for_series_state(self, market: Market) -> str | None:
        text = " ".join(filter(None, (market.category or "", *(market.tags or ())))).lower()
        if "nba" in text or "basketball" in text:
            return "nba"
        if "nhl" in text or "hockey" in text:
            return "nhl"
        if "mlb" in text or "baseball" in text:
            return "mlb"
        return None

    def sport_key_for_game_odds(self, market: Market) -> str | None:
        text = " ".join(filter(None, (market.category or "", *(market.tags or ())))).lower()
        if "nba" in text or "basketball" in text:
            return "basketball_nba"
        if "nhl" in text or "hockey" in text:
            return "icehockey_nhl"
        if "mlb" in text or "baseball" in text:
            return "baseball_mlb"
        return None

    def market_family_label(self, market: Market) -> str | None:
        descriptor = describe_sports_market(market)
        if not descriptor.accepted:
            return None
        return descriptor.market_family.value

    # --- SportsDiagnosticHooks implementation ---

    def series_state_from_metadata(
        self, metadata: Mapping[str, Any]
    ) -> SeriesState | None:
        return series_state_from_metadata(metadata)

    def season_odds_from_metadata(
        self, metadata: Mapping[str, Any]
    ) -> SeasonOddsSnapshot | None:
        return season_odds_from_metadata(metadata)

    def resolve_outright_team_debug_payload(
        self,
        market: Market,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        snapshot = season_odds_from_metadata(metadata)
        if snapshot is None:
            return None
        return resolve_market_team_debug(market, snapshot).as_payload()

    # --- LiveStateHooks implementation ---

    def match_live_state(
        self,
        market: Market,
        events: tuple[LiveEvent, ...],
    ) -> "LiveStateMatch | None":
        descriptor = describe_sports_market(market)
        if not descriptor.accepted or descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
            return None
        candidate_events = self._candidate_live_events_for_market(market, events)

        def _bypass(matched_market, event):
            return market_tail_window_bypass_reason(matched_market, event, descriptor, self._config)

        return build_live_state_match(
            market,
            candidate_events,
            market_end_horizon_seconds=self._config.tail_market_end_horizon_seconds,
            bypass_resolver=_bypass,
        )

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

    def _record_outright_decision_metric(self, decision: ExtensionDecision) -> None:
        metrics = self._ports.metrics
        if metrics is None:
            return
        outcome = "accepted" if decision.action.value == "buy" else "rejected"
        metrics.inc_counter(_METRIC_OUTRIGHT_DECISION, labels={"outcome": outcome})
        if outcome == "rejected":
            reason = resolve_outright_reject_label(decision)
            metrics.inc_counter(_METRIC_OUTRIGHT_REJECT, labels={"reason": reason})

    def _record_series_decision_metric(self, decision: ExtensionDecision) -> None:
        metrics = self._ports.metrics
        if metrics is None:
            return
        sub_type = resolve_series_sub_type_label(decision)
        outcome = "accepted" if decision.action.value == "buy" else "rejected"
        metrics.inc_counter(
            _METRIC_SERIES_DECISION,
            labels={"sub_type": sub_type, "outcome": outcome},
        )
        if outcome == "rejected":
            reason = resolve_series_reject_label(decision)
            metrics.inc_counter(
                _METRIC_SERIES_REJECT,
                labels={"sub_type": sub_type, "reason": reason},
            )


def build_strategy(
    *,
    ports: ExtensionPorts | None = None,
    config_path: str | None = None,
) -> BusinessExtension:
    return CurrentStrategy(
        config=load_current_strategy_config(config_path),
        ports=ports,
    )
