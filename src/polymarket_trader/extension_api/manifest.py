from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from polymarket_trader.extension_api.hooks import ExtensionHooks, LiveStateHooks
from polymarket_trader.extension_api.ports import ExtensionPorts


@dataclass(frozen=True, slots=True)
class ExtensionSpec:
    # strategy_id 是策略身份的强字段：单一策略实例 = 单一 strategy_id。
    # 框架使用该字段作为所有 SCOPE 表（orders/fills/positions/allocations/
    # audit_events/decision_records/candidates）写入和查询过滤的归属键，
    # 因此该值一旦确定就不允许在运行时变更。
    strategy_id: str
    name: str
    version: str = "1"
    description: str = ""
    config_type: type[Any] | None = None
    capabilities: tuple[str, ...] = ()


@runtime_checkable
class BusinessExtension(Protocol):
    @property
    def spec(self) -> ExtensionSpec: ...

    @property
    def hooks(self) -> ExtensionHooks: ...

    @property
    def live_state_hooks(self) -> LiveStateHooks | None: ...


@runtime_checkable
class ConfiguredExtension(Protocol):
    """扩展可选实现：暴露策略运行时配置实例。

    框架通过该协议在组合根读取策略配置，避免 getattr duck-typing 或硬编码
    具体策略类型。实现了该协议的扩展可以让 build_runtime 和 admin_service
    从策略侧直接读 kelly_* 等策略参数，而不是走框架 Settings。
    """

    @property
    def config(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class KellyParams:
    """Kelly 仓位计算参数的框架侧值容器。

    ``resolve_kelly_params`` 从 ``ConfiguredExtension.config`` 提取；不可用时返回保守默认值。
    框架通过此类型在 ``build_runtime`` 和 ``AdminService`` 中传递 Kelly 参数，
    避免直接依赖策略包配置类型。
    """

    kelly_fraction: Decimal = Decimal("0.25")
    kelly_max_position_fraction: Decimal = Decimal("0.10")
    kelly_min_edge: Decimal = Decimal("0.02")
    kelly_min_stake_usdc: Decimal = Decimal("1")
    kelly_allow_round_up_to_market_min: bool = True
    kelly_round_up_max_overbet_ratio: Decimal = Decimal("1")
    kelly_drawdown_halt_fraction: Decimal = Decimal("0.5")


_KELLY_DEFAULTS = KellyParams()


def resolve_kelly_params(extension: BusinessExtension) -> KellyParams:
    """从 ConfiguredExtension.config 提取 Kelly 参数，不可用时返回保守默认值。"""
    config: Any = extension.config if isinstance(extension, ConfiguredExtension) else None
    if config is None:
        return _KELLY_DEFAULTS
    return KellyParams(
        kelly_fraction=getattr(config, "kelly_fraction", _KELLY_DEFAULTS.kelly_fraction),
        kelly_max_position_fraction=getattr(config, "kelly_max_position_fraction", _KELLY_DEFAULTS.kelly_max_position_fraction),
        kelly_min_edge=getattr(config, "kelly_min_edge", _KELLY_DEFAULTS.kelly_min_edge),
        kelly_min_stake_usdc=getattr(config, "kelly_min_stake_usdc", _KELLY_DEFAULTS.kelly_min_stake_usdc),
        kelly_allow_round_up_to_market_min=getattr(config, "kelly_allow_round_up_to_market_min", _KELLY_DEFAULTS.kelly_allow_round_up_to_market_min),
        kelly_round_up_max_overbet_ratio=getattr(config, "kelly_round_up_max_overbet_ratio", _KELLY_DEFAULTS.kelly_round_up_max_overbet_ratio),
        kelly_drawdown_halt_fraction=getattr(config, "kelly_drawdown_halt_fraction", _KELLY_DEFAULTS.kelly_drawdown_halt_fraction),
    )


@runtime_checkable
class ConfigValidator(Protocol):
    """扩展可选实现：在框架启动期对策略侧配置进行联合校验。

    框架已经在 ``Settings.validate_startup_readiness()`` 中检查了金额、密钥、
    扩展模块路径等通用配置；扩展实现该协议后，可以在 ``build_runtime`` 阶段
    额外校验“策略本身是否能正常工作所需要的最小配置”，例如 discovery_queries
    是否非空、scale-in 参数是否合理等。

    返回的每条 ``ConfigIssue`` 都视为阻塞启动；空元组表示通过。
    """

    def validate_config(self, settings: Any) -> "tuple[Any, ...]": ...


class ExtensionFactory(Protocol):
    def __call__(
        self,
        *,
        ports: ExtensionPorts | None = None,
        config_path: str | None = None,
    ) -> BusinessExtension: ...


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    name: str
    version: str
    module_path: str
    factory: ExtensionFactory
    config_type: type[Any] | None = None
    capabilities: tuple[str, ...] = ()
