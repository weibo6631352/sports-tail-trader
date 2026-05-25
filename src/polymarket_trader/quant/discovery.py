"""当前体育扫尾策略的远端发现查询构造。

远端 discovery 只能做粗筛；本文件负责把策略配置转换成 Polymarket Gamma
支持的 ``DiscoveryQuery``。最终是否可交易仍由 universe、盘口解析、直播状态
和风控决定。
"""

from __future__ import annotations

from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.runtime.discovery_runner import DiscoveryQuery
from polymarket_trader.quant.config import CurrentStrategyConfig


def build_configured_discovery_queries(config: CurrentStrategyConfig) -> tuple[DiscoveryQuery, ...]:
    """生成 Gamma 粗筛查询——只用 polymarket 官方 ``live=true`` 标志。

    polymarket 自己标记 ``live=true`` 的事件即"当下真正可交易的直播比赛",
    实测 30 events / 458 markets 直接覆盖核心目标. 删除旧的 start_time 窗口
    查询 (旧 12h/8h 窗口拉了大量已结束 + 远期市场, 实测 1500+ markets, 60%+
    是 stale): 让 discovery 与 polymarket /sports/live 同口径.

    Trade-offs:
    - polymarket live 标志可能比赛事真实开赛晚几秒~几分钟标记 → 接受少量延迟
    - 不再 24h 提前 track 远期 → discovery 会在赛事开打瞬间 (live 标志一打开)
      立即纳入, 实测延迟可接受
    - outright/futures (champion/season winner) 由专用 worker 拉, 不依赖
      sports 类 discovery query
    """

    tag_slugs = tuple(
        tag_slug.strip() for tag_slug in config.discovery_tag_slugs if tag_slug.strip()
    ) or ("sports",)

    queries: list[DiscoveryQuery] = []
    for tag_slug in tag_slugs:
        base = {"tag_slug": tag_slug, "order": "startTime", "ascending": "true"}
        queries.append(
            DiscoveryQuery(name=f"sports_live:{tag_slug}", params={**base, "live": "true"})
        )
    return tuple(queries)


def build_live_event_discovery_queries(
    config: CurrentStrategyConfig,
    events: tuple[LiveEvent, ...],
) -> tuple[DiscoveryQuery, ...]:
    """不再由直播源驱动发现——返回空。

    历史上这里用 Goalserve 直播比赛的队名/slug 去 Gamma 反查市场，但
    Goalserve 与 Polymarket 的名字格式不一致，名字对不上就会漏市场。
    现 ``build_configured_discovery_queries`` 直接用 Polymarket 官方的
    ``live=true`` + ``start_time`` 查询，基于 Polymarket 自己的数据完整覆盖
    正在直播/即将开赛的赛事，零名字匹配、不会漏——这条直播源驱动的反查路
    径已无必要。保留函数签名只为兼容 ``discovery_queries_for_live_events``
    扩展钩子契约。
    """

    _ = (config, events)
    return ()
