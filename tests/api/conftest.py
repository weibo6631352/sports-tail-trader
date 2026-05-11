"""API 测试共享 fixture——每个测试前清空 rate-limit 桶，避免跨用例 429。"""
from __future__ import annotations

import pytest

from polymarket_trader.api.rate_limit import _REGISTRY


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets() -> None:
    _REGISTRY._buckets.clear()
