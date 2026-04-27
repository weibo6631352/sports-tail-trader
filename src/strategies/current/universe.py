"""当前策略的本地 universe 精筛逻辑。

远端 discovery 只负责“粗筛”，真正是否纳入策略 universe，
仍然由这里统一判断。这样做的好处是：

1. 即使远端搜索条件放宽，本地最终语义仍然稳定。
2. 二次开发时可以只改一个文件，就看清楚市场被接纳的完整条件。
"""

from __future__ import annotations

from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api import UniverseDecision

from strategies.current.config import CurrentStrategyConfig


def select_market(config: CurrentStrategyConfig, market: Market) -> UniverseDecision:
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

    category_tokens = _normalized_tokens(" ".join(_non_empty(market.category, *market.tags)))
    if not set(config.sports_category_tokens) & category_tokens:
        return UniverseDecision.exclude(reason="sports_category_not_matched")

    from strategies.current.outcomes import describe_sports_market

    descriptor = describe_sports_market(market)
    if not descriptor.accepted or descriptor.market_type is None:
        return UniverseDecision.exclude(reason=descriptor.reason or "sports_market_parse_failed")
    if descriptor.market_type not in config.sports_enabled_market_types:
        return UniverseDecision.exclude(reason="sports_market_type_disabled")
    return UniverseDecision.include(
        reason="sports_market_selected",
        metadata={
            "sports_market_type": descriptor.market_type.value,
            "sports_line": str(descriptor.line) if descriptor.line is not None else None,
            "sports_target_count": len(descriptor.targets),
        },
    )


def _non_empty(*values: str | None) -> tuple[str, ...]:
    """过滤掉空值文本，方便后续统一拼接。"""

    return tuple(value for value in values if value)


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
