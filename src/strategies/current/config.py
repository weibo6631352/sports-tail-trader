"""当前默认策略的配置定义。

这个文件只负责描述“策略自己关心的业务参数”，不负责框架级配置。
二次开发时如果只是替换筛选词、价格阈值、流动性门槛，通常从这里开始改。

远端 discovery 粗筛会调用 Polymarket Gamma Events keyset API：
https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination

能安全前移到粗筛的条件，应当是 Gamma API 原生支持、且即使接口语义波动也不会
破坏本地最终判断的条件，例如标题搜索词和稳定 tag slug。价格、盘口深度、spread、
持仓和挂单状态依赖热态数据，继续留在本地 universe / trading 判断里。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_trader.extension_api import load_extension_config
from strategies.current.sports_tail import (
    ExecutionPermission,
    SportsMarketType,
    SportsTailPolicy,
)


@dataclass(frozen=True, slots=True)
class CurrentStrategyConfig:
    """当前策略的静态配置。

    字段说明：
        entry_no_price_max:
            框架入场 BUY 的兜底最高价格。体育扫尾会优先使用各盘口自己的
            价格上限；这里保留给现有分配和下单接线使用。
        exit_no_price:
            退出时使用的目标挂卖价格。
        min_liquidity_usdc:
            允许入场前要求达到的最小盘口深度，单位是 USDC。
        max_spread:
            允许的最大买一卖一价差；为 ``None`` 表示不限制。
        discovery_title_searches:
            远端 discovery 的标题搜索词。策略会把这些词暴露为
            ``DiscoveryQuery``，框架负责分页、限流和 cursor。
        discovery_tag_slugs:
            可选的远端 discovery tag slug 粗筛。填写后会和
            ``discovery_title_searches`` 组合成 Gamma API 查询参数
            ``tag_slug``。例如 ``("sports",)`` 会请求
            ``/events/keyset?title_search=nba&tag_slug=sports``。
            如果不确定 Gamma tag 是否覆盖目标市场，保持为空，并让
            ``select_market()`` 做本地最终过滤。
        sports_category_tokens:
            本地 universe 精筛时用于识别体育市场的分类 token。
        sports_enabled_market_types:
            体育扫尾允许纳入 universe 的盘口类型。
        sports_*:
            体育扫尾策略自己的价格、流动性、时间窗口和执行权限参数。
    """

    entry_no_price_max: Decimal = Decimal("0.99")
    exit_no_price: Decimal = Decimal("0.995")
    min_liquidity_usdc: Decimal = Decimal("5")
    max_spread: Decimal | None = Decimal("0.10")
    discovery_title_searches: tuple[str, ...] = ("sports", "nba", "nhl", "soccer", "tennis")
    discovery_tag_slugs: tuple[str, ...] = ("sports",)
    sports_category_tokens: tuple[str, ...] = (
        "sports",
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "soccer",
        "tennis",
        "basketball",
        "hockey",
        "football",
    )
    sports_enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
    )
    sports_totals_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    sports_moneyline_execution_permission: ExecutionPermission = ExecutionPermission.MANUAL_CONFIRM
    sports_spreads_execution_permission: ExecutionPermission = ExecutionPermission.ALERT_ONLY
    sports_totals_max_entry_price: Decimal = Decimal("0.99")
    sports_moneyline_max_entry_price: Decimal = Decimal("0.97")
    sports_spreads_max_entry_price: Decimal = Decimal("0.96")
    sports_min_liquidity_usdc: Decimal = Decimal("5")
    sports_max_game_state_age_seconds: int = 10
    sports_max_under_seconds_remaining: int = 30
    sports_max_moneyline_seconds_remaining: int = 180
    sports_max_spreads_seconds_remaining: int = 120
    sports_min_under_safety_margin: Decimal = Decimal("2")
    sports_min_moneyline_lead: int = 6
    sports_min_spread_safety_margin: Decimal = Decimal("2")


def sports_tail_policy_from_config(config: CurrentStrategyConfig) -> SportsTailPolicy:
    """把当前策略配置转换成体育扫尾纯业务策略参数。"""

    return SportsTailPolicy(
        enabled_market_types=config.sports_enabled_market_types,
        totals_execution_permission=config.sports_totals_execution_permission,
        moneyline_execution_permission=config.sports_moneyline_execution_permission,
        spreads_execution_permission=config.sports_spreads_execution_permission,
        totals_max_entry_price=config.sports_totals_max_entry_price,
        moneyline_max_entry_price=config.sports_moneyline_max_entry_price,
        spreads_max_entry_price=config.sports_spreads_max_entry_price,
        min_liquidity_usdc=config.sports_min_liquidity_usdc,
        max_game_state_age_seconds=config.sports_max_game_state_age_seconds,
        max_under_seconds_remaining=config.sports_max_under_seconds_remaining,
        max_moneyline_seconds_remaining=config.sports_max_moneyline_seconds_remaining,
        max_spreads_seconds_remaining=config.sports_max_spreads_seconds_remaining,
        min_under_safety_margin=config.sports_min_under_safety_margin,
        min_moneyline_lead=config.sports_min_moneyline_lead,
        min_spread_safety_margin=config.sports_min_spread_safety_margin,
    )


def default_strategy_config() -> CurrentStrategyConfig:
    """返回内置默认配置。

    返回：
        一份可直接用于生产装配的 ``CurrentStrategyConfig``。
    """

    return CurrentStrategyConfig()


def load_current_strategy_config(config_path: str | None) -> CurrentStrategyConfig:
    """从外部配置文件加载当前策略配置。

    参数：
        config_path:
            外部配置文件路径。支持 ``json`` / ``toml``。如果为 ``None``，
            或者调用方没有提供配置文件，则退回默认配置。

    返回：
        解析后的 ``CurrentStrategyConfig``。
    """

    return load_extension_config(CurrentStrategyConfig, config_path) or default_strategy_config()
