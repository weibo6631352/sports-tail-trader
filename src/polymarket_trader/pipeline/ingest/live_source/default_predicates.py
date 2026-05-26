"""SportsSubscriptionPolicy 的默认 active_predicate 实现。

`is_market_live_active(market)` 判断 market 是否值得拉直播数据——三种 active
模式命中任一即视为活跃：

- **is_live**：已开赛且仍在 game_window 内（默认 7h，覆盖 MLB / NBA / NHL / NFL /
  Tennis 单场上限 6h + 1h buffer）
- **is_near_start**：未来 60min 内开赛
- **start_unknown_active**：game_start_time 缺失但 end_date 在未来 6h 内（Polymarket
  end_date 对体育单场常 == game_start_time，无法直接用 end > now 判 live）

逻辑迁移自旧 `runtime/sports_polling_demand._is_market_active`，本模块作为
SportsSubscriptionPolicy 的默认值。策略层可注入更精细的 active_predicate
（如按 sport 区分窗口、按风险 budget 限制订阅密度）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.market import Market

# 单场比赛持续时间上限 ≤ 6h（MLB/NBA/NHL/NFL/Tennis），加 1h buffer 保证末段
# inplay 仍拉。Polymarket end_date 对体育单场市场常 == game_start_time
# （Gamma API 字段语义混淆），不能用 end > now 判 live。
_DEFAULT_GAME_WINDOW = timedelta(hours=7)
_DEFAULT_NEAR_START_WINDOW = timedelta(minutes=60)
_DEFAULT_START_UNKNOWN_BUFFER = timedelta(hours=6)


def is_market_live_active(
    market: Market,
    *,
    now: datetime | None = None,
    game_window: timedelta = _DEFAULT_GAME_WINDOW,
    near_start_window: timedelta = _DEFAULT_NEAR_START_WINDOW,
    start_unknown_buffer: timedelta = _DEFAULT_START_UNKNOWN_BUFFER,
) -> bool:
    reference = now or datetime.now(timezone.utc)
    start = _normalize(market.game_start_time)
    end = _normalize(market.end_date)
    near_start_cutoff = reference + near_start_window

    is_live = start is not None and start <= reference <= start + game_window
    is_near_start = start is not None and reference <= start <= near_start_cutoff
    start_unknown_active = start is None and (
        end is None or reference < end < reference + start_unknown_buffer
    )
    return is_live or is_near_start or start_unknown_active


def _normalize(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
