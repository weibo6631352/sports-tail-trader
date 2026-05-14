from __future__ import annotations

from dataclasses import dataclass
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
