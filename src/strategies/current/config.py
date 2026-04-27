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

from dataclasses import dataclass
from decimal import Decimal

from polymarket_trader.extension_api import load_extension_config


@dataclass(frozen=True, slots=True)
class CurrentStrategyConfig:
    """当前策略的静态配置。

    字段说明：
        entry_no_price_max:
            NO 侧允许主动买入的最高价格。盘口 ask 高于这个值时，
            策略会直接跳过本轮入场。
        exit_no_price:
            退出时使用的目标挂卖价格。
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
            ``tag_slug``。例如 ``("crypto",)`` 会请求
            ``/events/keyset?title_search=fdv&tag_slug=crypto``。
            如果不确定 Gamma tag 是否覆盖目标市场，保持为空，并让
            ``select_market()`` 做本地最终过滤。
        required_category_tokens:
            本地 universe 精筛时必须命中的分类 token。
        required_event_tokens:
            本地 universe 精筛时必须命中的 event 级 token。
        required_target_tokens:
            本地 universe 精筛时必须命中的 market 文本 token。
    """

    entry_no_price_max: Decimal = Decimal("0.60")
    exit_no_price: Decimal = Decimal("0.70")
    min_liquidity_usdc: Decimal = Decimal("5")
    max_spread: Decimal | None = Decimal("0.10")
    discovery_title_searches: tuple[str, ...] = ("fdv", "fully diluted valuation")
    discovery_tag_slugs: tuple[str, ...] = ("crypto",)
    required_category_tokens: tuple[str, ...] = ("crypto", "cryptocurrency")
    required_event_tokens: tuple[str, ...] = ("fdv",)
    required_target_tokens: tuple[str, ...] = ("500m",)


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
