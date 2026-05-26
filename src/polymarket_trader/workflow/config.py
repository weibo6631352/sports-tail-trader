"""量化决策配置定义。

只承载量化业务参数，不负责框架级配置。所有量化决策（BUY / SELL / HOLD）
经 ``quant_decider`` 走 Kelly + quant_signal 主路径——下面只是 Kelly sizing
基础参数 + 入场 universe 过滤 + 直播状态新鲜度阈值。

新量化信号源应在 ``workflow.quant_signal`` 里扩展，不在本文件加新 knob。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from polymarket_trader.sports import SportsMarketType


def _default_league_source_affinity() -> Mapping[str, tuple[str, ...]]:
    """各 league 偏好的直播源顺序——全部联赛 Goalserve inplay。"""

    _gs = ("goalserve_inplay",)
    return {
        "NBA": _gs,
        "WNBA": _gs,
        "NHL": _gs,
        "MLB": _gs,
        "NFL": _gs,
        "NCAAF": _gs,
        "NCAAMB": _gs,
        "NCAAWB": _gs,
        "NCAAB": _gs,
        "ATP": _gs,
        "WTA": _gs,
        "EPL": _gs,
        "PREMIER-LEAGUE": _gs,
        "CS2": _gs,
        "DOTA2": _gs,
        "LOL": _gs,
        "VALORANT": _gs,
        "UFC": _gs,
        "MMA": _gs,
    }


@dataclass(frozen=True, slots=True)
class TradingWorkflowConfig:
    """量化决策静态配置。"""

    # Kelly sizing 参数（workflow 层决策参数，不进框架 Settings）。
    # κ 默认 0.25 = quarter Kelly：模型不确定性下的工业标准。
    kelly_fraction: Decimal = Decimal("0.25")
    # 单仓上限（占 bankroll 比例）。1.0 = 不额外设单仓硬上限，严格按 quarter-Kelly。
    kelly_max_position_fraction: Decimal = Decimal("1.0")
    # 最低 edge 阈值；edge < 200 bps 时 Kelly 对 p 估计误差极敏感，不下单。
    kelly_min_edge: Decimal = Decimal("0.02")
    # 框架硬下限 USDC；effective_min_stake = max(此值, market.min_order_size × price)。
    kelly_min_stake_usdc: Decimal = Decimal("1")
    # Kelly 推荐 stake < market min 时是否凑齐到 market min（轻度 over-bet）。
    kelly_allow_round_up_to_market_min: bool = True
    # 凑齐金额上限 = position_cap × ratio。
    kelly_round_up_max_overbet_ratio: Decimal = Decimal("10")

    # Discovery + universe 过滤。
    discovery_tag_slugs: tuple[str, ...] = ("sports",)
    # 已接入直播源的体育联赛 token 白名单；universe 精筛用。
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
        "cricket",
        "ipl",
        "bbl",
        "rugby",
        "esports",
        "cs2",
        "csgo",
        "counter-strike",
        "dota2",
        "dota",
        "lol",
        "league-of-legends",
        "valorant",
    )
    enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
        SportsMarketType.BINARY_PROP,
    )

    # 直播状态新鲜度阈值（reconcile 判 stale → pause market 用）。
    # default 兜底：inplay GZIP 运动 1-3s 推送，但暂停（timeout / break）30-60s
    # 间隙；60s 覆盖正常间隙。
    default_max_game_state_age_seconds: int = 60
    # MLB/KBO baseball livescore feed 30-90s 才推新比分；120s buffer 与 soccer 同口径。
    baseball_max_game_state_age_seconds: int = 120
    # cricket/rugby/handball 等纯 livescore-only 运动周期与 soccer 同级；统一 120s。
    livescore_only_max_game_state_age_seconds: int = 120
    tennis_max_game_state_age_seconds: int = 35
    # J1/J2/各次级足球联赛 60-90s 更新；120s buffer。
    soccer_max_game_state_age_seconds: int = 120
    # esports livescore feed 60s 刷新；90s 新鲜度窗口。
    esports_max_game_state_age_seconds: int = 90
    # 赛事起始时间超过此秒且仍无直播状态 → 视为 stale market 主动 pause。
    stale_no_live_state_seconds: int = 86_400

    # 历史遗留 maker BUY 撤单阈值；自动入场 BUY 走 FAK 不留 resting。
    entry_maker_max_resting_seconds: int = 60

    # league-aware 源亲和：aggregate_client 用此覆盖默认全局源优先级表。
    league_source_affinity: Mapping[str, tuple[str, ...]] = field(
        default_factory=_default_league_source_affinity
    )


def default_workflow_config() -> TradingWorkflowConfig:
    """返回内置默认配置。"""

    return TradingWorkflowConfig()


def load_workflow_config() -> TradingWorkflowConfig:
    """加载量化配置——直接 dataclass 默认值，无外部覆盖层。

    调阈值改代码里的默认值即可，重启生效。
    """

    return TradingWorkflowConfig()
