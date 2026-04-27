from __future__ import annotations

from dataclasses import fields, is_dataclass
import json
from pathlib import Path
import tomllib
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from polymarket_trader.extension_api.errors import ExtensionLoadError

T = TypeVar("T")


def load_mapping_file(config_path: str) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise ExtensionLoadError(f"extension file not found: {path}")
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        raise ExtensionLoadError(
            f"unsupported extension file format '{path.suffix or '<none>'}', expected .json or .toml"
        )
    if not isinstance(data, dict):
        raise ExtensionLoadError(f"extension file must contain an object at top level: {path}")
    return data


def load_extension_config(config_type: type[T], config_path: str | None) -> T | None:
    if config_path is None:
        return None
    if not is_dataclass(config_type):
        raise ExtensionLoadError(f"extension config type must be a dataclass: {config_type!r}")
    data = load_mapping_file(config_path)
    allowed = {field.name for field in fields(config_type)}
    values = {key: value for key, value in data.items() if key in allowed}
    try:
        return TypeAdapter(config_type).validate_python(values)
    except ValidationError as exc:
        raise ExtensionLoadError(
            f"failed to build config {config_type.__name__} from {config_path}: {exc}"
        ) from exc
