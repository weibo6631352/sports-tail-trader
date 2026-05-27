"""当前量化决策的远端发现查询构造。

远端 discovery 只能做粗筛；本文件负责把 workflow 配置转换成 Polymarket Gamma
支持的 ``DiscoveryQuery``。最终是否可交易仍由 universe、盘口解析、直播状态
和风控决定。
"""

from __future__ import annotations

from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.pipeline.ingest.market_discovery.discovery_runner import DiscoveryQuery
from polymarket_trader.workflow.config import TradingWorkflowConfig


def build_configured_discovery_queries(config: TradingWorkflowConfig) -> tuple[DiscoveryQuery, ...]:
    """生成 Gamma 粗筛查询——宽 query + 下游过滤替代不可靠的 ``live=true`` 信号。

    **架构假设级根因**（CPO R5 + 性能分析师 Round 1 + 架构师 Round 3 协评）：
    曾用 ``live=true`` 作唯一权威 query 参数，但 polymarket 该 flag 滞后/不可靠
    ——实测全平台只返 2 events，已开赛 6:48 的 CS2 LiveMix gamma 至今仍返
    ``live=None``。24h/522 commits 团队一直默认它是金标准，错过 NBA/MLB/NHL/UFC
    主流体育实时盘口，造成 discovery 死锁（7 个 mlbb 死盘 → WS 0 message →
    paper fill=0）。

    新方案：单 query + 宽过滤——
    - 主 query：``tag_slug=sports`` + ``order=startTime, ascending=true``
      （``fetch_full_market_discovery_page`` 已自动注入 ``active=True / closed=False``）
    - 不再带 ``live=true`` —— 让 polymarket 把所有 ``active+!closed`` 的 sports
      events 都返回，下游 universe / live_state matching / pruning 自己判定哪些
      真正"正在直播"
    - 单 query 单路径——架构师否决保留 ``live=true`` 作"补充查询"（§8 禁止双
      路径长期共存）

    Trade-offs：
    - events 数量可能从 2 涨到 100+：可接受。``fetch_full_market_discovery_page``
      按 cursor 分页 + ``_dedupe_discovery_queries`` 去重，带宽可吸收
    - outright/futures 远期市场会被 active 路径包含：universe / live_source
      matching 拒绝这些（无 live_state 不交易）
    - **TODO（如果实测 events 爆炸到 > 500）**：在
      ``MarketDiscoveryWorker.ingest_source_page`` 加 startDate ± N 小时二次
      过滤（架构师建议 ±6h），目前先观察实际数量
    """

    tag_slugs = tuple(
        tag_slug.strip() for tag_slug in config.discovery_tag_slugs if tag_slug.strip()
    ) or ("sports",)

    queries: list[DiscoveryQuery] = []
    for tag_slug in tag_slugs:
        params = {"tag_slug": tag_slug, "order": "startTime", "ascending": "true"}
        queries.append(DiscoveryQuery(name=f"sports:{tag_slug}", params=params))
    return tuple(queries)


def build_live_event_discovery_queries(
    config: TradingWorkflowConfig,
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
