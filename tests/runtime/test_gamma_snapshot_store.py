"""GammaMarketSnapshotStore 行为契约。

覆盖：
- upsert 单条 / 批量、condition_id 缺失时被忽略
- 读取 lock-free（直接返回 dict 引用查找）
- ``is_fresh`` 按时间窗口判定
- ``prune`` 同步清理，幂等
- ``get_dto_if_fresh`` 综合：stale 或 missing 都返回 None，让 caller 走 fallback
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.runtime.gamma_snapshot_store import GammaMarketSnapshotStore


@dataclass
class _StubGammaMarketDTO:
    """最小 stub：只有 condition_id（store 不关心其他字段）。"""

    condition_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    tick_size: Decimal | None = None


def test_upsert_writes_dto_and_get_returns_entry() -> None:
    store = GammaMarketSnapshotStore()
    dto = _StubGammaMarketDTO(condition_id="cond-1")
    store.upsert(dto)

    entry = store.get("cond-1")
    assert entry is not None
    assert entry.dto is dto
    assert entry.last_updated_at_mono > 0


def test_upsert_ignores_dto_with_empty_condition_id() -> None:
    """condition_id=None/空时 silently skip——避免 dict 出现垃圾 key。"""
    store = GammaMarketSnapshotStore()
    store.upsert(_StubGammaMarketDTO(condition_id=None))
    store.upsert(_StubGammaMarketDTO(condition_id=""))
    assert store.size() == 0


def test_upsert_many_batch_writes_only_valid_entries_and_returns_count() -> None:
    store = GammaMarketSnapshotStore()
    dtos = (
        _StubGammaMarketDTO(condition_id="cond-1"),
        _StubGammaMarketDTO(condition_id=None),  # skipped
        _StubGammaMarketDTO(condition_id="cond-2"),
    )
    count = store.upsert_many(dtos)
    assert count == 2
    assert store.size() == 2
    assert store.get("cond-1") is not None
    assert store.get("cond-2") is not None


def test_get_dto_if_fresh_returns_none_for_missing() -> None:
    store = GammaMarketSnapshotStore()
    assert store.get_dto_if_fresh("cond-missing", max_age_s=10.0) is None


def test_get_dto_if_fresh_returns_none_for_stale_entry() -> None:
    """stale 数据返回 None，让 caller 走 fallback 直查 gamma。"""
    store = GammaMarketSnapshotStore()
    dto = _StubGammaMarketDTO(condition_id="cond-old")
    store.upsert(dto)
    # 假装时间过去了
    time.sleep(0.05)
    assert store.get_dto_if_fresh("cond-old", max_age_s=0.01) is None


def test_get_dto_if_fresh_returns_dto_when_within_window() -> None:
    store = GammaMarketSnapshotStore()
    dto = _StubGammaMarketDTO(condition_id="cond-fresh")
    store.upsert(dto)
    assert store.get_dto_if_fresh("cond-fresh", max_age_s=60.0) is dto


def test_prune_removes_entry_and_is_idempotent() -> None:
    store = GammaMarketSnapshotStore()
    store.upsert(_StubGammaMarketDTO(condition_id="cond-x"))
    assert store.size() == 1
    store.prune("cond-x")
    assert store.size() == 0
    # 第二次 prune 已经不存在的 condition——no-op，不抛
    store.prune("cond-x")
    store.prune("never-existed")
    assert store.size() == 0


def test_upsert_again_refreshes_timestamp() -> None:
    store = GammaMarketSnapshotStore()
    dto_v1 = _StubGammaMarketDTO(condition_id="cond-1")
    store.upsert(dto_v1)
    first_ts = store.get("cond-1").last_updated_at_mono  # type: ignore[union-attr]
    time.sleep(0.01)
    dto_v2 = _StubGammaMarketDTO(condition_id="cond-1", tick_size=Decimal("0.01"))
    store.upsert(dto_v2)
    second_entry = store.get("cond-1")
    assert second_entry is not None
    assert second_entry.dto is dto_v2  # 最新覆盖
    assert second_entry.last_updated_at_mono > first_ts
