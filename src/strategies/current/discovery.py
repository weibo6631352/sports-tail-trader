"""当前体育扫尾策略的远端发现查询构造。

远端 discovery 只能做粗筛；本文件负责把策略配置转换成 Polymarket Gamma
支持的 ``DiscoveryQuery``。最终是否可交易仍由 universe、盘口解析、直播状态
和风控决定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.extension_api import DiscoveryQuery
from strategies.current.config import CurrentStrategyConfig


def build_configured_discovery_queries(config: CurrentStrategyConfig) -> tuple[DiscoveryQuery, ...]:
    """生成 Gamma 粗筛查询——复用 Polymarket 官方 sports/live 页面的发现方法。

    官方 live 页面对 ``/events/keyset`` 只发三个定向查询、不做全量翻页扫描：
    ① ``live=true``——正在直播的赛事；
    ② ``start_time_min/max``——按**开赛时间**窗口查进行中+临近开赛的赛事；
    ③ ``start_time_min/max``——未来 24h 即将开赛的赛事。

    这三个查询都基于 Polymarket 自己的事件数据(零名字匹配、不会漏市场)，
    每个一次定向查询、秒级返回——取代旧的 title_search × tag_slug 全量翻页。
    每轮发现都会用当前时间重新生成窗口。
    """

    tag_slugs = tuple(
        tag_slug.strip() for tag_slug in config.discovery_tag_slugs if tag_slug.strip()
    ) or ("sports",)
    now = datetime.now(timezone.utc)

    def _iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    queries: list[DiscoveryQuery] = []
    for tag_slug in tag_slugs:
        base = {"tag_slug": tag_slug, "order": "startTime", "ascending": "true"}
        # ① 正在直播
        queries.append(
            DiscoveryQuery(name=f"sports_live:{tag_slug}", params={**base, "live": "true"})
        )
        # ② 进行中 + 临近开赛：开赛时间在 [now-12h, now+1h]。
        # 这条按 start_time 取，与 live 标志无关——所有进行中的比赛(startTime
        # 在过去)都会被捞到，是 ①(live=true 可能滞后)的完整兜底。-12h 覆盖
        # 长时/雨延比赛。
        queries.append(
            DiscoveryQuery(
                name=f"sports_inplay_soon:{tag_slug}",
                params={
                    **base,
                    "start_time_min": _iso(now - timedelta(hours=12)),
                    "start_time_max": _iso(now + timedelta(hours=1)),
                },
            )
        )
        # ③ 即将开赛：开赛时间在 [now+1h, now+24h]——提前发现、临近时再纳入订阅
        queries.append(
            DiscoveryQuery(
                name=f"sports_upcoming:{tag_slug}",
                params={
                    **base,
                    "start_time_min": _iso(now + timedelta(hours=1)),
                    "start_time_max": _iso(now + timedelta(hours=24)),
                },
            )
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
