"""当前策略的本地 universe 精筛逻辑。

远端 discovery 只负责“粗筛”，真正是否纳入策略 universe，
仍然由这里统一判断。这样做的好处是：

1. 即使远端搜索条件放宽，本地最终语义仍然稳定。
2. 调参时只改一个文件即可看清楚市场被接纳的完整条件。
"""

from __future__ import annotations

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.decisions import UniverseDecision
from polymarket_trader.workflow.tail import SportsMarketFamily, SportsMarketType
from polymarket_trader.sports.slug import is_unsupported_period_total

from polymarket_trader.workflow.config import TradingWorkflowConfig


def select_market(config: TradingWorkflowConfig, market: Market) -> UniverseDecision:
    """判断某个 market 是否属于当前策略 universe。

    参数：
        config:
            当前策略配置，提供 token 级筛选条件。
        market:
            已经过框架基础分类后的内部 ``Market`` 对象。

    返回：
        ``UniverseDecision``：
        - ``include`` 表示纳入策略 universe；
        - ``exclude`` 表示排除，并附带原因。

    规则：
        - 分类或标签文本至少命中一个体育 token；
        - market 文本和 outcomes 能解析成目标盘口类型；
        - 盘口类型在策略白名单内。
    """

    from polymarket_trader.workflow.outcomes import describe_sports_market

    descriptor = describe_sports_market(market)
    if not descriptor.accepted or descriptor.market_type is None:
        return UniverseDecision.exclude(reason=descriptor.reason or "market_parse_failed")
    family = descriptor.market_family
    # 接受 single_game ∪ outright ∪ series；esports 仍可审计拒绝（§9：所有盘口至少建模）。
    if family not in {SportsMarketFamily.SINGLE_GAME, SportsMarketFamily.OUTRIGHT, SportsMarketFamily.SERIES}:
        return UniverseDecision.exclude(
            reason=descriptor.reason or f"{family.value}_family_excluded",
        )

    category_tokens = _normalized_tokens(_universe_text(market))
    if family == SportsMarketFamily.SINGLE_GAME:
        # single_game 必须命中体育 token 才进入策略 universe，避免泛体育候选噪音。
        if not set(config.tail_category_tokens) & category_tokens:
            return UniverseDecision.exclude(reason="category_not_matched")
        # 分节/分局/前N局 totals（如 1st half total、1st quarter total、
        # 1st inning total）无完整结算模型，提前排除避免浪费 sizing 计算。
        if descriptor.market_type == SportsMarketType.TOTALS and is_unsupported_period_total(
            _universe_text(market).lower()
        ):
            return UniverseDecision.exclude(reason="unsupported_period_total_record_only")
        if descriptor.market_type not in config.tail_enabled_market_types:
            return UniverseDecision.exclude(reason="market_type_disabled")
    elif family == SportsMarketFamily.OUTRIGHT:
        # outright：枚举的 market_type 是 binary_prop/moneyline；按 outright 自己的
        # 白名单过滤，避免和 single_game 共用 tail_enabled_market_types。
        if descriptor.market_type not in config.tail_outright_enabled_market_types:
            return UniverseDecision.exclude(reason="outright_market_type_disabled")
    else:
        # series：classify_series_sub_type 返回 OTHER 表示文本无系列赛关键词，直接拒绝。
        from polymarket_trader.workflow.series.classifier import classify_series_sub_type
        from polymarket_trader.workflow.series.types import SeriesSubType

        sub_type = classify_series_sub_type(market)
        if sub_type == SeriesSubType.OTHER:
            return UniverseDecision.exclude(reason="series_sub_type_unclassifiable")
    return UniverseDecision.include(
        reason="market_selected",
        metadata={
            "market_family": family,
            "market_type_label": descriptor.market_type.value,
            "market_line": str(descriptor.line) if descriptor.line is not None else None,
            "target_count": len(descriptor.targets),
        },
    )


def _non_empty(*values: str | None) -> tuple[str, ...]:
    """过滤掉空值文本，方便后续统一拼接。"""

    return tuple(value for value in values if value)


def _universe_text(market: Market) -> str:
    """汇总用于识别目标体育联赛的稳定市场文本。

    Gamma 部分网球市场会缺失 category/tags，但 slug 和 event_slug 仍然包含
    ATP/WTA 等联赛信号。universe 精筛先完成盘口和市场家族解析，再用这些
    稳定文本做联赛兜底，避免把真实直播候选误挡在交易链路之前。
    """

    return " ".join(
        _non_empty(
            market.category,
            *market.tags,
            *market.matched_keywords,
            market.market_slug,
            market.event_slug,
            market.market_question,
            market.market_name,
            market.event_title,
        )
    )


def _normalized_tokens(text: str | None) -> set[str]:
    """把原始文本归一化成稳定 token 集合。

    参数：
        text:
            原始标题、问题或 slug 文本。

    返回：
        归一化后的 token 集合。

    说明：
        这个函数故意只做通用分词，不承载盘口判断；盘口语义由
        ``outcomes.describe_sports_market`` 统一解析。
    """

    if not text:
        return set()
    normalized = text.lower()
    parts: list[str] = []
    current: list[str] = []
    for char in normalized:
        if char.isalnum():
            current.append(char)
            continue
        if current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    return {part for part in parts if part}
