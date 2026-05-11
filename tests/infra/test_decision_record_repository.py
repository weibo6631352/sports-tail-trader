"""``DecisionRecordRepository`` round-trip / query 覆盖。

单元层（默认 ``pytest -q``）：只验证 ORM 模型 ``from_domain`` / ``to_domain``
保留 schema 与不变量。PG 集成层（``pytest -q --pg``）：跑真实 ``save_*`` 和
``list_decisions_snapshot``，断言过滤、时间窗口与 ``accepted`` 标志在 SQL
层语义正确。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import DecisionRecordModel


def _make_record(**overrides: Any) -> DecisionRecord:
    base: dict[str, Any] = {
        "strategy_id": "sports_tail",
        "trace_id": "trace-1",
        "condition_id": "cond-1",
        "hook_name": "decide_entry",
        "token_id": "tok-1",
        "market_slug": "slug-1",
        "decision_input": {"market": {"slug": "slug-1"}, "trace_id": "trace-1"},
        "decision_output": {"action": "skip", "reason": "no_signal"},
        "accepted": False,
        "reason": "no_signal",
    }
    base.update(overrides)
    return DecisionRecord(**base)


def test_decision_record_model_round_trip_preserves_fields() -> None:
    original = _make_record()
    model = DecisionRecordModel.from_domain(original)
    restored = model.to_domain()

    assert restored.record_id == original.record_id
    assert restored.trace_id == original.trace_id
    assert restored.condition_id == original.condition_id
    assert restored.hook_name == original.hook_name
    assert restored.token_id == original.token_id
    assert restored.market_slug == original.market_slug
    assert restored.accepted is False
    assert restored.reason == "no_signal"
    assert dict(restored.decision_input) == dict(original.decision_input)
    assert dict(restored.decision_output) == dict(original.decision_output)


def test_decision_record_model_drops_empty_hook_name() -> None:
    record = _make_record(hook_name="")
    model = DecisionRecordModel.from_domain(record)
    assert model.hook_name is None


@pytest.mark.pg
async def test_decision_repository_filters_by_time_window_and_accepted(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import DecisionRecordRepository
    from polymarket_trader.infra.db.models import DecisionRecordModel as _Model

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    seeds = [
        _make_record(
            trace_id="t-old",
            condition_id="cond-A",
            accepted=False,
            reason="no_signal",
            decision_output={"action": "skip", "reason": "no_signal"},
            created_at=base - timedelta(hours=2),
        ),
        _make_record(
            trace_id="t-mid",
            condition_id="cond-A",
            accepted=True,
            reason=None,
            decision_output={"action": "buy", "price": "0.5", "amount_usdc": "3"},
            created_at=base - timedelta(minutes=30),
        ),
        _make_record(
            trace_id="t-new",
            condition_id="cond-B",
            accepted=False,
            reason="risk_blocked",
            decision_output={"action": "skip", "reason": "risk_blocked"},
            created_at=base,
        ),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(_Model))
        repo = DecisionRecordRepository(session)
        await repo.save_decision_records(seeds)
        await session.commit()

    # condition_id filter
    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        page = await repo.list_decisions_snapshot(condition_id="cond-A")
        assert page.total == 2
        assert {r.trace_id for r in page.items} == {"t-old", "t-mid"}

    # accepted filter
    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        page = await repo.list_decisions_snapshot(accepted=True)
        assert page.total == 1
        assert page.items[0].trace_id == "t-mid"

    # time range filter (only 'mid' and 'new')
    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        since_ms = int((base - timedelta(hours=1)).timestamp() * 1000)
        page = await repo.list_decisions_snapshot(time_range=TimeRange(since_ms=since_ms))
        assert {r.trace_id for r in page.items} == {"t-mid", "t-new"}

    # trace_id filter
    async with pg_session_factory() as session:
        repo = DecisionRecordRepository(session)
        page = await repo.list_decisions_snapshot(trace_id="t-new")
        assert page.total == 1
        assert page.items[0].condition_id == "cond-B"


@pytest.mark.pg
async def test_decision_repository_save_is_idempotent_on_record_id(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete, select, func

    from polymarket_trader.infra.db import DecisionRecordRepository
    from polymarket_trader.infra.db.models import DecisionRecordModel as _Model

    record = _make_record(record_id="rid-duplicate")

    async with pg_session_factory() as session:
        await session.execute(delete(_Model))
        repo = DecisionRecordRepository(session)
        await repo.save_decision_records([record, record])  # 同一 record_id
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(_Model.id)))
    assert total == 1
