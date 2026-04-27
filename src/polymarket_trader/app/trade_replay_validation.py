"""成交复盘与 Polymarket 持仓导出的离线核对工具。

该模块用于运营期验收：把管理 API `/trade-replays` 返回的复盘记录，与
Polymarket 页面导出或 Data API `/positions` 返回的数据做字段级对账。

它不访问交易客户端、不写数据库，也不参与交易主链路。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_trader.infra.polymarket import normalize_position_payload
from polymarket_trader.serialization import jsonable


@dataclass(frozen=True, slots=True)
class TradeReplayValidationMismatch:
    """单个复盘字段与 Polymarket 权威数据不一致。"""

    condition_id: str
    token_id: str
    field: str
    expected: Any
    actual: Any

    def as_payload(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "field": self.field,
            "expected": jsonable(self.expected),
            "actual": jsonable(self.actual),
        }


@dataclass(frozen=True, slots=True)
class TradeReplayValidationReport:
    """成交复盘核对报告。"""

    checked_records: int
    matched_positions: int
    missing_positions: tuple[dict[str, str], ...]
    mismatches: tuple[TradeReplayValidationMismatch, ...]

    @property
    def passed(self) -> bool:
        return not self.missing_positions and not self.mismatches

    def as_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_records": self.checked_records,
            "matched_positions": self.matched_positions,
            "missing_positions": list(self.missing_positions),
            "mismatches": [item.as_payload() for item in self.mismatches],
        }


def validate_trade_replays_against_positions(
    trade_replays_payload: Any,
    positions_payload: Any,
    *,
    tolerance: Decimal = Decimal("0.000001"),
) -> TradeReplayValidationReport:
    """核对 `/trade-replays` 复盘记录与 Polymarket position 导出。"""

    replays = _items(trade_replays_payload, "items", "trade_replays", "replays")
    positions = tuple(normalize_position_payload(item) for item in _items(positions_payload, "positions", "data", "items", "results", "rows"))
    positions_by_key = {
        (position.condition_id, position.token_id): position
        for position in positions
        if position.condition_id and position.token_id
    }
    missing_positions: list[dict[str, str]] = []
    mismatches: list[TradeReplayValidationMismatch] = []
    matched_positions = 0
    for replay in replays:
        condition_id = str(replay.get("condition_id") or "")
        token_id = str(replay.get("token_id") or "")
        if not condition_id or not token_id:
            continue
        position = positions_by_key.get((condition_id, token_id))
        if position is None:
            missing_positions.append({"condition_id": condition_id, "token_id": token_id})
            continue
        matched_positions += 1
        mismatches.extend(_compare_replay_position(replay, position, tolerance=tolerance))
    return TradeReplayValidationReport(
        checked_records=len(replays),
        matched_positions=matched_positions,
        missing_positions=tuple(missing_positions),
        mismatches=tuple(mismatches),
    )


def validate_trade_replay_files(
    *,
    trade_replays_path: str,
    positions_path: str,
    tolerance: Decimal = Decimal("0.000001"),
) -> TradeReplayValidationReport:
    """读取本地 JSON 文件并执行成交复盘核对。"""

    return validate_trade_replays_against_positions(
        _load_json(trade_replays_path),
        _load_json(positions_path),
        tolerance=tolerance,
    )


def _compare_replay_position(
    replay: Mapping[str, Any],
    position: Any,
    *,
    tolerance: Decimal,
) -> tuple[TradeReplayValidationMismatch, ...]:
    condition_id = str(replay.get("condition_id") or "")
    token_id = str(replay.get("token_id") or "")
    replay_position = _mapping(replay.get("position"))
    replay_pnl = _mapping(replay.get("pnl"))
    comparisons = (
        ("position.shares", replay_position.get("shares"), position.shares),
        ("position.cost_usdc", replay_position.get("cost_usdc"), position.cost_usdc),
        ("pnl.realized_pnl_usdc", replay_pnl.get("realized_pnl_usdc"), position.realized_pnl),
        ("pnl.cash_pnl_usdc", replay_pnl.get("cash_pnl_usdc"), position.cash_pnl),
        ("pnl.current_value_usdc", replay_pnl.get("current_value_usdc"), position.current_value),
        ("pnl.cur_price", replay_pnl.get("cur_price"), position.cur_price),
    )
    mismatches: list[TradeReplayValidationMismatch] = []
    for field, replay_value, position_value in comparisons:
        if replay_value is None or position_value is None:
            continue
        if abs(Decimal(str(replay_value)) - Decimal(str(position_value))) > tolerance:
            mismatches.append(
                TradeReplayValidationMismatch(
                    condition_id=condition_id,
                    token_id=token_id,
                    field=field,
                    expected=position_value,
                    actual=replay_value,
                )
            )
    replay_redeemable = replay_pnl.get("redeemable")
    if replay_redeemable is not None and position.redeemable is not None and replay_redeemable is not position.redeemable:
        mismatches.append(
            TradeReplayValidationMismatch(
                condition_id=condition_id,
                token_id=token_id,
                field="pnl.redeemable",
                expected=position.redeemable,
                actual=replay_redeemable,
            )
        )
    return tuple(mismatches)


def _items(payload: Any, *keys: str) -> tuple[Mapping[str, Any], ...]:
    if _is_record_sequence(payload):
        return tuple(item for item in payload if isinstance(item, Mapping))
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if _is_record_sequence(value):
                return tuple(item for item in value if isinstance(item, Mapping))
        return (payload,)
    return ()


def _is_record_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：核对两个 JSON 文件并输出报告。"""

    parser = argparse.ArgumentParser(description="Validate /trade-replays against Polymarket position export")
    parser.add_argument("--trade-replays", required=True, help="/trade-replays 响应 JSON 文件")
    parser.add_argument("--positions", required=True, help="Polymarket Data API 或页面导出的 positions JSON 文件")
    parser.add_argument("--tolerance", default="0.000001", help="Decimal 差异容忍度")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = validate_trade_replay_files(
        trade_replays_path=args.trade_replays,
        positions_path=args.positions,
        tolerance=Decimal(str(args.tolerance)),
    )
    print(json.dumps(report.as_payload(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - 人工验收命令入口
    raise SystemExit(main())
