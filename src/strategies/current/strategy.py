"""当前默认策略的装配入口。

这个文件把 discovery、universe、trading、recovery、tracking 这些子模块
组装成一个完整的 ``BusinessExtension`` 实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Any, Mapping

from polymarket_trader.domain.order import ManagedOrderIntent
from polymarket_trader.domain.orderbook import OrderbookSnapshot

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
    MarketTokenView,
    RecoveryDecision,
    ExtensionContext,
    ExtensionDecision,
    ExtensionPorts,
    StrategySummary,
    UniverseDecision,
)

from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import BaseballGameState, LiveEvent, TennisGameState

from strategies.current.config import CurrentStrategyConfig, OUTRIGHT_FALLBACK_ENTRY_PRICE, load_current_strategy_config
from strategies.current.identity import STRATEGY_ID
from strategies.current.discovery import (
    build_configured_discovery_queries,
    build_live_event_discovery_queries,
)
from strategies.current.exit_plan import cap_price_to_clob_limit, build_exit_plan_metadata, exit_price_for_context
from strategies.current.live_state import build_live_state_match
from strategies.current.outcomes import SportsMarketDescriptor, describe_sports_market
from strategies.current.allocation import AllocationMarketSnapshot, ProbView, kelly_plan
from strategies.current.outright import (
    OutrightEvaluation,
    check_outright_entry_risk,
    evaluate_outright_opportunity,
    season_odds_from_metadata,
)
from strategies.current.outright.pricing import outright_fair_value
from strategies.current.series import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesEvaluatorInputs,
    SeriesSubType,
    SeriesSubTypeRiskConfig,
    check_series_entry_risk,
    evaluate_series_opportunity,
)
from strategies.current.parameter_overrides import active_ports_scope, effective_decimal, effective_int
from strategies.current.recovery import decide_recovery
from strategies.current.tail.types import ExecutionPermission as _ExecPerm
from strategies.current.tracking import build_filtered_tracking_market, should_keep_tracking
from strategies.current.trading import decide_entry, decide_exit, size_entry
from strategies.current.trading.helpers import (
    bid_plus_tick_fallback_ask,
    bid_plus_tick_fallback_metadata,
)
from strategies.current.universe import select_market

logger = logging.getLogger(__name__)


# 命中率 / 拒绝原因指标。Worktree 5：把新增拒绝原因纳入有界 metric 维度，让
# 线上能从 /metrics 端点直接读出 outright / series 的 accepted / rejected 命中率
# 与 top reason 分布。所有 label 维度都是 StrEnum 值（reason）或 SubType 值
# （sub_type），不含 condition_id / token_id 之类无界 label，避免 registry 膨胀。
_METRIC_OUTRIGHT_DECISION = "strategy_outright_decision_total"
_METRIC_OUTRIGHT_REJECT = "strategy_outright_reject_total"
_METRIC_SERIES_DECISION = "strategy_series_decision_total"
_METRIC_SERIES_REJECT = "strategy_series_reject_total"


@dataclass(frozen=True, slots=True)
class _MockTokenView:
    """测试兜底 token view（生产路径 framework 注入真正的 MarketTokenView）。"""

    token_id: str
    outcome: str
    orderbook: OrderbookSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _SeriesSubTypeSettings:
    """sub_type 维度的策略配置切片：解耦 strategy._size_series / _decide_series 与 CurrentStrategyConfig。

    Series 子类型（WINNER / TOTAL_GAMES / HANDICAP）的 budget / cap / horizon 各自独立，
    把对应字段挑选成这一份 dataclass 后，下游 sizing / decide 只读它，不再针对每个
    sub_type 写 if-else 分支。
    """

    execution_permission: _ExecPerm
    min_edge_bps: int
    max_entry_price: Decimal
    budget_usdc: Decimal
    max_per_market_usdc: Decimal
    max_event_correlation_usdc: Decimal
    min_orderbook_depth_usdc: Decimal
    max_state_age_seconds: int
    max_game_odds_age_seconds: int
    max_hold_horizon_days: int
    min_remaining_days: int


class CurrentStrategy:
    """当前默认策略的主对象。

    这个类本身尽量保持“薄”：
    - 配置定义在 ``config.py``
    - 市场筛选定义在 ``universe.py``
    - 分配、入场、退出定义在 ``trading.py``
    - 恢复定义在 ``recovery.py``
    - 过滤后继续跟踪的规则定义在 ``tracking.py``

    这样拆分后，二次开发可以直接按关注点替换单个文件，
    不必在一个大类里来回跳。
    """

    def __init__(
        self,
        *,
        config: CurrentStrategyConfig,
        ports: ExtensionPorts | None = None,
    ) -> None:
        """初始化当前策略。

        参数：
            config:
                当前策略配置对象，包含价格阈值、扫描词、筛选 token 等业务参数。
            ports:
                框架注入给策略的应用层端口集合。当前默认策略暂时没有深度使用，
                但这里保留接口，是为了让后续策略能通过稳定端口读取市场、
                账户、运行时和遥测能力，而不是直接依赖框架内部实现。
        """

        self._config = config
        self._ports = ports or ExtensionPorts()
        self._live_event_filter_cache_events_id: int | None = None
        self._live_event_filter_cache: dict[tuple[tuple[str, ...], str | None], tuple[LiveEvent, ...]] = {}
        # 订阅 LIVE_STATE_NO_FEASIBLE_SOURCE：worker 一旦发现所有源都不健康，
        # recovery 路径据此把 single_game 市场进入 sports_live_state_no_source 暂停。
        self._live_state_no_feasible_source: bool = False
        if self._ports.lifecycle is not None:
            try:
                self._ports.lifecycle.subscribe(
                    _LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE,
                    self._on_live_state_no_feasible_source,
                )
            except Exception:
                # 订阅失败不阻断 boot；缺少订阅时 recovery 退化到 stale-age 判断。
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
        """返回策略元信息。

        框架会用它展示策略名、版本、能力列表以及配置类型。
        """

        return self._spec

    @property
    def hooks(self) -> "CurrentStrategy":
        """暴露策略 hook 集合。"""

        return self

    @property
    def live_state_hooks(self) -> "LiveStateHooks":
        """本策略消费体育直播；自身同时实现 ``LiveStateHooks``。"""

        return self

    @property
    def league_source_affinity(self) -> Mapping[str, tuple[str, ...]] | None:
        return self._config.league_source_affinity or None

    def validate_config(self, settings: Any) -> tuple[Any, ...]:
        """在框架启动期校验策略侧配置是否足以正常运行。

        与 ``Settings.validate_startup_readiness()`` 互补：框架那一侧已经检查
        了金额、密钥、扩展模块路径等通用配置；这里集中检查“当前策略本身需要
        的最小可执行集”，避免上线后才发现 discovery 列表被清空、permission
        和预算之间不一致这类启动期可暴露的问题。
        """

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

        # Outright 数值不变式：发现明显非法配置时启动期就拒绝，避免运行时
        # 才暴露（默认值都合法，所以下列检查只在用户主动改 settings 后命中）。
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

        # Outright AUTO_EXECUTE 必须前置数据源：缺 season state 或缺 odds api key 时拒绝启动。
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
                # 单市场上限大于总预算时永远凑不出第二笔 outright 入场，且首单也会被
                # check_outright_entry_risk 的 total_budget 检查在 proposed=per_market 时拒绝。
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
        """暴露框架注入的应用层端口。

        当前默认策略主要依赖传入的 ``ExtensionContext``，
        但其他策略可以在内部按需使用这些端口读取更多运行时信息。
        """

        return self._ports

    def select_market(self, market: Market) -> UniverseDecision:
        """判断 market 是否属于当前策略 universe。"""

        return select_market(self._config, market)

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]:
        """返回当前策略希望远端 discovery 使用的粗筛查询。"""

        return build_configured_discovery_queries(self._config)

    def discovery_queries_for_live_events(
        self,
        events: tuple[LiveEvent, ...],
    ) -> tuple[DiscoveryQuery, ...]:
        """用直播源里的真实事件补充高意图 market discovery 查询。"""

        return build_live_event_discovery_queries(self._config, events)

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        """为当前 market 生成入场预算分配结果。

        outright / series family 各自走独立预算包络，与 single_game 互不挤占。
        预算=0 时返回空 sizing；>0 时按 per-market 上限平分并接入 Kelly。
        """

        descriptor = describe_sports_market(context.market) if context.market else None
        if descriptor is not None and descriptor.market_family.value == "outright":
            return self._size_outright_entry(context)
        if descriptor is not None and descriptor.market_family.value == "series":
            return self._size_series_entry(context)
        with active_ports_scope(self._ports):
            return size_entry(self._config, context)

    def _size_outright_entry(self, context: ExtensionContext) -> EntrySizing:
        """Outright family Kelly sizing。

        用赛季赔率 ``outright_fair_value()`` 作为真概率（conf=1.0）喂 Kelly 公式，
        与 single-game tail 共用 ``kelly_plan`` 路径。Outright 使用独立预算包络，
        不占用 single_game 资金。
        """

        config = self._config
        budget = config.tail_outright_budget_usdc
        if budget <= Decimal("0"):
            return EntrySizing(
                allocation_plan=AllocationPlan(
                    trace_id=context.trace_id,
                    total_budget_usdc=budget,
                    reason="outright_budget_zero",
                ),
                reason="outright_budget_zero",
            )

        market = context.market
        if market is None:
            return EntrySizing(
                allocation_plan=AllocationPlan(
                    trace_id=context.trace_id,
                    total_budget_usdc=budget,
                    reason="outright_missing_market",
                ),
                reason="outright_missing_market",
            )

        kelly_fraction = context.kelly_fraction
        kelly_max_position_fraction = context.kelly_max_position_fraction
        kelly_min_stake_usdc = context.kelly_min_stake_usdc
        if kelly_fraction is None or kelly_max_position_fraction is None or kelly_min_stake_usdc is None:
            # Kelly 参数由 EntryPlanner 注入；缺失说明框架契约违反，记录并退化到平坦预算。
            logger.warning(
                "outright_sizing.missing_kelly_params: trace_id=%s condition_id=%s",
                context.trace_id,
                market.condition_id,
            )
            return EntrySizing(
                allocation_plan=AllocationPlan(
                    trace_id=context.trace_id,
                    total_budget_usdc=budget,
                    reason="outright_sizing_no_kelly_params",
                ),
                reason="outright_sizing_no_kelly_params",
                metadata={
                    "outright_budget_usdc": str(budget),
                    "kelly_path": "not_applied",
                },
            )

        snapshot = season_odds_from_metadata(context.metadata or {})
        token_views = tuple(context.market_token_views or ())
        outcome_by_token: dict[str, str] = {
            tv.token_id: (tv.outcome or "") for tv in token_views if tv.token_id
        }

        per_market_cap = min(budget, config.tail_outright_max_per_market_usdc)
        market_snapshots: list[AllocationMarketSnapshot] = []
        for tv in token_views:
            if not tv.token_id:
                continue
            ob = tv.orderbook
            best_ask = ob.best_ask if ob is not None else None
            market_snapshots.append(AllocationMarketSnapshot(
                market=market,
                token_id=tv.token_id,
                orderbook=ob,
                best_ask=best_ask,
                strategy_budget_cap_usdc=per_market_cap,
            ))

        def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
            outcome_label = outcome_by_token.get(snap.token_id, "")
            if snapshot is None or not outcome_label:
                return ProbView(prob_p=None, prob_confidence=Decimal("0"), source="outright_real_missing")
            pricing_result = outright_fair_value(snapshot, snap.market, outcome_label)
            if pricing_result.value is None:
                source = (
                    f"outright_real_rejected:{pricing_result.reject.value}"
                    if pricing_result.reject is not None
                    else "outright_real_missing"
                )
                return ProbView(prob_p=None, prob_confidence=Decimal("0"), source=source)
            # 赛季赔率是真实概率（非 implied），conf=1.0 不做折扣。
            return ProbView(prob_p=pricing_result.value, prob_confidence=Decimal("1"), source="outright_real")

        plan = kelly_plan(
            trace_id=context.trace_id,
            bankroll_usdc=context.bankroll_usdc or budget,
            portfolio_budget_usdc=budget,
            markets=tuple(market_snapshots),
            prob_provider=_prob_provider,
            kelly_fraction=kelly_fraction,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_min_edge=context.kelly_min_edge or Decimal("0"),
            kelly_min_stake_usdc=kelly_min_stake_usdc,
        )
        return EntrySizing(allocation_plan=plan, reason="outright_kelly")

    def _size_series_entry(self, context: ExtensionContext) -> EntrySizing:
        """Series Kelly sizing（WINNER / TOTAL_GAMES / HANDICAP 共享）。

        设计要点：sub_type 在分类阶段决定；budget / cap / min_edge 等参数从
        ``_series_subtype_settings`` 取对应包络。Kelly 的 prob_p 由 evaluator
        已经算好的 fair_value 直接喂（conf=1.0），避免在 sizing 阶段重跑模型——
        evaluator 是单一定价真相来源（CLAUDE.md §10 单义命名）。
        """

        market = context.market
        if market is None:
            return EntrySizing(
                allocation_plan=AllocationPlan(
                    trace_id=context.trace_id,
                    total_budget_usdc=Decimal("0"),
                    reason="series_missing_market",
                ),
                reason="series_missing_market",
            )

        sub_type, settings = self._series_subtype_settings(market)
        budget = settings.budget_usdc
        if budget <= Decimal("0"):
            return EntrySizing(
                allocation_plan=AllocationPlan(
                    trace_id=context.trace_id,
                    total_budget_usdc=budget,
                    reason=f"series_{sub_type.value}_budget_zero",
                ),
                reason=f"series_{sub_type.value}_budget_zero",
            )

        kelly_fraction = context.kelly_fraction
        kelly_max_position_fraction = context.kelly_max_position_fraction
        kelly_min_stake_usdc = context.kelly_min_stake_usdc
        if kelly_fraction is None or kelly_max_position_fraction is None or kelly_min_stake_usdc is None:
            # 框架契约违反——EntryPlanner 应注入 Kelly 参数；缺失记日志并降级。
            logger.warning(
                "series_sizing.missing_kelly_params: trace_id=%s condition_id=%s",
                context.trace_id,
                market.condition_id,
            )
            return EntrySizing(
                allocation_plan=AllocationPlan(
                    trace_id=context.trace_id,
                    total_budget_usdc=budget,
                    reason="series_sizing_no_kelly_params",
                ),
                reason="series_sizing_no_kelly_params",
                metadata={
                    "series_budget_usdc": str(budget),
                    "series_sub_type": sub_type.value,
                    "kelly_path": "not_applied",
                },
            )

        metadata = context.metadata or {}
        now = context.now or datetime.now(timezone.utc)
        token_views = tuple(context.market_token_views or ())

        per_market_cap = min(budget, settings.max_per_market_usdc)
        fair_value_by_token: dict[str, Decimal] = {}
        market_snapshots: list[AllocationMarketSnapshot] = []
        for tv in token_views:
            if not tv.token_id:
                continue
            ob = tv.orderbook
            best_ask = ob.best_ask if ob is not None else None
            buyable_usdc = (
                ob.buyable_ask_depth(max_price=settings.max_entry_price) * best_ask
                if ob is not None and best_ask is not None
                else Decimal("0")
            )
            evaluator_inputs = SeriesEvaluatorInputs(
                best_ask=best_ask,
                buyable_liquidity_usdc=buyable_usdc,
                now=now,
                min_edge_bps=settings.min_edge_bps,
                max_entry_price=settings.max_entry_price,
                min_orderbook_depth_usdc=settings.min_orderbook_depth_usdc,
                max_series_state_age_seconds=settings.max_state_age_seconds,
                max_game_odds_age_seconds=settings.max_game_odds_age_seconds,
                season_snapshot=None,
            )
            candidate = SeriesCandidate(
                market=market,
                outcome_label=tv.outcome or "",
                token_id=tv.token_id,
                metadata=metadata,
            )
            evaluation = evaluate_series_opportunity(candidate, inputs=evaluator_inputs)
            if evaluation.fair_value is not None:
                # accepted 与 gate-only reject（INSUFFICIENT_EDGE / PRICE_ABOVE_FAIR 等）
                # 都已算出 fair_value——sizing 仍可用。但 MISSING_STATE / OUTCOME_NOT_PARSED
                # 等定价前置失败时 fair_value=None，Kelly 自动跳过该 market。
                fair_value_by_token[tv.token_id] = evaluation.fair_value
            market_snapshots.append(AllocationMarketSnapshot(
                market=market,
                token_id=tv.token_id,
                orderbook=ob,
                best_ask=best_ask,
                strategy_budget_cap_usdc=per_market_cap,
            ))

        def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
            fair = fair_value_by_token.get(snap.token_id)
            if fair is None:
                return ProbView(
                    prob_p=None,
                    prob_confidence=Decimal("0"),
                    source=f"series_{sub_type.value}_no_fair_value",
                )
            # evaluator 输出已是真概率（非 implied），conf=1.0 不再折扣。
            return ProbView(
                prob_p=fair,
                prob_confidence=Decimal("1"),
                source=f"series_{sub_type.value}_real",
            )

        plan = kelly_plan(
            trace_id=context.trace_id,
            bankroll_usdc=context.bankroll_usdc or budget,
            portfolio_budget_usdc=budget,
            markets=tuple(market_snapshots),
            prob_provider=_prob_provider,
            kelly_fraction=kelly_fraction,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_min_edge=context.kelly_min_edge or Decimal("0"),
            kelly_min_stake_usdc=kelly_min_stake_usdc,
        )
        return EntrySizing(allocation_plan=plan, reason=f"series_{sub_type.value}_kelly")

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        """根据盘口和预算生成 BUY 决策。

        按 ``descriptor.market_family`` 分派：
        - SINGLE_GAME → 现有 tail 链路（live state + tail evaluator）
        - OUTRIGHT → outright 子包评估（赛季隐含概率反向定价）

        以上是 §9 必要的可审计建模：outright 不再静默拒绝，而是走 outright 评估器
        输出可审计原因（缺数据、edge 不足等），或在 budget>0 + AUTO_EXECUTE 时
        返回 BUY。
        """

        descriptor = describe_sports_market(context.market) if context.market else None
        if descriptor is not None and descriptor.market_family.value == "outright":
            decision = _enrich_decision(
                self._decide_outright_entry(context),
                default_kind=DecisionKind.ENTRY,
            )
            self._record_outright_decision_metric(decision)
            return decision
        if descriptor is not None and descriptor.market_family.value == "series":
            # 子类型（WINNER / TOTAL_GAMES / GAME_HANDICAP）全部接通真实定价模型；
            # accepted 路径在 budget 解锁 + AUTO_EXECUTE 下产 BUY，其余路径 SKIP 携带
            # 可审计 reject_reason。
            decision = _enrich_decision(
                self._decide_series_entry(context),
                default_kind=DecisionKind.ENTRY,
            )
            self._record_series_decision_metric(decision)
            return decision
        with active_ports_scope(self._ports):
            return _enrich_decision(decide_entry(self._config, context), default_kind=DecisionKind.ENTRY)

    def _record_outright_decision_metric(self, decision: ExtensionDecision) -> None:
        """同步上报 outright 决策结果到 MetricsRegistry。

        P0 路径：``inc_counter`` 是 RLock 保护的内存 dict 写入（O(1)），无 IO；
        ``NullMetricsPort`` 场景下整体退化为函数 ret。所有 label 都是 bounded
        集合（outcome ∈ accepted/rejected，reason 来自 ``OutrightRejectReason``
        StrEnum 或固定字符串），不会引入 registry 膨胀。
        """

        metrics = self._ports.metrics
        if metrics is None:
            return
        outcome = "accepted" if decision.action.value == "buy" else "rejected"
        metrics.inc_counter(_METRIC_OUTRIGHT_DECISION, labels={"outcome": outcome})
        if outcome == "rejected":
            reason = _resolve_outright_reject_label(decision)
            metrics.inc_counter(_METRIC_OUTRIGHT_REJECT, labels={"reason": reason})

    def _record_series_decision_metric(self, decision: ExtensionDecision) -> None:
        """同步上报 series 决策结果到 MetricsRegistry，按 sub_type 维度分桶。"""

        metrics = self._ports.metrics
        if metrics is None:
            return
        sub_type = _resolve_series_sub_type_label(decision)
        outcome = "accepted" if decision.action.value == "buy" else "rejected"
        metrics.inc_counter(
            _METRIC_SERIES_DECISION,
            labels={"sub_type": sub_type, "outcome": outcome},
        )
        if outcome == "rejected":
            reason = _resolve_series_reject_label(decision)
            metrics.inc_counter(
                _METRIC_SERIES_REJECT,
                labels={"sub_type": sub_type, "reason": reason},
            )

    def _decide_outright_entry(self, context: ExtensionContext) -> ExtensionDecision:
        """outright 子包驱动的入场决策。

        遍历每个 outcome 的 ``MarketTokenView``，对每个 token 跑一次评估器；
        选 edge 最大的接受候选；都拒绝则返回最早出现的 reject 原因供审计。
        """

        market = context.market
        if market is None:
            return ExtensionDecision.skip(reason="outright_missing_market")
        snapshot = season_odds_from_metadata(context.metadata or {})
        token_views = tuple(context.market_token_views or ())
        outcomes = market.outcomes
        if not token_views and not outcomes:
            return ExtensionDecision.skip(
                reason="outright_missing_outcomes",
                metadata={"market_family": "outright"},
            )
        # 框架真正驱动时 token_views 必填；测试场景下可能只给 outcomes，做兜底。
        if not token_views:
            token_views = tuple(
                _MockTokenView(token_id=o.token_id, outcome=o.outcome)
                for o in outcomes
            )
        config = self._config
        permission = config.tail_outright_execution_permission
        # 双闸门：未到 AUTO_EXECUTE 或 budget=0 时，evaluator 仍跑但强制降级到 RECORD_ONLY 视图，
        # 让管理面看到诊断但不会构造 BUY。
        budget_unlocked = config.tail_outright_budget_usdc > Decimal("0")
        effective_permission = (
            permission if budget_unlocked else _ExecPerm.RECORD_ONLY
        )
        now = context.now or datetime.now(timezone.utc)
        best_accept: tuple[tuple[Decimal, OutrightEvaluation], MarketTokenView] | None = None
        first_reject: tuple[OutrightEvaluation, MarketTokenView] | None = None
        for token_view in token_views:
            orderbook = token_view.orderbook
            best_ask = orderbook.best_ask if orderbook is not None else None
            # missing_best_ask fallback：盘口有 best_bid + tick_size 时用 bid+tick 估算
            # 一个"理论可成交 ask"，让 evaluator 仍然能跑出 fair_value 与估算 ask 的对比
            # 并产生 RECORD_ONLY 决策——下单不允许，但分析路径不再 silent drop（§10
            # 拒绝原因可审计）。所有 fallback metadata 透传到 decision_records 用于校准。
            permission_for_token = effective_permission
            fallback_meta: dict[str, str] = {}
            if best_ask is None and orderbook is not None:
                fallback_ask = bid_plus_tick_fallback_ask(orderbook, market.tick_size)
                if fallback_ask is not None:
                    best_ask = fallback_ask
                    permission_for_token = _ExecPerm.RECORD_ONLY
                    fallback_meta = bid_plus_tick_fallback_metadata(
                        orderbook=orderbook,
                        tick_size=market.tick_size,
                        fallback_ask=fallback_ask,
                    )
                    logger.warning(
                        "bid_plus_tick_fallback: no best_ask, using bid+tick=%s for token=%s market=%s",
                        fallback_ask,
                        token_view.token_id,
                        market.condition_id,
                    )
            buyable = (
                orderbook.buyable_ask_depth(max_price=config.tail_outright_max_entry_price)
                if orderbook is not None
                else Decimal("0")
            )
            # buyable_ask_depth 返回的是 shares 数；按价格折算成 USDC 才能与 min_orderbook_depth_usdc 比较。
            # best_ask 为 None 时 buyable 必然为 0（无 ask），直接置零避免语义混乱。
            buyable_usdc = buyable * best_ask if best_ask is not None else Decimal("0")
            # Runtime override 优先于 frozen config——让 agent 通过 PUT
            # /parameters/strategy/{key} 实时调阈值。无 port 或无 override 时
            # 行为与原来完全一致。
            evaluation = evaluate_outright_opportunity(
                snapshot=snapshot,
                market=market,
                outcome_label=token_view.outcome,
                token_id=token_view.token_id,
                best_ask=best_ask,
                buyable_liquidity_usdc=buyable_usdc,
                now=now,
                max_season_odds_age_seconds=config.tail_outright_max_season_odds_age_seconds,
                min_edge_bps=effective_int(
                    self._ports,
                    "tail_outright_min_edge_bps",
                    config.tail_outright_min_edge_bps,
                ),
                max_entry_price=effective_decimal(
                    self._ports,
                    "tail_outright_max_entry_price",
                    config.tail_outright_max_entry_price,
                ),
                min_orderbook_depth_usdc=effective_decimal(
                    self._ports,
                    "tail_outright_min_orderbook_depth_usdc",
                    config.tail_outright_min_orderbook_depth_usdc,
                ),
                exit_edge_target=effective_decimal(
                    self._ports,
                    "tail_outright_exit_edge_target",
                    config.tail_outright_exit_edge_target,
                ),
                min_profit_per_share=effective_decimal(
                    self._ports,
                    "tail_outright_min_profit_per_share",
                    config.tail_outright_min_profit_per_share,
                ),
                execution_permission=permission_for_token,
                extra_metadata=fallback_meta or None,
            )
            if evaluation.accepted:
                edge = (evaluation.fair_value or Decimal(0)) - (best_ask or Decimal(0))
                if best_accept is None or edge > best_accept[0][0]:
                    best_accept = ((edge, evaluation), token_view)
            elif first_reject is None:
                first_reject = (evaluation, token_view)
        if best_accept is None:
            evaluation, token_view = first_reject if first_reject else (None, None)
            if evaluation is None:
                return ExtensionDecision.skip(
                    reason="outright_no_candidates",
                    metadata={"market_family": "outright"},
                )
            return ExtensionDecision.skip(
                reason=evaluation.reason,
                metadata={
                    "market_family": "outright",
                    "outright_action": evaluation.action.value,
                    "outright_reject_reason": (
                        evaluation.reject_reason.value if evaluation.reject_reason else None
                    ),
                    "outright_accepted": False,
                    "outright_metadata": dict(evaluation.metadata),
                },
            )
        (_edge, evaluation), token_view = best_accept
        # 录单视图：被接受但 permission != AUTO_EXECUTE，返回 SKIP 携带 record 元数据。
        if evaluation.action.value != "auto_execute":
            return ExtensionDecision.skip(
                reason=f"outright_{evaluation.action.value}",
                metadata={
                    "market_family": "outright",
                    "outright_action": evaluation.action.value,
                    "outright_accepted": True,
                    "outright_metadata": dict(evaluation.metadata),
                    "budget_unlocked": budget_unlocked,
                },
            )
        # AUTO_EXECUTE：构造 BUY。预算/合约级 RiskManager 仍是最终门禁；本函数只生成 intent。
        # 优先从 kelly_plan 分配结果读金额；fallback 到 config 硬上限（兼容 sizing 降级路径）。
        allocation = context.allocation
        if allocation is not None and allocation.buy_budget_usdc > Decimal("0"):
            proposed_amount = allocation.buy_budget_usdc
        else:
            proposed_amount = min(config.tail_outright_budget_usdc, config.tail_outright_max_per_market_usdc)
        if proposed_amount <= Decimal("0"):
            return ExtensionDecision.skip(
                reason="outright_budget_exhausted",
                metadata={"market_family": "outright"},
            )
        # outright 风控前置（horizon / 相关性 / per-market / total）。框架 RiskManager
        # 是最终门禁，这里负责给出 outright-specific 拒绝原因便于审计。
        market_end_at = market.end_date
        # 已有敞口可能由 framework 通过 context.metadata 透出；缺失视为 0。
        ctx_meta = context.metadata or {}
        existing_outright_exposure = _decimal_from_metadata(ctx_meta.get("outright_total_exposure_usdc")) or Decimal("0")
        existing_event_exposure = _decimal_from_metadata(ctx_meta.get("outright_event_exposure_usdc")) or Decimal("0")
        risk_reject = check_outright_entry_risk(
            now=now,
            market_end_at=market_end_at,
            proposed_amount_usdc=proposed_amount,
            existing_outright_exposure_usdc=existing_outright_exposure,
            existing_event_exposure_usdc=existing_event_exposure,
            max_per_market_usdc=config.tail_outright_max_per_market_usdc,
            max_event_correlation_usdc=config.tail_outright_max_event_correlation_usdc,
            max_total_outright_usdc=config.tail_outright_budget_usdc,
            max_hold_horizon_days=config.tail_outright_max_hold_horizon_days,
            min_remaining_days=config.tail_outright_min_remaining_days,
        )
        if risk_reject is not None:
            return ExtensionDecision.skip(
                reason=f"outright_{risk_reject.value}",
                metadata={
                    "market_family": "outright",
                    "outright_reject_reason": risk_reject.value,
                    "outright_metadata": dict(evaluation.metadata),
                },
            )
        return ExtensionDecision.buy(
            reason=evaluation.reason,
            token_id=evaluation.candidate.token_id if evaluation.candidate else None,
            price=evaluation.entry_price_cap or OUTRIGHT_FALLBACK_ENTRY_PRICE,
            amount_usdc=proposed_amount,
            market_slug=market.market_slug,
            decision_kind=DecisionKind.ENTRY,
            metadata={
                "market_family": "outright",
                "outright_metadata": dict(evaluation.metadata),
                "outright_fair_value": str(evaluation.fair_value) if evaluation.fair_value else None,
                "outright_exit_price_target": (
                    str(evaluation.exit_price_target) if evaluation.exit_price_target else None
                ),
            },
        )

    def _decide_series_entry(self, context: ExtensionContext) -> ExtensionDecision:
        """Series 子包驱动的入场决策。

        按 sub_type（WINNER / TOTAL_GAMES / GAME_HANDICAP）选取对应风控 + 定价
        配置；accepted 路径走 Kelly 分配 + check_series_entry_risk → BUY，最终
        仍由框架 RiskManager 与 OrderExecutor 把关；reject 路径返回带可审计
        原因的 SKIP。
        """

        market = context.market
        if market is None:
            return ExtensionDecision.skip(reason="series_missing_market")
        token_views = tuple(context.market_token_views or ())
        outcomes = market.outcomes
        if not token_views and not outcomes:
            return ExtensionDecision.skip(
                reason="series_missing_outcomes",
                metadata={"market_family": "series"},
            )
        if not token_views:
            token_views = tuple(
                MarketTokenView(token_id=o.token_id, outcome=o.outcome)
                for o in outcomes
            )

        sub_type, settings = self._series_subtype_settings(market)
        permission = settings.execution_permission
        budget_unlocked = settings.budget_usdc > Decimal("0")
        auto_enabled = budget_unlocked and permission.value == "auto_execute"
        now = context.now or datetime.now(timezone.utc)
        ctx_meta = context.metadata or {}

        best_accept: tuple[tuple[Decimal, SeriesEvaluation], MarketTokenView] | None = None
        first_reject: tuple[SeriesEvaluation, MarketTokenView] | None = None
        for token_view in token_views:
            orderbook = token_view.orderbook
            best_ask = orderbook.best_ask if orderbook is not None else None
            buyable = (
                orderbook.buyable_ask_depth(max_price=settings.max_entry_price)
                if orderbook is not None
                else Decimal("0")
            )
            buyable_usdc = buyable * best_ask if best_ask is not None else Decimal("0")
            inputs = SeriesEvaluatorInputs(
                best_ask=best_ask,
                buyable_liquidity_usdc=buyable_usdc,
                now=now,
                min_edge_bps=settings.min_edge_bps,
                max_entry_price=settings.max_entry_price,
                min_orderbook_depth_usdc=settings.min_orderbook_depth_usdc,
                max_series_state_age_seconds=settings.max_state_age_seconds,
                max_game_odds_age_seconds=settings.max_game_odds_age_seconds,
                season_snapshot=None,
            )
            # candidate.metadata 必须挂上 context.metadata，evaluator 在那里读取
            # SeriesState / GameOdds / GameSpreads 等 worker 写入的快照。
            candidate = SeriesCandidate(
                market=market,
                outcome_label=token_view.outcome,
                token_id=token_view.token_id,
                metadata=ctx_meta,
            )
            evaluation = evaluate_series_opportunity(candidate, inputs=inputs)
            if evaluation.accepted:
                edge = (evaluation.fair_value or Decimal(0)) - (best_ask or Decimal(0))
                if best_accept is None or edge > best_accept[0][0]:
                    best_accept = ((edge, evaluation), token_view)
            elif first_reject is None:
                first_reject = (evaluation, token_view)

        if best_accept is None:
            evaluation, token_view = first_reject if first_reject else (None, None)
            if evaluation is None or token_view is None:
                return ExtensionDecision.skip(
                    reason="series_no_candidates",
                    metadata={"market_family": "series", "series_sub_type": sub_type.value},
                )
            return ExtensionDecision.skip(
                reason=evaluation.reject_reason.value if evaluation.reject_reason else "series_unclassified",
                metadata={
                    "market_family": "series",
                    "series_sub_type": evaluation.sub_type.value,
                    "series_reject_reason": (
                        evaluation.reject_reason.value if evaluation.reject_reason else None
                    ),
                    "series_accepted": False,
                    "series_metadata": dict(evaluation.metadata),
                    "token_id": token_view.token_id,
                    "outcome_label": token_view.outcome,
                },
            )

        (_edge, evaluation), token_view = best_accept
        if not auto_enabled:
            # accepted 但 permission/budget 未解锁 → RECORD_ONLY 视图，下游不构造 BUY。
            return ExtensionDecision.skip(
                reason=f"series_record_only_{permission.value}",
                metadata={
                    "market_family": "series",
                    "series_sub_type": evaluation.sub_type.value,
                    "series_accepted": True,
                    "series_metadata": dict(evaluation.metadata),
                    "budget_unlocked": budget_unlocked,
                    "execution_permission": permission.value,
                    "token_id": token_view.token_id,
                    "outcome_label": token_view.outcome,
                },
            )

        # AUTO_EXECUTE：先做策略侧 risk 前置（暴露细粒度审计），框架 RiskManager
        # 仍是最终门禁——这里只产 BUY intent，不绕过任何上层风控。
        allocation = context.allocation
        if allocation is not None and allocation.buy_budget_usdc > Decimal("0"):
            proposed_amount = allocation.buy_budget_usdc
        else:
            proposed_amount = min(settings.budget_usdc, settings.max_per_market_usdc)
        if proposed_amount <= Decimal("0"):
            return ExtensionDecision.skip(
                reason="series_budget_exhausted",
                metadata={"market_family": "series", "series_sub_type": sub_type.value},
            )

        existing_series_exposure = (
            _decimal_from_metadata(ctx_meta.get("series_total_exposure_usdc")) or Decimal("0")
        )
        existing_event_exposure = (
            _decimal_from_metadata(ctx_meta.get("series_event_exposure_usdc")) or Decimal("0")
        )
        risk_reject = check_series_entry_risk(
            now=now,
            market_end_at=market.end_date,
            proposed_amount_usdc=proposed_amount,
            existing_series_exposure_usdc=existing_series_exposure,
            existing_event_exposure_usdc=existing_event_exposure,
            config=SeriesSubTypeRiskConfig(
                max_per_market_usdc=settings.max_per_market_usdc,
                max_event_correlation_usdc=settings.max_event_correlation_usdc,
                max_total_series_usdc=settings.budget_usdc,
                max_hold_horizon_days=settings.max_hold_horizon_days,
                min_remaining_days=settings.min_remaining_days,
            ),
        )
        if risk_reject is not None:
            return ExtensionDecision.skip(
                reason=f"series_{risk_reject.value}",
                metadata={
                    "market_family": "series",
                    "series_sub_type": sub_type.value,
                    "series_reject_reason": risk_reject.value,
                    "series_metadata": dict(evaluation.metadata),
                },
            )

        entry_price = _series_entry_price(evaluation, fallback=settings.max_entry_price)
        return ExtensionDecision.buy(
            reason=f"series_{sub_type.value}_entry_accepted",
            token_id=evaluation.candidate.token_id,
            price=entry_price,
            amount_usdc=proposed_amount,
            market_slug=market.market_slug,
            decision_kind=DecisionKind.ENTRY,
            metadata={
                "market_family": "series",
                "series_sub_type": evaluation.sub_type.value,
                "series_metadata": dict(evaluation.metadata),
                "series_fair_value": str(evaluation.fair_value) if evaluation.fair_value else None,
            },
        )

    def _series_subtype_settings(self, market: Market) -> tuple[SeriesSubType, _SeriesSubTypeSettings]:
        """根据 market 的子类型分类返回对应配置。

        OTHER 子类型也走 winner 配置（evaluator 会直接 SUBTYPE_UNCLASSIFIED 拒绝，
        不会真正消费配置）；这样 sizing / decide 共享一份 fallback 配置。
        """

        from strategies.current.series.classifier import classify_series_sub_type

        sub_type = classify_series_sub_type(market)
        config = self._config
        if sub_type == SeriesSubType.TOTAL_GAMES:
            return sub_type, _SeriesSubTypeSettings(
                execution_permission=config.tail_series_total_games_execution_permission,
                min_edge_bps=config.tail_series_total_games_min_edge_bps,
                max_entry_price=config.tail_series_total_games_max_entry_price,
                budget_usdc=config.tail_series_total_games_budget_usdc,
                max_per_market_usdc=config.tail_series_total_games_max_per_market_usdc,
                max_event_correlation_usdc=config.tail_series_total_games_max_event_correlation_usdc,
                min_orderbook_depth_usdc=config.tail_series_total_games_min_orderbook_depth_usdc,
                max_state_age_seconds=config.tail_series_winner_max_state_age_seconds,
                max_game_odds_age_seconds=config.tail_series_winner_max_game_odds_age_seconds,
                max_hold_horizon_days=config.tail_series_total_games_max_hold_horizon_days,
                min_remaining_days=config.tail_series_total_games_min_remaining_days,
            )
        if sub_type == SeriesSubType.GAME_HANDICAP:
            return sub_type, _SeriesSubTypeSettings(
                execution_permission=config.tail_series_handicap_execution_permission,
                min_edge_bps=config.tail_series_handicap_min_edge_bps,
                max_entry_price=config.tail_series_handicap_max_entry_price,
                budget_usdc=config.tail_series_handicap_budget_usdc,
                max_per_market_usdc=config.tail_series_handicap_max_per_market_usdc,
                max_event_correlation_usdc=config.tail_series_handicap_max_event_correlation_usdc,
                min_orderbook_depth_usdc=config.tail_series_handicap_min_orderbook_depth_usdc,
                max_state_age_seconds=config.tail_series_winner_max_state_age_seconds,
                max_game_odds_age_seconds=config.tail_series_winner_max_game_odds_age_seconds,
                max_hold_horizon_days=config.tail_series_handicap_max_hold_horizon_days,
                min_remaining_days=config.tail_series_handicap_min_remaining_days,
            )
        # WINNER 与 OTHER 共用 winner 配置：OTHER 在 evaluator 立即 SUBTYPE_UNCLASSIFIED
        # 拒绝，不会真正下注；用 winner 包络只是为了让 sizing 接口不空转。
        sub_type = SeriesSubType.WINNER if sub_type == SeriesSubType.OTHER else sub_type
        return sub_type, _SeriesSubTypeSettings(
            execution_permission=config.tail_series_winner_execution_permission,
            min_edge_bps=config.tail_series_winner_min_edge_bps,
            max_entry_price=config.tail_series_winner_max_entry_price,
            budget_usdc=config.tail_series_winner_budget_usdc,
            max_per_market_usdc=config.tail_series_winner_max_per_market_usdc,
            max_event_correlation_usdc=config.tail_series_winner_max_event_correlation_usdc,
            min_orderbook_depth_usdc=config.tail_series_winner_min_orderbook_depth_usdc,
            max_state_age_seconds=config.tail_series_winner_max_state_age_seconds,
            max_game_odds_age_seconds=config.tail_series_winner_max_game_odds_age_seconds,
            max_hold_horizon_days=config.tail_series_winner_max_hold_horizon_days,
            min_remaining_days=config.tail_series_winner_min_remaining_days,
        )

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        """根据持仓状态生成 SELL 决策。"""

        with active_ports_scope(self._ports):
            return _enrich_decision(decide_exit(self._config, context), default_kind=DecisionKind.EXIT)

    async def _on_live_state_no_feasible_source(self, envelope: _LifecycleEnvelope) -> None:
        """Lifecycle 回调：worker 报出"全源不可用"后，缓存为 True；后续可在重新可用
        时由 LIVE_STATE_UPDATED 任意成功事件清回 False。"""

        statuses = envelope.payload.get("source_statuses") or ()
        no_feasible = True
        for status in statuses:
            health = status.get("health") if isinstance(status, dict) else None
            if health in {"success_with_live_data", "cached"}:
                no_feasible = False
                break
        self._live_state_no_feasible_source = no_feasible

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        """根据热状态生成恢复语义。

        额外路径：当 worker 已订阅到全源不可用信号时，对没有 live_game 但属于
        single_game 家族的市场，主动暂停交易而不是默默放行。
        """

        if self._live_state_no_feasible_source:
            descriptor = describe_sports_market(context.market) if context.market else None
            family = descriptor.market_family.value if descriptor is not None else None
            if family == "single_game":
                return RecoveryDecision(
                    reason="sports_live_state_no_source",
                    actions=(),
                    pause_trading=True,
                    pause_reason="sports_live_state_no_source",
                )
        recovery = decide_recovery(self._config, context)
        if not recovery.actions:
            return recovery
        enriched = tuple(_enrich_decision(action, default_kind=DecisionKind.RECOVERY) for action in recovery.actions)
        return RecoveryDecision(
            reason=recovery.reason,
            actions=enriched,
            pause_trading=recovery.pause_trading,
            pause_reason=recovery.pause_reason,
        )

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        """根据成交结果生成后续动作。"""

        if context.order_result is None:
            return ()
        if context.order_result.side is None or context.order_result.side.value != "BUY":
            return ()
        if context.order_result.matched_shares <= 0:
            return ()
        intent_metadata = _order_intent_metadata(context.order_result.intent)
        if _has_profit_take_follow_up(intent_metadata):
            target_price = _decimal_from_metadata(intent_metadata.get("profit_take_target_price"))
            if target_price is None:
                return ()
            target_price = cap_price_to_clob_limit(target_price, tick_size=_effective_tick_size(context))
            exit_metadata = build_exit_plan_metadata(
                self._config,
                context,
                token_id=context.order_result.token_id,
                source_reason="profit_take_after_buy_fill",
                target_size_shares=context.order_result.matched_shares,
            )
            exit_metadata.update(intent_metadata)
            exit_plan = exit_metadata.get("exit_plan")
            if isinstance(exit_plan, dict):
                exit_plan["target_exit_price"] = str(target_price)
                exit_plan["primary_action"] = "place_profit_take_gtc_sell_after_buy_fill"
                exit_plan["settlement_rule"] = "keep_profit_take_order_until_fill_or_authoritative_resolution"
                exit_plan["recovery_rule"] = "cancel_open_entry_orders_and_cover_profit_take_positions"
            exit_metadata["exit_target_price"] = str(target_price)
            exit_metadata["exit_source_reason"] = "profit_take_after_buy_fill"
            return (
                _enrich_decision(
                    ExtensionDecision.sell(
                        reason="strategy_profit_take",
                        token_id=context.order_result.token_id,
                        price=target_price,
                        size_shares=context.order_result.matched_shares,
                        market_slug=context.order_result.market_slug or (
                            context.market.market_slug if context.market is not None else None
                        ),
                        metadata=exit_metadata,
                    ),
                    default_kind=DecisionKind.FOLLOW_UP,
                ),
            )
        if not self._config.auto_exit_enabled:
            return ()
        return (
            _enrich_decision(
                ExtensionDecision.sell(
                    reason="strategy_exit",
                    token_id=context.order_result.token_id,
                    price=exit_price_for_context(self._config, context),
                    size_shares=context.order_result.matched_shares,
                    market_slug=context.order_result.market_slug or (
                        context.market.market_slug if context.market is not None else None
                    ),
                    metadata=build_exit_plan_metadata(
                        self._config,
                        context,
                        token_id=context.order_result.token_id,
                        source_reason="follow_up_after_buy_fill",
                        target_size_shares=context.order_result.matched_shares,
                    ),
                ),
                default_kind=DecisionKind.FOLLOW_UP,
            ),
        )

    def should_keep_tracking(
        self,
        market: Market,
        account_snapshot: AccountSnapshotView | None,
    ) -> bool:
        """判断已被过滤 market 是否继续保留跟踪。"""

        return should_keep_tracking(market, account_snapshot)

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        """构造“继续跟踪但已被排除”的 market 运行时状态。"""

        return build_filtered_tracking_market(
            candidate_market,
            existing_market=existing_market,
            reason=reason,
        )

    def match_live_state(
        self,
        market: Market,
        events: tuple[LiveEvent, ...],
    ) -> "LiveStateMatch | None":
        """将外部直播事件集合匹配成 framework 可消费的 LiveStateMatch。"""

        descriptor = describe_sports_market(market)
        if not descriptor.accepted or descriptor.market_family.value != "single_game":
            return None
        candidate_events = self._candidate_live_events_for_market(market, events)

        def _bypass(matched_market, event):
            return _market_tail_window_bypass_reason(matched_market, event, descriptor, self._config)

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
        filtered = _candidate_live_events_for_market(market, events)
        self._live_event_filter_cache[cache_key] = filtered
        return filtered


def build_strategy(
    *,
    ports: ExtensionPorts | None = None,
    config_path: str | None = None,
) -> BusinessExtension:
    """构造当前策略实例。

    参数：
        ports:
            框架注入的应用层端口集合。
        config_path:
            可选外部配置文件路径。未提供时使用默认配置。

    返回：
        一个符合 ``BusinessExtension`` 协议的当前策略实例。
    """

    return CurrentStrategy(
        config=load_current_strategy_config(config_path),
        ports=ports,
    )


def _order_intent_metadata(intent: ManagedOrderIntent | None) -> Mapping[str, Any]:
    """读取成交来源 intent 上的透传 metadata。"""

    if intent is None:
        return {}
    return intent.metadata


def _decimal_from_metadata(value: object) -> Decimal | None:
    """把 metadata 中的价格文本转换为 Decimal。"""

    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _resolve_outright_reject_label(decision: ExtensionDecision) -> str:
    """从 SKIP 决策的 metadata / reason 还原 metric 用的拒绝原因 label。

    优先读 ``outright_reject_reason`` 强类型字段（来自 ``OutrightRejectReason``
    StrEnum）；缺失时落到 ``decision.reason``。fallback ``unspecified`` 仅在
    完全没有结构化原因时出现，便于线上 grep "为什么没被分类"。
    """

    metadata = decision.metadata or {}
    reason = metadata.get("outright_reject_reason")
    if isinstance(reason, str) and reason:
        return reason
    if decision.reason:
        return decision.reason
    return "unspecified"


def _resolve_series_sub_type_label(decision: ExtensionDecision) -> str:
    """从决策 metadata 取 series sub_type；缺失时记 ``unknown``。"""

    metadata = decision.metadata or {}
    sub_type = metadata.get("series_sub_type")
    if isinstance(sub_type, str) and sub_type:
        return sub_type
    return "unknown"


def _resolve_series_reject_label(decision: ExtensionDecision) -> str:
    """同 _resolve_outright_reject_label，但适配 series 决策的 metadata key。"""

    metadata = decision.metadata or {}
    reason = metadata.get("series_reject_reason")
    if isinstance(reason, str) and reason:
        return reason
    if decision.reason:
        return decision.reason
    return "unspecified"


def _series_entry_price(evaluation: SeriesEvaluation, *, fallback: Decimal) -> Decimal:
    """从 series evaluation metadata 还原 entry_price_cap，缺失时退到配置 fallback。"""

    cap_text = evaluation.metadata.get("entry_price_cap") if evaluation.metadata else None
    cap = _decimal_from_metadata(cap_text)
    if cap is not None and cap > Decimal("0"):
        return cap
    return fallback


def _effective_tick_size(context: ExtensionContext) -> Decimal | None:
    if context.orderbook is not None and context.orderbook.tick_size is not None:
        return context.orderbook.tick_size
    if context.market is not None:
        return context.market.tick_size
    return None


def _has_profit_take_follow_up(metadata: Mapping[str, object]) -> bool:
    """判断 BUY 成交后是否需要补挂主动止盈单。"""

    return metadata.get("exit_mode") == "profit_take" or bool(
        metadata.get("profit_take_overlay_enabled")
    )


def _enrich_decision(decision: ExtensionDecision, *, default_kind: DecisionKind) -> ExtensionDecision:
    """把策略私有 metadata 投影成 framework 中性的 ``StrategySummary``、
    ``decision_kind`` 与 ``intent_tags``，让 framework / admin / 复盘只读这些强类型字段。

    策略 metadata 仍然被保留（透传 audit 用），但 framework 不再按 metadata key 名读。
    """

    metadata = dict(decision.metadata or {})
    is_scale_in = metadata.get("opportunity_type") == "scale_in_advantage"
    decision_kind = decision.decision_kind if decision.decision_kind is not None else (
        DecisionKind.SCALE_IN if is_scale_in else default_kind
    )
    intent_tags = decision.intent_tags if decision.intent_tags else (
        frozenset({"scale_in"}) if is_scale_in else frozenset()
    )
    summary = decision.summary if decision.summary is not None else _build_strategy_summary(metadata)
    return replace(
        decision,
        decision_kind=decision_kind,
        intent_tags=intent_tags,
        summary=summary,
    )


def _build_strategy_summary(metadata: Mapping[str, Any]) -> StrategySummary:
    """从策略写入的 metadata 投影出 framework 展示用的 StrategySummary。"""

    live_game = (
        metadata.get("live_game") if isinstance(metadata.get("live_game"), Mapping) else {}
    )
    home = live_game.get("home_name") or ""
    away = live_game.get("away_name") or ""
    period = live_game.get("period") or ""
    label_parts = [str(part).strip() for part in (home, "vs" if home and away else "", away, period) if str(part).strip()]
    label = " ".join(label_parts)
    return StrategySummary(
        action=str(metadata.get("tail_action") or ""),
        reason=str(metadata.get("tail_reason") or ""),
        label=label,
        market_type=str(metadata.get("market_type") or ""),
        side=str(metadata.get("side") or ""),
        line=_decimal_from_metadata(metadata.get("line")),
        best_ask=_decimal_from_metadata(metadata.get("best_ask")),
        observed_at=None,
        manual_confirmed=bool(metadata.get("manual_confirmed")),
        confirmed_by=str(metadata.get("confirmed_by") or ""),
        confirm_reason=str(metadata.get("confirm_reason") or ""),
        extras={
            "league": live_game.get("league"),
            "home_name": live_game.get("home_name"),
            "away_name": live_game.get("away_name"),
            "period": live_game.get("period"),
            "observed_at": live_game.get("observed_at"),
            "game_status": metadata.get("game_status") or live_game.get("status"),
            "total_score": metadata.get("total_score"),
            "seconds_remaining": metadata.get("seconds_remaining"),
            "execution_permission": metadata.get("execution_permission"),
            "market_family": metadata.get("market_family"),
            "risk_reason": metadata.get("risk_reason"),
            "exit_plan": metadata.get("exit_plan"),
        },
    )


def _market_tail_window_bypass_reason(
    market: Market,
    event: LiveEvent,
    descriptor: SportsMarketDescriptor,
    config: CurrentStrategyConfig,
) -> str | None:
    """返回可绕过远期 endDate 粗筛的直播尾盘原因。"""

    if _market_can_lock_before_tail_window(market, event, descriptor):
        return "live_outcome_lock_candidate"
    if _market_has_live_tail_state(market, event, descriptor, config):
        return "live_tail_state_candidate"
    return None


def _market_can_lock_before_tail_window(market: Market, event: LiveEvent, descriptor: SportsMarketDescriptor) -> bool:
    """判断 market 是否存在不依赖比赛封盘时间的数学锁定机会。"""

    status = event.status.value.lower()
    if status != "live":
        return False
    market_type = descriptor.market_type
    if market_type == "totals":
        return _totals_market_is_already_over(market, event, descriptor)
    if market_type != "moneyline" or not _is_tennis_set_winner_market(market):
        return False
    state = event.tennis_state
    if state is None:
        return False
    set_number = _tennis_set_winner_number(market)
    current_set = state.current_set
    return set_number is not None and current_set is not None and current_set > set_number and bool(state.set_scores)


def _market_has_live_tail_state(
    market: Market,
    event: LiveEvent,
    descriptor: SportsMarketDescriptor,
    config: CurrentStrategyConfig,
) -> bool:
    """判断直播状态是否已达到策略尾盘条件，但结果尚未完全数学锁定。"""

    status = event.status.value.lower()
    if status != "live":
        return False
    market_type = descriptor.market_type
    state = event.tennis_state
    if state is not None:
        if market_type == "moneyline":
            if _is_tennis_set_winner_market(market):
                return _tennis_set_winner_tail_state_reached(market, state)
            return _tennis_moneyline_tail_state_reached(state)
        return False
    baseball_state = event.baseball_state
    if baseball_state is not None:
        return _baseball_tail_state_reached(baseball_state)
    if market_type == "moneyline":
        seconds_remaining = _int_value(event.seconds_remaining)
        if seconds_remaining is None:
            return False
        home = event.home
        away = event.away
        if home is None or away is None:
            return False
        return (
            seconds_remaining <= config.tail_max_moneyline_seconds_remaining
            and abs((home.score or 0) - (away.score or 0)) >= config.tail_min_moneyline_lead
        )
    return False


def _baseball_tail_state_reached(state: BaseballGameState) -> bool:
    """用结构化棒球局面判断是否值得触发快速入场重放。"""

    current_inning = _int_value(state.current_inning)
    outs = _int_value(state.outs)
    occupied_bases = tuple(state.occupied_bases or ())
    return current_inning is not None and current_inning >= 9 and outs is not None and outs >= 2 and not occupied_bases


def _tennis_moneyline_tail_state_reached(state: TennisGameState) -> bool:
    home_games = state.home_current_set_games
    away_games = state.away_current_set_games
    if home_games is None or away_games is None:
        return False
    home_sets = state.home_sets_won
    away_sets = state.away_sets_won
    if home_sets is None or away_sets is None:
        return False
    if home_games >= 5 and home_games - away_games >= 2 and home_sets - away_sets >= 1:
        return True
    return away_games >= 5 and away_games - home_games >= 2 and away_sets - home_sets >= 1


def _tennis_set_winner_tail_state_reached(market: Market, state: TennisGameState) -> bool:
    set_number = _tennis_set_winner_number(market)
    current_set = state.current_set
    if set_number is None or current_set != set_number:
        return False
    home_games = state.home_current_set_games
    away_games = state.away_current_set_games
    if home_games is None or away_games is None:
        return False
    return max(home_games, away_games) >= 5 and abs(home_games - away_games) >= 2


def _totals_market_is_already_over(market: Market, event: LiveEvent, descriptor: SportsMarketDescriptor) -> bool:
    line = descriptor.line
    if line is None:
        return False
    state = event.tennis_state
    if state is not None:
        if _is_set_total_market_slug(market.market_slug):
            current_set = state.current_set
            return current_set is not None and current_set > line
        return state.total_games > line or _tennis_match_total_min_final_games_is_over(state, line)
    home = event.home
    away = event.away
    if home is None or away is None:
        return False
    return ((home.score or 0) + (away.score or 0)) > line


def _is_tennis_set_winner_market(market: Market) -> bool:
    text = _normalized_market_slug(market)
    return "set winner" in text or "first set winner" in text


def _tennis_set_winner_number(market: Market) -> int | None:
    text = _normalized_market_slug(market)
    if "first set winner" in text or "1st set winner" in text or "set 1 winner" in text:
        return 1
    if "second set winner" in text or "2nd set winner" in text or "set 2 winner" in text:
        return 2
    if "third set winner" in text or "3rd set winner" in text or "set 3 winner" in text:
        return 3
    return None


def _is_set_total_market_slug(slug: str) -> bool:
    text = slug.strip().lower().replace("_", " ").replace("-", " ")
    return "set total" in text or "set totals" in text or "total sets" in text


def _tennis_match_total_min_final_games_is_over(state: TennisGameState, line: object) -> bool:
    """判断当前网球局面下，整场最低可能最终总局数是否已越过 totals 线。"""

    current_home = state.home_current_set_games
    current_away = state.away_current_set_games
    if current_home is None or current_away is None:
        return False
    minimum_current_set_games = _tennis_minimum_final_set_games(current_home, current_away)
    if minimum_current_set_games is None:
        return False
    current_games = current_home + current_away
    already_counted_total = state.home_total_games + state.away_total_games
    minimum_final_games = already_counted_total - current_games + minimum_current_set_games
    return minimum_final_games > line


def _tennis_minimum_final_set_games(home_games: int, away_games: int) -> int | None:
    if home_games < 0 or away_games < 0:
        return None
    if _tennis_set_score_is_final(home_games, away_games):
        return home_games + away_games
    for extra_games in range(0, 8):
        for home_extra in range(extra_games + 1):
            away_extra = extra_games - home_extra
            final_home = home_games + home_extra
            final_away = away_games + away_extra
            if _tennis_set_score_is_final(final_home, final_away):
                return final_home + final_away
    return None


def _tennis_set_score_is_final(home_games: int, away_games: int) -> bool:
    winner_games = max(home_games, away_games)
    loser_games = min(home_games, away_games)
    return (winner_games >= 6 and winner_games - loser_games >= 2) or winner_games == 7


def _normalized_market_slug(market: Market) -> str:
    return market.market_slug.strip().lower().replace("_", " ").replace("-", " ")


def _candidate_live_events_for_market(
    market: Market,
    events: tuple[LiveEvent, ...],
) -> tuple[LiveEvent, ...]:
    """按运动类型和开赛时间缩小直播匹配候选集。

    全体育覆盖会让 SofaScore 单轮返回数千场比赛。策略 hook 在进入文本匹配前
    做保守过滤，避免每个 market 都执行 markets x events 全量匹配。
    """

    sport_codes = _market_sport_codes(market)
    market_start = _ensure_utc(market.game_start_time)
    filtered: list[LiveEvent] = []
    for event in events:
        if sport_codes and (event_sport := _event_sport_code(event)) is not None and event_sport not in sport_codes:
            continue
        if market_start is not None and not _event_start_is_near_market_start(event, market_start):
            continue
        filtered.append(event)
    return tuple(filtered)


def _market_sport_codes(market: Market) -> set[str]:
    text = _normalized_market_text(market)
    mapping = (
        ("table-tennis", ("table tennis", "table-tennis", "wtt", "world team championships")),
        ("baseball", ("mlb", "kbo", "baseball")),
        ("tennis", ("atp", "wta")),
        ("basketball", ("nba", "wnba", "ncaamb", "ncaawb", "basketball")),
        ("ice-hockey", ("nhl", "ahl", "hockey", "ice hockey")),
        ("american-football", ("nfl", "ncaaf", "american football")),
        ("football", ("soccer", "football", "mls", "nwsl", "epl")),
    )
    return {sport for sport, tokens in mapping if any(f" {token} " in f" {text} " for token in tokens)}


def _event_sport_code(event: LiveEvent) -> str | None:
    # 优先用 LiveEvent.sport 一等字段；payload fallback 仅在历史快照里兼容。
    sport = str(event.sport or event.source_payload.get("sport") or "").strip().lower()
    if sport:
        return _normalize_sport_code(sport)
    source = str(event.source or "").strip().lower()
    league = str(event.league or "").strip().lower()
    text = f"{source} {league}"
    if "mlb" in text:
        return "baseball"
    if "nba" in text or "basketball" in text:
        return "basketball"
    if "nhl" in text or "hockey" in text:
        return "ice-hockey"
    if "table-tennis" in text or "table tennis" in text or "wtt" in text:
        return "table-tennis"
    if "tennis" in text or "atp" in text or "wta" in text:
        return "tennis"
    if "football" in text or "soccer" in text:
        return "football"
    return None


def _normalize_sport_code(value: str) -> str:
    normalized = value.replace("_", "-").replace(" ", "-")
    if normalized in {"soccer"}:
        return "football"
    if normalized in {"icehockey"}:
        return "ice-hockey"
    if normalized in {"tabletennis"}:
        return "table-tennis"
    return normalized


def _event_start_is_near_market_start(event: LiveEvent, market_start: datetime) -> bool:
    event_start = _event_start_time(event)
    if event_start is None:
        return True
    tolerance = timedelta(hours=24) if _event_sport_code(event) == "tennis" else timedelta(hours=6)
    return abs(event_start - market_start) <= tolerance


def _event_start_time(event: LiveEvent) -> datetime | None:
    # 一等字段优先，fallback 仅为旧 payload 兼容；新 client 都填一等字段。
    if event.event_start_time is not None:
        return _ensure_utc(event.event_start_time)
    for key in ("start_timestamp", "start_time_utc", "game_time_utc", "game_date", "date"):
        parsed = _parse_datetime_value(event.source_payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime_value(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(_timestamp_seconds(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text or re.fullmatch(r"20\d{2}-[01]\d-[0-3]\d", text):
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            return datetime.fromtimestamp(_timestamp_seconds(float(text)), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _timestamp_seconds(value: int | float) -> float:
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timestamp


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalized_market_text(market: Market) -> str:
    return " ".join(
        re.sub(
            r"[^a-z0-9]+",
            " ",
            " ".join(
                part
                for part in (
                    market.market_question,
                    market.market_name,
                    market.market_slug,
                    market.event_title,
                    market.event_slug,
                    market.category,
                    " ".join(market.tags),
                    " ".join(outcome.outcome for outcome in market.outcomes),
                )
                if part
            ).lower(),
        ).split()
    )


def _int_value(value: object) -> int | None:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


# missing_best_ask fallback helper 提到 trading/helpers.py 共享给 tail / outright 两条评估路径。
