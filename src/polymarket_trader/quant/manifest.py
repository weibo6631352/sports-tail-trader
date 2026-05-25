"""当前默认策略的 manifest。

框架的 extension loader 会通过 manifest 找到扩展模块路径和工厂函数。
"""

from __future__ import annotations

from polymarket_trader.extension_api import ExtensionManifest

from polymarket_trader.quant.config import CurrentStrategyConfig
from polymarket_trader.quant.strategy import build_strategy

# manifest 本身只描述“如何装配该策略”，不承载业务规则。
manifest = ExtensionManifest(
    name="current",
    version="1",
    module_path="polymarket_trader.quant",
    factory=build_strategy,
    config_type=CurrentStrategyConfig,
)
