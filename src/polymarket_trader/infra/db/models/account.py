from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _decimal,
    _ensure_aware,
    _json_mapping,
    _json_safe,
    _market_pause_payloads,
    _market_pauses_from_payload,
    _utc_now,
)


class AccountSnapshotModel(Base, TimestampMixin):
    """账户余额、买入闸门和净值快照。

    append-only 时间序列：每次写入都是一行 ``(account_key, recorded_at)`` 复合键
    的新记录。恢复路径按 ``recorded_at DESC LIMIT 1`` 读取最新一行；
    ``GET /portfolio/equity-curve`` 等审计路径按时间窗 + downsampling 聚合。
    ``net_value_usdc`` 在写入时一次性算好（``balance + Σ(position.current_value)``），
    避免事后回放还要依赖历史 mark 价格。
    """

    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
        index=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(255), index=True)
    balance_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    allowance_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    net_value_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    user_ws_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_new_entries: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    market_pauses: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_account_snapshots_account_recorded", "account_key", "recorded_at"),
    )

    @classmethod
    def from_domain(
        cls,
        snapshot: AccountSnapshot,
        *,
        trace_id: str | None = None,
        raw_payload: JsonMapping | None = None,
        account_key: str = "primary",
        recorded_at: datetime | None = None,
        net_value_usdc: Decimal | None = None,
    ) -> "AccountSnapshotModel":
        net_value = (
            net_value_usdc
            if net_value_usdc is not None
            else _net_value_from_snapshot(snapshot)
        )
        recorded = _ensure_aware(recorded_at) if recorded_at is not None else _utc_now()
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "account_key": account_key,
            "trace_id": trace_id,
            "recorded_at": _json_safe(recorded),
            "balance_usdc": str(snapshot.balance_usdc),
            "allowance_usdc": str(snapshot.allowance_usdc),
            "net_value_usdc": str(net_value),
            "user_ws_connected": snapshot.user_ws_connected,
            "allow_new_entries": snapshot.allow_new_entries,
            "market_pauses": [pause.as_payload() for pause in snapshot.market_pauses],
            "last_reconcile_at": _json_safe(snapshot.last_reconcile_at),
        }
        return cls(
            account_key=account_key,
            recorded_at=recorded,
            trace_id=trace_id,
            balance_usdc=snapshot.balance_usdc,
            allowance_usdc=snapshot.allowance_usdc,
            net_value_usdc=net_value,
            user_ws_connected=snapshot.user_ws_connected,
            allow_new_entries=snapshot.allow_new_entries,
            market_pauses=_market_pause_payloads(snapshot.market_pauses),
            last_reconcile_at=snapshot.last_reconcile_at,
            raw_payload=payload,
        )

    def to_domain(self) -> AccountSnapshot:
        return AccountSnapshot(
            balance_usdc=_decimal(self.balance_usdc) or Decimal("0"),
            allowance_usdc=_decimal(self.allowance_usdc) or Decimal("0"),
            user_ws_connected=bool(self.user_ws_connected),
            allow_new_entries=bool(self.allow_new_entries),
            market_pauses=_market_pauses_from_payload(self.market_pauses),
            last_reconcile_at=(
                None if self.last_reconcile_at is None else _ensure_aware(self.last_reconcile_at)
            ),
        )


def _net_value_from_snapshot(snapshot: AccountSnapshot) -> Decimal:
    """按 ``balance + Σ(position.current_value)`` 计算净值。

    历史净值在写入时定格——不再依赖外部历史 mark 价格，否则无法回放。
    缺失 ``current_value`` 的仓位按 0 计；reconciler 会持续刷新 mark 价格。
    """

    total = snapshot.balance_usdc or Decimal("0")
    for position in snapshot.positions:
        value = position.current_value
        if value is None:
            continue
        total += value
    return total


__all__ = ["AccountSnapshotModel"]
