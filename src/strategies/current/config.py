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

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from polymarket_trader.extension_api import load_extension_config
from strategies.current.tail import (
    ExecutionPermission,
    SportsMarketType,
    TailPolicy,
)


def _default_league_source_affinity() -> Mapping[str, tuple[str, ...]]:
    """各 league 偏好的直播源顺序，靠前权重更高（aggregate league-aware priority）。

    第 1 位映射到 priority=100、第 2 位 90、...，aggregate 据此覆盖默认全局表。
    没列出的 league 仍回退到全局 _DEFAULT_SOURCE_PRIORITY。
    """

    return {
        "NBA": ("nba", "espn", "sofascore", "thesportsdb"),
        "WNBA": ("espn", "sofascore"),
        "NHL": ("nhl", "espn", "sofascore", "thesportsdb"),
        "MLB": ("mlb", "espn", "sofascore", "thesportsdb"),
        "NFL": ("espn", "sofascore"),
        "NCAAF": ("college_football_data", "espn"),
        "NCAAMB": ("ncaa_api", "espn"),
        "NCAAWB": ("espn",),
        "NCAAB": ("ncaa_api", "espn"),
        "ATP": ("tennis_live_data", "sofascore", "espn"),
        "WTA": ("tennis_live_data", "sofascore", "espn"),
        "EPL": ("api_football", "sofascore"),
        "PREMIER-LEAGUE": ("api_football", "sofascore"),
        "F1": ("espn",),
        "NASCAR": ("espn",),
        "INDYCAR": ("espn",),
        "CS2": ("pandascore",),
        "DOTA2": ("pandascore",),
        "LOL": ("pandascore",),
        "VALORANT": ("pandascore",),
    }


