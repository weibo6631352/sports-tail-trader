from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from polymarket_trader.extension_api import (
    load_extension_config as exported_load_extension_config,
)
from polymarket_trader.extension_api.config_loader import (
    load_extension_config,
    load_mapping_file,
)
from polymarket_trader.extension_api.errors import ExtensionLoadError


@dataclass(frozen=True, slots=True)
class _DemoConfig:
    name: str
    enabled: bool = False


def test_load_mapping_file_supports_json_and_toml(tmp_path: Path) -> None:
    json_path = tmp_path / "extension.json"
    json_path.write_text('{"name": "json-demo", "enabled": true}\n', encoding="utf-8")
    toml_path = tmp_path / "extension.toml"
    toml_path.write_text('name = "toml-demo"\nenabled = true\n', encoding="utf-8")

    assert load_mapping_file(json_path)["name"] == "json-demo"
    assert load_mapping_file(toml_path)["name"] == "toml-demo"


def test_load_extension_config_builds_dataclass(tmp_path: Path) -> None:
    config_path = tmp_path / "extension.json"
    config_path.write_text('{"name": "demo", "enabled": true, "ignored": 1}\n', encoding="utf-8")

    config = load_extension_config(_DemoConfig, str(config_path))

    assert config == _DemoConfig(name="demo", enabled=True)


def test_load_mapping_file_rejects_unsupported_suffix(tmp_path: Path) -> None:
    config_path = tmp_path / "extension.yaml"
    config_path.write_text("name: demo\n", encoding="utf-8")

    with pytest.raises(ExtensionLoadError):
        load_mapping_file(config_path)


def test_extension_api_exports_config_loader_helpers() -> None:
    assert exported_load_extension_config is load_extension_config
