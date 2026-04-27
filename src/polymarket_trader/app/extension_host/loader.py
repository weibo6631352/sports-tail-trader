from __future__ import annotations

from importlib import import_module
from types import ModuleType

from polymarket_trader.extension_api import BusinessExtension, ExtensionLoadError, ExtensionManifest, ExtensionPorts


def load_extension(
    *,
    module_path: str,
    ports: ExtensionPorts | None = None,
    config_path: str | None = None,
) -> BusinessExtension:
    manifest = load_extension_manifest(module_path)
    extension = manifest.factory(ports=ports, config_path=config_path)
    if not isinstance(extension, BusinessExtension):
        raise ExtensionLoadError(
            f"extension factory '{manifest.module_path}' did not return a BusinessExtension-compatible object"
        )
    return extension


def load_extension_manifest(module_path: str) -> ExtensionManifest:
    module = import_module(module_path)
    manifest = _extract_manifest(module)
    if manifest is not None:
        return manifest
    try:
        manifest_module = import_module(f"{module_path}.manifest")
    except ModuleNotFoundError as exc:
        raise ExtensionLoadError(
            f"extension module '{module_path}' does not expose a manifest"
        ) from exc
    manifest = _extract_manifest(manifest_module)
    if manifest is None:
        raise ExtensionLoadError(f"extension module '{module_path}' does not expose a valid manifest")
    return manifest


def _extract_manifest(module: ModuleType) -> ExtensionManifest | None:
    manifest = getattr(module, "manifest", None)
    if isinstance(manifest, ExtensionManifest):
        return manifest
    return None