# outright 评估路径：当 evaluator 未能计算出 entry_price_cap 时使用的兜底价。
# fair × (1-edge) 反向定价失效（无成交量/无隐含概率）时才会触发，等价于"以市价中点入场"。
OUTRIGHT_FALLBACK_ENTRY_PRICE = Decimal("0.50")


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
        tail_live_discovery_*:
            用外部直播源里的真实比赛队名补充高意图 discovery 查询，避免通用
            ``nba/nhl/mlb`` 搜索长期停留在冠军、系列赛、选秀或电竞市场。
        tail_category_tokens:
            本地 universe 精筛时用于识别已接入直播源的体育联赛 token。
            识别文本以 category/tags 为优先信号，并用 market/event slug、
            问题和标题兜底处理 Gamma 缺失标签的真实赛事。这里不使用泛化的
            ``sports``，避免未建模联赛仅凭大类标签进入自动交易候选。
        tail_enabled_market_types:
            体育扫尾允许纳入 universe 的盘口类型。
        tail_*:
            体育扫尾策略自己的价格、流动性、时间窗口和执行权限参数。
        tail_market_end_horizon_seconds:
            live 市场进入扫尾候选前允许的最长封盘剩余秒数。默认 3600 秒，
            用于把远离封盘的真实直播赛事留在全量发现里，但不进入实时交易候选。
        tail_max_*:
            体育扫尾策略级相关性硬上限，用于约束同一比赛、同一联赛和当日新增
            暴露（替代旧绝对 USDC，改为 bankroll fraction + 绝对 floor）。
            框架级 Kelly 单仓 cap (`KELLY_MAX_POSITION_FRACTION`) 仍由
            RiskManager 做最终门禁。
        tail_min_expected_profit_*:
            入场前按买入价格和买入金额估算等待权威结算的毛利润和资金占用效率。
            如果结算持有收益太低，策略只在可挂出满足最小毛利润的 profit-take
            SELL 时允许买入，否则拒绝这类长时间占用资金的小利润订单。
        tail_profit_take_min_profit_usdc:
            低结算效率订单允许走 profit-take 路径时，目标卖价相对买入价至少需要
            产生的预期毛利润。
        tail_profit_take_hold_minutes:
            估算一档 profit-take 挂单成交前的资金占用时间。小绝对利润订单只有
            按该占用时间折算后的每小时资金效率达标时才允许进入，避免长期挂单
            只赚极小金额。
        tail_entry_maker_max_resting_seconds:
            历史遗留开放 BUY 的最长容忍秒数。自动入场 BUY 只走触发式
            FAK 市价单；该参数只用于恢复链路撤掉旧 GTC BUY，避免长期占用资金。
            撤掉后下一轮 entry signal 会重跑 Kelly（重读最新 bankroll / fair_value），
            实现"maker 单 staleness → 重新 sizing"闭环（C10）。
        tail_settlement_hold_minutes:
            估算等待权威结算的保守资金占用时间。实盘里市场结束到可结算可能跨越
            数小时，因此这里不只看比赛剩余时间。
        tail_baseball_max_game_state_age_seconds:
            MLB 官方结构化局面允许的最大状态年龄。MLB schedule/linescore
            拉取和匹配会批量处理大量市场，不能用通用 10 秒阈值误杀第 9 局
            这类真实尾盘；该放宽只作用于棒球结构化局面。
        tail_mlb_eighth_moneyline_min_lead:
            MLB 第 8 局 moneyline 早期领先机会的最低领先分差。该规则还要求
            至少一出局且二/三垒无得分威胁，避免把普通中局波动提前纳入。
        tail_recovery_profit_take_*:
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
    tail_live_discovery_max_games: int = 40
    tail_live_discovery_max_queries: int = 120
    # CLAUDE.md §9：所有目标盘口必须纳入诊断，不得 silent 忽略。所以 universe
    # 仍接受 KBO / WTT / table-tennis 等无默认源支持的联赛——它们进 record-only
    # 通道，由 live-source-gaps 把 `slug_prefix` 标 unsupported_league 让运维侧
    # 区分"暂时缺直播 vs 联赛根本不被覆盖"。运维启用 sofascore/pandascore 等
    # 额外源后这些 token 自然产生有效信号。
    tail_category_tokens: tuple[str, ...] = (
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
    tail_enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
        SportsMarketType.BINARY_PROP,
    )
    tail_totals_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_moneyline_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_spreads_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_totals_max_entry_price: Decimal = Decimal("0.99")
    tail_moneyline_max_entry_price: Decimal = Decimal("0.98")
    tail_tennis_locked_moneyline_max_entry_price: Decimal = Decimal("0.995")
    tail_spreads_max_entry_price: Decimal = Decimal("0.96")
    tail_min_liquidity_usdc: Decimal = Decimal("1")
    tail_max_game_state_age_seconds: int = 10
    tail_baseball_max_game_state_age_seconds: int = 45
    tail_tennis_max_game_state_age_seconds: int = 35
    # 赛事起始时间超过该秒数且仍无任何直播状态 → 视为 stale market 主动 pause。
    # 默认 24 小时：MLB/NBA/NHL 单场比赛通常 4-6 小时内完成；超过 24 小时无任何
    # 直播信号意味着该 market 已脱离入场窗口（赛事已结束 / 联赛不被任何数据源覆盖），
    # 继续扫描只是 noise。reconcile 看到 pause 后会把 market 从订阅集合排除。
    tail_stale_no_live_state_seconds: int = 86_400
    tail_market_end_horizon_seconds: int = 3600
    tail_max_under_seconds_remaining: int = 30
    tail_max_moneyline_seconds_remaining: int = 180
    tail_max_spreads_seconds_remaining: int = 120
    tail_min_under_safety_margin: Decimal = Decimal("2")
    tail_min_moneyline_lead: int = 6
    tail_mlb_eighth_moneyline_min_lead: int = 2
    tail_min_spread_safety_margin: Decimal = Decimal("2")
    # 相关性硬上限（与 Kelly 单市场 cap 互补）：单一事件 / 联赛 / 日新增 限额
    # = bankroll × fraction。bankroll 涨大时 cap 同步放大；bankroll 极小时 cap
    # 接近 0 但是用绝对 USDC floor 兜底，避免极小 bankroll 阶段每个 cap 都拒。
    tail_max_event_exposure_fraction: Decimal = Decimal("0.25")
    tail_max_league_exposure_fraction: Decimal = Decimal("0.75")
    tail_max_daily_entry_fraction: Decimal = Decimal("1.5")
    tail_max_event_exposure_min_floor_usdc: Decimal = Decimal("5")
    tail_max_league_exposure_min_floor_usdc: Decimal = Decimal("10")
    tail_max_daily_entry_min_floor_usdc: Decimal = Decimal("25")
    # 单场景 (single-game tail) implied fair value 公式：``cap = fair × (1 - edge_required)``
    # 解出 fair。500 bps = 5% 表示策略相信"fair 比 cap 至少高 5%"。
    # 用于 Kelly sizing 的 prob_p。outright path 直接用 the-odds-api 真概率。
    #
    # 与 ``tail_outright_min_edge_bps`` 数学功能相同（required edge），但作用范围不同：
    # 这个用于 tail single-game 反推 implied prob_p；outright_min_edge_bps 用于
    # outright family 在 fair_value 已知后算 entry_price_cap。outright 接通 Kelly 后
    # （B9 / 未来工作）再考虑统一字段。
    # 操盘手运行时调宽走 ParameterStore override，不动默认。
    tail_implied_min_edge_bps: int = 500
    # tail implied_p 的不确定性 → κ 缩放（confidence）。0.5 = 半 κ baseline。
    # 实际 conf = base × min(1, ask_depth / depth_baseline_usdc) × max(0.25, 1 - spread/spread_widening)
    # 流动性薄 / 价差宽时进一步收缩，符合"implied_p 在低质量盘口里更不可靠"。
    # outright path 当前不走 Kelly（_size_outright_entry 用固定 budget 包络 +
    # outright/evaluator 的反向定价），不消费此字段；未来 outright 接通 Kelly 时
    # 应直接用 the-odds-api 真概率 + conf=1.0（B9 / 未来工作）。
    # 操盘手运行时调宽走 ParameterStore override，不动默认。
    tail_implied_prob_confidence: Decimal = Decimal("0.5")
    # 流动性 baseline：ask_depth >= 此值时不再缩 conf；不到时按比例缩。25 USDC ≈ 5 shares × 0.50。
    tail_implied_conf_depth_baseline_usdc: Decimal = Decimal("25")
    # spread 容忍：spread > 此值时 conf 衰减到 25%；spread=0 时不缩。
    tail_implied_conf_spread_widening: Decimal = Decimal("0.05")
    tail_max_consecutive_losses: int = 3
    tail_scale_in_budget_fraction: Decimal = Decimal("0.5")
    tail_scale_in_max_buy_fills: int = 2
    tail_min_expected_profit_usdc: Decimal = Decimal("0.03")
    tail_min_expected_profit_per_hour_usdc: Decimal = Decimal("0.10")
    tail_profit_take_min_profit_usdc: Decimal = Decimal("0.02")
    tail_profit_take_hold_minutes: int = 2
    tail_entry_maker_max_resting_seconds: int = 60
    tail_settlement_hold_minutes: int = 180
    tail_recovery_profit_take_enabled: bool = True
    tail_recovery_profit_take_min_avg_price: Decimal = Decimal("0.90")

    # Outright family 配置。默认 budget=0 + RECORD_ONLY；必须两个 flip 才真实下单。
    tail_outright_enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.MONEYLINE,
        SportsMarketType.BINARY_PROP,
    )
    tail_outright_execution_permission: ExecutionPermission = ExecutionPermission.RECORD_ONLY
    tail_outright_min_edge_bps: int = 500  # 5%
    tail_outright_max_entry_price: Decimal = Decimal("0.85")
    tail_outright_budget_usdc: Decimal = Decimal("0")
    tail_outright_max_per_market_usdc: Decimal = Decimal("25")
    tail_outright_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_outright_max_hold_horizon_days: int = 180
    tail_outright_max_season_odds_age_seconds: int = 14400
    tail_outright_season_odds_ttl_seconds: int = 1800
    tail_outright_reassessment_interval_seconds: int = 3600
    tail_outright_exit_edge_target: Decimal = Decimal("0.03")
    tail_outright_min_profit_per_share: Decimal = Decimal("0.02")
    tail_outright_stop_loss_pct: Decimal = Decimal("0.30")
    tail_outright_fair_value_drift_pct: Decimal = Decimal("0.15")
    tail_outright_min_remaining_days: int = 7
    tail_outright_late_min_pnl_pct: Decimal = Decimal("0.05")
    tail_outright_entry_maker_max_resting_seconds: int = 86400
    tail_outright_min_orderbook_depth_usdc: Decimal = Decimal("100")

    # Series WINNER family 配置。默认 budget=0 + RECORD_ONLY；与 outright 同样
    # 双 flip（permission=AUTO_EXECUTE + budget>0）才会真实下单。
    tail_series_winner_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_series_winner_min_edge_bps: int = 200
    tail_series_winner_max_entry_price: Decimal = Decimal("0.95")
    tail_series_winner_budget_usdc: Decimal = Decimal("25")
    tail_series_winner_max_per_market_usdc: Decimal = Decimal("25")
    tail_series_winner_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_series_winner_min_orderbook_depth_usdc: Decimal = Decimal("50")
    tail_series_winner_max_state_age_seconds: int = 3600  # 1h：比赛日内 state 应频繁刷新
    tail_series_winner_max_game_odds_age_seconds: int = 7200  # 2h
    tail_series_winner_max_hold_horizon_days: int = 30
    tail_series_winner_min_remaining_days: int = 0  # 系列赛剩余比赛随时可成交
    tail_series_winner_exit_edge_target: Decimal = Decimal("0.05")
    tail_series_winner_min_profit_per_share: Decimal = Decimal("0.02")

    # Series TOTAL_GAMES family 配置。分布尾端方差大，default min_edge 比 WINNER 略高。
    tail_series_total_games_execution_permission: ExecutionPermission = ExecutionPermission.RECORD_ONLY
    tail_series_total_games_min_edge_bps: int = 1000  # 10%
    tail_series_total_games_max_entry_price: Decimal = Decimal("0.92")
    tail_series_total_games_budget_usdc: Decimal = Decimal("0")
    tail_series_total_games_max_per_market_usdc: Decimal = Decimal("25")
    tail_series_total_games_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_series_total_games_min_orderbook_depth_usdc: Decimal = Decimal("50")
    tail_series_total_games_max_hold_horizon_days: int = 30
    tail_series_total_games_min_remaining_days: int = 0

    # Series GAME_HANDICAP family 配置。single_game scope 与 series scope 共用
    # 同一组配置——风险定价路径不同但资金 / 时间窗约束等同。
    tail_series_handicap_execution_permission: ExecutionPermission = ExecutionPermission.RECORD_ONLY
    tail_series_handicap_min_edge_bps: int = 1000  # 10%
    tail_series_handicap_max_entry_price: Decimal = Decimal("0.92")
    tail_series_handicap_budget_usdc: Decimal = Decimal("0")
    tail_series_handicap_max_per_market_usdc: Decimal = Decimal("25")
    tail_series_handicap_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_series_handicap_min_orderbook_depth_usdc: Decimal = Decimal("50")
    tail_series_handicap_max_hold_horizon_days: int = 30
    tail_series_handicap_min_remaining_days: int = 0

    # league-aware 源亲和：aggregate_client 用此覆盖默认全局源优先级表。
    # 仅当前体育扫尾策略关心；framework Settings 不持有，CLAUDE.md §10。
    league_source_affinity: Mapping[str, tuple[str, ...]] = field(
        default_factory=_default_league_source_affinity
    )


