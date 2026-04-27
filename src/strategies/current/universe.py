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
        - 分类文本至少命中一个要求的 category token；
        - event 文本必须包含所有要求的 event token；
        - market 文本必须包含所有要求的 target token。
    """

    category_tokens = _normalized_tokens(" ".join(_non_empty(market.category, *market.tags)))
    event_tokens = _normalized_tokens(market.event_title)
    market_tokens = _normalized_tokens(
        " ".join(_non_empty(market.market_question, market.market_name, market.market_slug))
    )

    has_category = bool(set(config.required_category_tokens) & category_tokens)
    has_event = all(token in event_tokens for token in config.required_event_tokens)
    has_target = all(token in market_tokens for token in config.required_target_tokens)
    if has_category and has_event and has_target:
        return UniverseDecision.include(reason="selected_by_strategy")
    return UniverseDecision.exclude(reason="market_out_of_universe")


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
        这个函数故意做了少量业务缩写归一化，例如把
        ``fully diluted valuation`` 统一成 ``fdv``，
        把不同写法的 ``500 million`` 统一成 ``500m``，
        这样上层筛选规则可以写得更稳定。
    """

    if not text:
        return set()
    normalized = (
        text.lower()
        .replace("$500 million", "500m")
        .replace("500 million", "500m")
        .replace("500,000,000", "500m")
        .replace("$500m", "500m")
        .replace("500 m", "500m")
        .replace("fully diluted valuation", "fdv")
    )
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
