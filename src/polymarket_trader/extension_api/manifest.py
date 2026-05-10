from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from polymarket_trader.extension_api.hooks import ExtensionHooks, LiveStateHooks
from polymarket_trader.extension_api.ports import ExtensionPorts


@dataclass(frozen=True, slots=True)
class ExtensionSpec:
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