def tail_policy_from_config(config: CurrentStrategyConfig) -> TailPolicy:
    """把当前策略配置转换成体育扫尾纯业务策略参数。"""

    return TailPolicy(
        enabled_market_types=config.tail_enabled_market_types,
        totals_execution_permission=config.tail_totals_execution_permission,
        moneyline_execution_permission=config.tail_moneyline_execution_permission,
        spreads_execution_permission=config.tail_spreads_execution_permission,
        totals_max_entry_price=config.tail_totals_max_entry_price,
        moneyline_max_entry_price=config.tail_moneyline_max_entry_price,
        tennis_locked_moneyline_max_entry_price=config.tail_tennis_locked_moneyline_max_entry_price,
        spreads_max_entry_price=config.tail_spreads_max_entry_price,
        min_liquidity_usdc=config.tail_min_liquidity_usdc,
        max_game_state_age_seconds=config.tail_max_game_state_age_seconds,
        baseball_max_game_state_age_seconds=config.tail_baseball_max_game_state_age_seconds,
        tennis_max_game_state_age_seconds=config.tail_tennis_max_game_state_age_seconds,
        max_market_end_seconds=config.tail_market_end_horizon_seconds,
        max_under_seconds_remaining=config.tail_max_under_seconds_remaining,
        max_moneyline_seconds_remaining=config.tail_max_moneyline_seconds_remaining,
        max_spreads_seconds_remaining=config.tail_max_spreads_seconds_remaining,
        min_under_safety_margin=config.tail_min_under_safety_margin,
        min_moneyline_lead=config.tail_min_moneyline_lead,
        mlb_eighth_moneyline_min_lead=config.tail_mlb_eighth_moneyline_min_lead,
        min_spread_safety_margin=config.tail_min_spread_safety_margin,
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
