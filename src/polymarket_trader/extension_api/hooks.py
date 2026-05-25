from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from polymarket_trader.extension_api.context import AccountSnapshotView, ExtensionContext
from polymarket_trader.extension_api.live_state import SeriesState
from polymarket_trader.extension_api.decisions import (
    EntrySizing,
    ExtensionDecision,
    QuantDecision,
    UniverseDecision,
)
from polymarket_trader.extension_api.discovery import DiscoveryQuery
from polymarket_trader.extension_api.live_state import LiveStateMatch


@runtime_checkable
class ExtensionHooks(Protocol):
    """所有策略必须实现的核心决策契约。

    注意：体育直播 / 比分源消费是可选能力，独立到 ``LiveStateHooks``；这里不强制。
    """

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]: ...

    def select_market(self, market: Market) -> UniverseDecision: ...

    def size_entry(self, context: ExtensionContext) -> EntrySizing: ...

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision: ...

    def quant_decide(self, context: ExtensionContext) -> QuantDecision:
        """量化决策器——入场后所有 WS / 周期触发的决策统一入口。

        ``context.quant_trigger_kind`` 指明本次触发源：
        - ``"orderbook_tick"``：market_ws 盘口事件，主要做 SELL 价跟随
        - ``"order_fill"``：成交事件，主要做跟单（BUY 后挂 GTC SELL）
        - ``"reconcile_cycle"``：周期性扫账户，主要做 recovery（清理僵尸 / 覆盖裸单）

        返回 QuantDecision，actions 可为 0/1/N 个 intent。
        """
        ...

    def should_keep_tracking(
        self,
        market: Market,
        account_snapshot: AccountSnapshotView | None,
    ) -> bool: ...

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market: ...


@runtime_checkable
class MarketClassificationHooks(Protocol):
    """可选：策略实现市场分类 hook，框架通过此接口路由 worker 装配与敞口聚合。

    ``BusinessExtension`` 实现此协议后，main.py 的 worker 工厂和
    ``_portfolio_exposure_metadata`` 不再直接调用策略包具体函数；
    未实现时框架跳过分类 hook，依赖 worker 侧的默认行为（通常等价于
    不过滤 / 不聚合）。
    """

    def is_outright_market(self, market: Market) -> bool:
        """market 是否属于 outright（冠军归属 / 赛季归属）family。"""
        ...

    def is_series_winner_market(self, market: Market) -> bool:
        """market 是否属于系列赛 WINNER 子类型。"""
        ...

    def sport_key_for_season_odds(self, market: Market) -> str | None:
        """market 对应的赛季胜率数据源 sport_key（TheOddsAPI 格式，如 'basketball_nba'）。"""
        ...

    def sport_key_for_series_state(self, market: Market) -> str | None:
        """market 对应的系列赛热态数据源 sport_key（短格式，如 'nba'）。"""
        ...

    def sport_key_for_game_odds(self, market: Market) -> str | None:
        """market 对应的单场赔率数据源 sport_key（TheOddsAPI 格式）。"""
        ...

    def market_family_label(self, market: Market) -> str | None:
        """market 所属 family 的字符串标签（'outright' / 'series' / 'single_game' 等）。

        框架用于聚合持仓敞口时区分 family bucket；未知或不支持时返回 None。
        """
        ...


@runtime_checkable
class SportsDiagnosticHooks(Protocol):
    """可选：策略暴露 admin 诊断 hooks，避免 app 层直接调用策略包纯函数。

    framework 通过 ``isinstance(extension, SportsDiagnosticHooks)`` 检测能力；
    未实现时相关 admin 诊断端点降级为空结果。
    """

    def series_state_from_metadata(
        self, metadata: Mapping[str, Any]
    ) -> SeriesState | None:
        """从 EntryMetadataStore metadata 中解析系列赛状态快照。"""
        ...

    def season_odds_from_metadata(
        self, metadata: Mapping[str, Any]
    ) -> SeasonOddsSnapshot | None:
        """从 EntryMetadataStore metadata 中解析赛季赔率快照。"""
        ...

    def resolve_outright_team_debug_payload(
        self,
        market: Market,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """对 outright market 跑 team resolver trace，返回序列化 payload。

        market 无对应 season_odds 快照时返回 None。
        """
        ...


@runtime_checkable
class LiveStateHooks(Protocol):
    """可选：策略消费 framework 的体育直播状态时实现。

    framework 通过 ``BusinessExtension.live_state_hooks`` 拿到这个对象；返回 None
    表示策略不参与直播驱动的市场发现 / 跟踪，framework 会跳过 ``SportsLiveStateWorker``
    的装配，不强制非体育策略实现这两个 hook。
    """

    @property
    def league_source_affinity(self) -> Mapping[str, tuple[str, ...]] | None:
        """每个 league 的首选直播源顺序；None 表示走 aggregate 内置全局默认表。"""
        ...

    def discovery_queries_for_live_events(
        self, events: tuple[LiveEvent, ...]
    ) -> tuple[DiscoveryQuery, ...]: ...

    def match_live_state(
        self, market: Market, events: tuple[LiveEvent, ...]
    ) -> LiveStateMatch | None: ...
