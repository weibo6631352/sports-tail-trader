"""通用辅助：从 ExtensionContext.metadata 中读 Decimal / 文本。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.extension_api import ExtensionContext


def _metadata_decimal(context: ExtensionContext, *keys: str) -> Decimal | None:
    """按优先顺序从 metadata 中读取十进制数值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is None:
            continue
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None


def _metadata_text(context: ExtensionContext, *keys: str) -> str | None:
    """按优先顺序从 metadata 中读取文本值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is not None:
            return str(value)
    return None
