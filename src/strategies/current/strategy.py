"""当前默认策略的装配入口。

这个文件把 discovery、universe、trading、recovery、tracking 这些子模块
组装成一个完整的 ``BusinessExtension`` 实现。
"""

from __future__ import annotations

from typing import Any, Mapping

from polymarket_trader.extension_api import (
    AccountSnapshotView,
    BusinessExtension,
    DiscoveryQuery,
    EntrySizing,
    ExtensionSpec,
    RecoveryDecision,
    ExtensionContext,
    ExtensionDecision,
    ExtensionPorts,
    UniverseDecision,
)

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import SportsLiveGame

from strategies.current.config import CurrentStrategyConfig, load_current_strategy_config
from strategies.current.discovery import (
    build_configured_discovery_queries,
    build_live_game_discovery_queries,
)
from strategies.current.exit_plan import build_exit_plan_metadata, exit_price_for_context
from strategies.current.live_state import sports_live_metadata_match
from strategies.current.recovery import decide_recovery
from strategies.current.tracking import build_filtered_tracking_market, should_keep_tracking
from strategies.current.trading import decide_entry, decide_exit, size_entry
from strategies.current.universe import select_market


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
        self._spec = ExtensionSpec(
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

    def discovery_queries_for_live_games(
        self,
        games: tuple[SportsLiveGame, ...],
    ) -> tuple[DiscoveryQuery, ...]:
        """用直播源里的真实比赛补充高意图 market discovery 查询。"""

        return build_live_game_discovery_queries(self._config, games)

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        """为当前 market 生成入场预算分配结果。"""

        return size_entry(self._config, context)

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        """根据盘口和预算生成 BUY 决策。"""

        return decide_entry(self._config, context)

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        """根据持仓状态生成 SELL 决策。"""

        return decide_exit(self._config, context)

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        """根据热状态生成恢复语义。"""

        return decide_recovery(self._config, context)

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        """根据成交结果生成后续动作。"""

        if context.order_result is None:
            return ()
        if context.order_result.side is None or context.order_result.side.value != "BUY":
            return ()
        if context.order_result.matched_shares <= 0:
            return ()
        return (
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

    def match_sports_live_state(
        self,
        market: Market,
        games: tuple[SportsLiveGame, ...],
    ) -> tuple[Market, SportsLiveGame, Mapping[str, Any]] | None:
        """将外部直播比赛集合匹配成当前策略可消费的 metadata。"""

        return sports_live_metadata_match(market, games)


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
