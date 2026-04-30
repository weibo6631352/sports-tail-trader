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
            启用自动退出时使用的目标挂卖价格。当前默认扫尾策略买入后等待
            权威结算，不主动挂 follow-up SELL。
        auto_exit_enabled:
            是否在 BUY 成交或持仓恢复时自动生成 SELL。默认关闭，避免扫尾盘
            在结果确定后为了提前卖出而增加挂单、撤单和流动性风险。
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
        sports_live_discovery_*:
            用外部直播源里的真实比赛队名补充高意图 discovery 查询，避免通用
            ``nba/nhl/mlb`` 搜索长期停留在冠军、系列赛、选秀或电竞市场。
        sports_category_tokens:
            本地 universe 精筛时用于识别已接入直播源的体育联赛 token。
            识别文本以 category/tags 为优先信号，并用 market/event slug、
            问题和标题兜底处理 Gamma 缺失标签的真实赛事。这里不使用泛化的
            ``sports``，避免未建模联赛仅凭大类标签进入自动交易候选。
        sports_enabled_market_types:
            体育扫尾允许纳入 universe 的盘口类型。
        sports_*:
            体育扫尾策略自己的价格、流动性、时间窗口和执行权限参数。
        sports_market_end_horizon_seconds:
            live 市场进入扫尾候选前允许的最长封盘剩余秒数。默认 3600 秒，
            用于把远离封盘的真实直播赛事留在全量发现里，但不进入实时交易候选。
        sports_max_*:
            体育扫尾策略级风险上限，用于约束同一比赛、同一联赛和当日新增
            暴露。框架级 `MAX_ORDER_USDC / MAX_MARKET_USDC / MAX_TOTAL_USDC`
            仍由 RiskManager 做最终门禁。
        sports_min_expected_profit_*:
            入场前按买入价格和买入金额估算等待权威结算的毛利润和资金占用效率。
            如果结算持有收益太低，策略只在可挂出满足最小毛利润的 profit-take
            SELL 时允许买入，否则拒绝这类长时间占用资金的小利润订单。
        sports_profit_take_min_profit_usdc:
            低结算效率订单允许走 profit-take 路径时，目标卖价相对买入价至少需要
            产生的预期毛利润。
        sports_profit_take_hold_minutes:
            估算一档 profit-take 挂单成交前的资金占用时间。小绝对利润订单只有
            按该占用时间折算后的每小时资金效率达标时才允许进入，避免长期挂单
            只赚极小金额。
        sports_entry_maker_max_resting_seconds:
            历史遗留开放 BUY 的最长容忍秒数。自动入场 BUY 只走触发式
            FAK 市价单；该参数只用于恢复链路撤掉旧 GTC BUY，避免长期占用资金。
        sports_settlement_hold_minutes:
            估算等待权威结算的保守资金占用时间。实盘里市场结束到可结算可能跨越
            数小时，因此这里不只看比赛剩余时间。
        sports_baseball_max_game_state_age_seconds:
            MLB 官方结构化局面允许的最大状态年龄。MLB schedule/linescore
            拉取和匹配会批量处理大量市场，不能用通用 10 秒阈值误杀第 9 局
            这类真实尾盘；该放宽只作用于棒球结构化局面。
        sports_mlb_eighth_moneyline_min_lead:
            MLB 第 8 局 moneyline 早期领先机会的最低领先分差。该规则还要求
            至少一出局且二/三垒无得分威胁，避免把普通中局波动提前纳入。
        sports_recovery_profit_take_*:
            恢复侧对历史遗留或买入后缺失止盈挂单的近端仓位补救退出参数。
            默认只在持仓均价较高、无开放 SELL、且挂到下一档 tick 的预期毛利润
            达标时补 profit-take SELL，避免继续长期占用资金。
    """

    entry_no_price_max: Decimal = Decimal("0.99")
    exit_no_price: Decimal = Decimal("0.995")
    auto_exit_enabled: bool = False
    min_liquidity_usdc: Decimal = Decimal("1")
    max_spread: Decimal | None = Decimal("0.10")
    discovery_title_searches: tuple[str, ...] = ("nba", "nhl", "nfl", "mlb", "tennis", "atp", "wta")
    discovery_tag_slugs: tuple[str, ...] = ("sports",)
    sports_live_discovery_max_games: int = 40
    sports_live_discovery_max_queries: int = 120
    sports_category_tokens: tuple[str, ...] = (
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "kbo",
        "basketball",
        "baseball",
        "korean baseball",
        "hockey",
        "football",
        "soccer",
        "table tennis",
        "table-tennis",
        "wtt",
        "tennis",
        "atp",
        "wta",
    )
    sports_enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
        SportsMarketType.BINARY_PROP,
    )
    sports_totals_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    sports_moneyline_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    sports_spreads_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    sports_totals_max_entry_price: Decimal = Decimal("0.99")
    sports_moneyline_max_entry_price: Decimal = Decimal("0.97")
    sports_tennis_locked_moneyline_max_entry_price: Decimal = Decimal("0.995")
    sports_spreads_max_entry_price: Decimal = Decimal("0.96")
    sports_min_liquidity_usdc: Decimal = Decimal("1")
    sports_max_game_state_age_seconds: int = 10
    sports_baseball_max_game_state_age_seconds: int = 45
    sports_tennis_max_game_state_age_seconds: int = 35
    sports_market_end_horizon_seconds: int = 3600
    sports_max_under_seconds_remaining: int = 30
    sports_max_moneyline_seconds_remaining: int = 180
    sports_max_spreads_seconds_remaining: int = 120
    sports_min_under_safety_margin: Decimal = Decimal("2")
    sports_min_moneyline_lead: int = 6
    sports_mlb_eighth_moneyline_min_lead: int = 2
    sports_min_spread_safety_margin: Decimal = Decimal("2")
    sports_max_event_exposure_usdc: Decimal = Decimal("25")
    sports_max_league_exposure_usdc: Decimal = Decimal("75")
    sports_max_daily_entry_usdc: Decimal = Decimal("150")
    sports_max_consecutive_losses: int = 3
    sports_scale_in_budget_fraction: Decimal = Decimal("0.5")
    sports_scale_in_max_buy_fills: int = 2
    sports_min_expected_profit_usdc: Decimal = Decimal("0.03")
    sports_min_expected_profit_per_hour_usdc: Decimal = Decimal("0.10")
    sports_profit_take_min_profit_usdc: Decimal = Decimal("0.02")
    sports_profit_take_hold_minutes: int = 2
    sports_entry_maker_max_resting_seconds: int = 60
    sports_settlement_hold_minutes: int = 180
    sports_recovery_profit_take_enabled: bool = True
    sports_recovery_profit_take_min_avg_price: Decimal = Decimal("0.90")


def sports_tail_policy_from_config(config: CurrentStrategyConfig) -> SportsTailPolicy:
    """把当前策略配置转换成体育扫尾纯业务策略参数。"""

    return SportsTailPolicy(
        enabled_market_types=config.sports_enabled_market_types,
        totals_execution_permission=config.sports_totals_execution_permission,
        moneyline_execution_permission=config.sports_moneyline_execution_permission,
        spreads_execution_permission=config.sports_spreads_execution_permission,
        totals_max_entry_price=config.sports_totals_max_entry_price,
        moneyline_max_entry_price=config.sports_moneyline_max_entry_price,
        tennis_locked_moneyline_max_entry_price=config.sports_tennis_locked_moneyline_max_entry_price,
        spreads_max_entry_price=config.sports_spreads_max_entry_price,
        min_liquidity_usdc=config.sports_min_liquidity_usdc,
        max_game_state_age_seconds=config.sports_max_game_state_age_seconds,
        baseball_max_game_state_age_seconds=config.sports_baseball_max_game_state_age_seconds,
        tennis_max_game_state_age_seconds=config.sports_tennis_max_game_state_age_seconds,
        max_market_end_seconds=config.sports_market_end_horizon_seconds,
        max_under_seconds_remaining=config.sports_max_under_seconds_remaining,
        max_moneyline_seconds_remaining=config.sports_max_moneyline_seconds_remaining,
        max_spreads_seconds_remaining=config.sports_max_spreads_seconds_remaining,
        min_under_safety_margin=config.sports_min_under_safety_margin,
        min_moneyline_lead=config.sports_min_moneyline_lead,
        mlb_eighth_moneyline_min_lead=config.sports_mlb_eighth_moneyline_min_lead,
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
