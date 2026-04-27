from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from polymarket_trader.extension_api.config_loader import load_extension_config
from polymarket_trader.extension_api.errors import ExtensionLoadError


@dataclass(frozen=True, slots=True)
class LoaderConfig:
    threshold: Decimal


def test_load_extension_config_coerces_decimal_from_json_string(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"threshold": "0.42"}', encoding="utf-8")

    config = load_extension_config(LoaderConfig, str(config_path))

    assert config is not None
    assert config.threshold == Decimal("0.42")
    assert isinstance(config.threshold, Decimal)


def test_load_extension_config_wraps_validation_errors(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"threshold": "not-a-number"}', encoding="utf-8")

    with pytest.raises(ExtensionLoadError):
        load_extension_config(LoaderConfig, str(config_path))
