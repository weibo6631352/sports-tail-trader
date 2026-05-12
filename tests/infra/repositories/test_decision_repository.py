"""``DecisionRecordRepository`` 补充覆盖。

``tests/infra/test_decision_record_repository.py`` 已覆盖 ORM 字段映射、
``list_decisions_snapshot`` 的 condition/accepted/time-range/trace 过滤
与 record_id 幂等。这里补 ``get_by_record_id`` 与分页 / strategy_id
过滤，避免聚合拆分后单点查询和分页边界回归。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from polymarket_trader.domain.decisions import DecisionRecord


def _make_record(**overrides: Any) -> DecisionRecord:
    base: dict[str, Any] = {
        "strategy_id": "sports_tail",
        "trace_id": "trace-x",
        "condition_id": "cond-x",
        "hook_name": "decide_entry",
        "token_id": "tok-x",
        "market_slug": "slug-x",
        "decision_input": {"market": {"slug": "slug-x"}},
        "decision_output": {"action": "skip", "reason": "no_signal"},
        "accepted": False,
        "reason": "no_signal",
    }
    base.update(overrides)
    return DecisionRecord(**base)


@pytest.mark.pg
async def test_decision_repository_get_by_record_id_returns_single_row(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import DecisionRecordRepository
    from polymarket_trader.infra.db.models import DecisionRecordModel

    target = _make_record(record_id="rec-target")
    others = [_make_record(record_id=f"rec-{i}") for i in range(3)]

    async with pg_session_factory() as session:
        await session.execute(delete(DecisionRecordModel))
        repo = DecisionRecordRepository(session)
        await repo.save_decision_records([target, *others])
        await session.commit()

    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        fetched = await repo.get_by_record_id("rec-target")
        missing = await repo.get_by_record_id("does-not-exist")

    assert fetched is not None and fetched.record_id == "rec-target"
    assert missing is None


@pytest.mark.pg
async def test_decision_repository_pagination_honors_limit_offset_and_ordering(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import DecisionRecordRepository
    from polymarket_trader.infra.db.models import DecisionRecordModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    seeds = [
        _make_record(record_id=f"rec-{idx}", created_at=base - timedelta(minutes=idx))
        for idx in range(5)
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(DecisionRecordModel))
        repo = DecisionRecordRepository(session)
        await repo.save_decision_records(seeds)
        await session.commit()

    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        first_page = await repo.list_decisions_snapshot(limit=2, offset=0)
        second_page = await repo.list_decisions_snapshot(limit=2, offset=2)

    assert first_page.total == 5
    assert first_page.limit == 2 and first_page.offset == 0
    assert len(first_page.items) == 2
    # 默认按 created_at desc：第一页是最新两条（rec-0, rec-1）
    assert {r.record_id for r in first_page.items} == {"rec-0", "rec-1"}
    assert {r.record_id for r in second_page.items} == {"rec-2", "rec-3"}


@pytest.mark.pg
async def test_decision_repository_filters_by_strategy_id(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import DecisionRecordRepository
    from polymarket_trader.infra.db.models import DecisionRecordModel

    keep = _make_record(record_id="rec-keep", strategy_id="sports_tail")
    other = _make_record(record_id="rec-other", strategy_id="other_strategy")

    async with pg_session_factory() as session:
        await session.execute(delete(DecisionRecordModel))
        repo = DecisionRecordRepository(session)
        await repo.save_decision_records([keep, other])
        await session.commit()

    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        page = await repo.list_decisions_snapshot(strategy_id="sports_tail")

    assert page.total == 1
    assert page.items[0].record_id == "rec-keep"
