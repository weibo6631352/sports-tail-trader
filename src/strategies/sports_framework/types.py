"""体育策略通用类型层：枚举 + 内部 dataclass。

不依赖任何评估或解析逻辑；任何体育策略都可以从这里取业务对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    BasketballGameState,
    CricketGameState,
    EsportsGameState,
    HandballGameState,
    SoccerGameState,
    TennisGameState,
    VolleyballGameState,
)


class SportsMarketType(StrEnum):
    """体育盘口类型。"""

    TOTALS = "totals"
    MONEYLINE = "moneyline"
    SPREADS = "spreads"
    BINARY_PROP = "binary_prop"


class SportsMarketFamily(StrEnum):
    """按结算语义划分的市场家族。"""

    SINGLE_GAME = "single_game"
    SERIES = "series"
    OUTRIGHT = "outright"
    ESPORTS = "esports"
    UNSUPPORTED = "unsupported"


class SportsMarketSide(StrEnum):
    """体育盘口方向。"""

    OVER = "over"
    UNDER = "under"
    HOME = "home"
    AWAY = "away"
    YES = "yes"
    NO = "no"


class SportsMarketScopeType(StrEnum):
    """盘口结算范围。"""

    FULL_GAME = "full_game"
    TENNIS_MATCH_GAMES = "tennis_match_games"
    TENNIS_TOTAL_SETS = "tennis_total_sets"
    TENNIS_SET_GAMES = "tennis_set_games"
    # 篮球上半场盘口（1H total / 1H spread / 1H moneyline）。
    BASKETBALL_FIRST_HALF = "basketball_first_half"
    # 篮球单节盘口（Q1-Q4），scope_number 标注第几节。
    BASKETBALL_QUARTER = "basketball_quarter"
    # 篮球下半场盘口（2H = Q3+Q4）。
    BASKETBALL_SECOND_HALF = "basketball_second_half"
    # 其它运动的分段盘口（冰球分节、棒球 F5 等）——已识别为分段但当前无
    # 干净的分段比分模型，区别于 totals 用的 UNSUPPORTED_PERIOD。
    UNSUPPORTED_SUBPERIOD = "unsupported_subperiod"
    UNSUPPORTED_PERIOD = "unsupported_period"


class LiveGameStatus(StrEnum):
    """策略侧直播比赛状态。"""

    SCHEDULED = "scheduled"
    LIVE = "live"
    PAUSED = "paused"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    RETIRED = "retired"
    ENDED = "ended"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LiveGameState:
    """策略评估所需的直播比赛状态。

    所有 sport-specific state（baseball/tennis/soccer/esports/cricket/volleyball）均复用
    ``polymarket_trader.domain.sports_live`` 中的单一定义，与 infra 归一化保持
    类型同源。
    """

    league: str
    home_name: str
    away_name: str
    home_score: int
    away_score: int
    period: str
    status: LiveGameStatus
    seconds_remaining: int | None = None
    observed_at: datetime | None = None
    # HTTP `Date` header（Goalserve server 生成响应时刻）—— live_feed_lag_seconds
    # @property 据此算"feed 数据 stale 程度"，决策侧在 lag > 阈值时降级。
    server_clock_at: datetime | None = None
    source_conflicts: tuple[Mapping[str, Any], ...] = ()
    sport: str = ""
    baseball_state: BaseballGameState | None = None
    basketball_state: BasketballGameState | None = None
    tennis_state: TennisGameState | None = None
    soccer_state: SoccerGameState | None = None
    esports_state: EsportsGameState | None = None
    cricket_state: CricketGameState | None = None
    handball_state: HandballGameState | None = None
    volleyball_state: VolleyballGameState | None = None

    @property
    def total_score(self) -> int:
        return self.home_score + self.away_score

    def live_feed_lag_seconds(self, now: datetime | None = None) -> float | None:
        """直播 feed 当前 stale 程度（秒）= now - server_clock_at。

        返回 None 表示无 server_clock 数据（旧数据 / 测试构造 / 未走 Goalserve 客户端）。
        >0 = feed 已陈旧；阈值 10s 是经验值（inplay feed 1.2s 轮询 + 网络 < 2s，
        正常 ≤ 4s；超过 10s 说明 server / 网络 / 我方解析有问题，决策应降级）。
        """
        if self.server_clock_at is None:
            return None
        if now is None:
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
        return (now - self.server_clock_at).total_seconds()

    def score_diff_for(self, side: SportsMarketSide) -> int:
        if side == SportsMarketSide.HOME:
            return self.home_score - self.away_score
        if side == SportsMarketSide.AWAY:
            return self.away_score - self.home_score
        return 0

    def progress_quantification(self) -> dict[str, object]:
        """跨运动统一的比赛进度量化（自动决策必备时间维度信号）。

        返回:
        - progress_pct: 比赛进度 0.0-1.0（0=刚开赛, 1=结束）
        - phase: pregame / early / mid / late / final / ended
        - time_remaining_seconds: 剩余秒数（如不可估算返回 None）
        - segment_label: 该运动的阶段文字（"Q3" / "Inning 6" / "Set 2" / "Min 65"）
        - is_critical_moment: 末段关键时刻（last 2min / 9th inning / final set 决胜局等）

        各运动定义:
        - baseball: progress = (current_inning - 1 + 0.5*inning_half) / 9, 9th inning = critical
        - basketball: progress = (current_period - 1 + 0.5) / 4 (或按 seconds_remaining 精细化),
                      4th quarter last 2min = critical
        - tennis: progress = current_set / max(best_of, 3), 决胜盘 = critical
        - soccer: progress = clock_minutes / 90, second_half 80+min = critical
        - 其他: progress 估算 = (period_num / typical_periods); 无法估则 None
        """
        result: dict[str, object] = {
            "progress_pct": None,
            "phase": "unknown",
            "time_remaining_seconds": self.seconds_remaining,
            "segment_label": self.period or "",
            "is_critical_moment": False,
        }
        if self.status == LiveGameStatus.SCHEDULED:
            result.update({"progress_pct": 0.0, "phase": "pregame"})
            return result
        if self.status == LiveGameStatus.ENDED:
            result.update({"progress_pct": 1.0, "phase": "ended"})
            return result

        sport = (self.sport or "").lower()
        # baseball: 9 inning, top/bottom 各算半局
        if sport == "baseball" and self.baseball_state is not None:
            inning = self.baseball_state.current_inning or 0
            half = 0.5 if (self.baseball_state.inning_half or "").lower() == "bottom" else 0.0
            pct = max(0.0, min(1.0, (inning - 1 + half) / 9.0))
            result["progress_pct"] = round(pct, 3)
            result["segment_label"] = f"Inning {inning}" + (" Bot" if half else " Top")
            result["is_critical_moment"] = inning >= 9
        # basketball: 4 quarter
        elif sport in ("basketball", "basket") and self.basketball_state is not None:
            period = self.basketball_state.current_period or 0
            pct = max(0.0, min(1.0, (period - 0.5) / 4.0))
            result["progress_pct"] = round(pct, 3)
            result["segment_label"] = f"Q{period}"
            # 4th quarter last 2min（如有 seconds_remaining）= critical
            result["is_critical_moment"] = period >= 4 and (
                self.seconds_remaining is None or self.seconds_remaining <= 120
            )
        # tennis: current_set / best_of
        elif sport == "tennis" and self.tennis_state is not None:
            cur = self.tennis_state.current_set or 0
            best_of = self.tennis_state.best_of or 3
            pct = max(0.0, min(1.0, cur / best_of)) if best_of > 0 else 0.0
            result["progress_pct"] = round(pct, 3)
            result["segment_label"] = f"Set {cur}/{best_of}"
            result["is_critical_moment"] = cur >= best_of  # 决胜盘
        # soccer: clock_minutes / 90
        elif sport == "soccer" and self.soccer_state is not None:
            mins = self.soccer_state.clock_minutes or 0
            period = (self.soccer_state.period or "").lower()
            base_mins = 0 if "first" in period else (45 if "second" in period else (90 if "extra" in period else 0))
            total_mins = base_mins + mins
            pct = max(0.0, min(1.0, total_mins / 90.0))
            result["progress_pct"] = round(pct, 3)
            result["segment_label"] = f"Min {total_mins}"
            # second_half 80+min = critical
            result["is_critical_moment"] = total_mins >= 80
        elif sport == "ice-hockey":
            # 3 period 各 20min；period 从 self.period 字符串解析
            result["segment_label"] = self.period
        else:
            # 其他运动: 仅返回 period 文本，progress=None
            result["segment_label"] = self.period

        # 阶段分类（基于 progress_pct）
        pct_val = result["progress_pct"]
        if isinstance(pct_val, (int, float)):
            if pct_val < 0.25:
                result["phase"] = "early"
            elif pct_val < 0.6:
                result["phase"] = "mid"
            elif pct_val < 0.9:
                result["phase"] = "late"
            else:
                result["phase"] = "final"
        return result


@dataclass(frozen=True, slots=True)
class SportsMarketSnapshot:
    """策略评估所需的体育盘口快照。"""

    market_type: SportsMarketType
    side: SportsMarketSide
    token_id: str
    line: Decimal | None
    best_ask: Decimal | None
    buyable_liquidity_usdc: Decimal
    market_family: SportsMarketFamily = SportsMarketFamily.SINGLE_GAME
    market_slug: str | None = None
    # Polymarket Gamma 的 sportsMarketType：运动专属 prop 家族（method-of-victory、
    # F1 props、cricket props 等）识别的首选信号；常规盘口多为空，回退 slug 关键字。
    sports_market_type: str | None = None
    market_end_date: datetime | None = None
    scope_type: SportsMarketScopeType = SportsMarketScopeType.FULL_GAME
    scope_number: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    best_bid: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SportsMarketScope:
    """结构化盘口结算范围。"""

    scope_type: SportsMarketScopeType
    scope_number: int | None = None
