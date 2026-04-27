from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from polymarket_trader.extension_api.hooks import ExtensionHooks
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
