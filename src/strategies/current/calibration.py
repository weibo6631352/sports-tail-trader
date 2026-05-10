"""体育扫尾策略阈值校准工具。

本模块把 Moneyline、Spreads 和 Totals 的历史/只记录样本统一评估成校准报告。
它只复用当前策略的纯业务评估函数，不改运行时配置、不下单、不写数据库。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from polymarket_trader.extension_api import load_mapping_file
from polymarket_trader.serialization import jsonable
from strategies.current.tail import (
    ExecutionPermission,
    LiveGameState,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    TailEvaluation,
    TailPolicy,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)


@dataclass(frozen=True, slots=True)
class CalibrationCaseResult:
    """单个样本在一组阈值下的评估结果。"""

    case_id: str
    market_type: str
    side: str
    accepted: bool
    action: str
    reason: str
    execution_permission: str | None
    expected_accept: bool | None
    labeled_pnl_usdc: Decimal | None
    metadata: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        """返回可序列化样本结果。"""

        return {
            "case_id": self.case_id,
            "market_type": self.market_type,
            "side": self.side,
            "accepted": self.accepted,
            "action": self.action,
            "reason": self.reason,
            "execution_permission": self.execution_permission,
            "expected_accept": self.expected_accept,
            "labeled_pnl_usdc": None if self.labeled_pnl_usdc is None else str(self.labeled_pnl_usdc),
            "metadata": jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CalibrationVariantReport:
    """一组策略阈值对应的批量校准报告。"""

    name: str
    policy: TailPolicy
    case_results: tuple[CalibrationCaseResult, ...]
    reason_counts: Mapping[str, int]
    by_market_type: Mapping[str, Mapping[str, Any]]

    @property
    def accepted_count(self) -> int:
        return sum(1 for item in self.case_results if item.accepted)

    @property
    def rejected_count(self) -> int:
        return len(self.case_results) - self.accepted_count

    @property
    def accepted_labeled_pnl_usdc(self) -> Decimal:
        return sum(
            (item.labeled_pnl_usdc for item in self.case_results if item.accepted and item.labeled_pnl_usdc is not None),
            Decimal("0"),
        )

    def as_payload(self) -> dict[str, Any]:
        """返回可序列化校准报告。"""

        return {
            "name": self.name,
            "policy": _policy_payload(self.policy),
            "total_cases": len(self.case_results),
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "accepted_labeled_pnl_usdc": str(self.accepted_labeled_pnl_usdc),
            "reason_counts": dict(self.reason_counts),
            "by_market_type": jsonable(self.by_market_type),
            "case_results": [item.as_payload() for item in self.case_results],
        }


@dataclass(frozen=True, slots=True)
class SportsTailCalibrationReport:
    """体育扫尾批量阈值校准报告。"""

    rule_version: str
    generated_at: datetime
    variants: tuple[CalibrationVariantReport, ...]

    @property
    def best_variant(self) -> str | None:
        """按已标注样本盈亏优先、通过数次之返回当前最佳阈值组名。"""

        if not self.variants:
            return None
        best = max(
            self.variants,
            key=lambda item: (item.accepted_labeled_pnl_usdc, item.accepted_count),
        )
        return best.name

    def as_payload(self) -> dict[str, Any]:
        """返回可序列化校准报告。"""

        return {
            "rule_version": self.rule_version,
            "generated_at": self.generated_at.isoformat(),
            "best_variant": self.best_variant,
            "variants": [variant.as_payload() for variant in self.variants],
        }


def run_tail_calibration(
    sample: Mapping[str, Any],
    *,
    base_policy: TailPolicy | None = None,
) -> SportsTailCalibrationReport:
    """运行 Moneyline/Spreads/Totals 批量校准。

    ``sample`` 支持：
    - ``cases``：样本列表，每个样本包含 ``game``、``market`` 和可选 ``label``。
    - ``policy_variants``：阈值组列表，每组用 ``overrides`` 覆盖 ``TailPolicy``。
    """

    base_policy = base_policy or TailPolicy()
    variants = _policy_variants(sample, base_policy)
    cases = _mapping_list(sample.get("cases"))
    generated_at = _datetime_value(sample.get("generated_at")) or datetime.now(timezone.utc)
    return SportsTailCalibrationReport(
        rule_version=str(sample.get("rule_version") or "tail.current"),
        generated_at=generated_at,
        variants=tuple(_run_variant(name, policy, cases, sample=sample) for name, policy in variants),
    )


def run_tail_calibration_file(path: str) -> SportsTailCalibrationReport:
    """从 JSON/TOML 文件运行阈值校准。"""

    return run_tail_calibration(load_mapping_file(path))


def _run_variant(
    name: str,
    policy: TailPolicy,
    cases: Sequence[Mapping[str, Any]],
    *,
    sample: Mapping[str, Any],
) -> CalibrationVariantReport:
    results = tuple(_evaluate_case(case, policy=policy, sample=sample) for case in cases)
    return CalibrationVariantReport(
        name=name,
        policy=policy,
        case_results=results,
        reason_counts=_reason_counts(results),
        by_market_type=_market_type_summary(results),
    )


def _evaluate_case(
    case: Mapping[str, Any],
    *,
    policy: TailPolicy,
    sample: Mapping[str, Any],
) -> CalibrationCaseResult:
    game = _load_game(case)
    market = _load_market_snapshot(case)
    now = _datetime_value(case.get("now")) or _datetime_value(sample.get("now")) or (
        game.observed_at if game is not None else None
    )
    evaluation = evaluate_tail_opportunity(game, market, policy=policy, now=now)
    label = _mapping(case.get("label"))
    expected_accept = _optional_bool(label.get("should_accept"))
    return CalibrationCaseResult(
        case_id=str(case.get("case_id") or case.get("id") or f"{market.market_type.value}:{market.token_id}"),
        market_type=market.market_type.value,
        side=market.side.value,
        accepted=evaluation.accepted,
        action=evaluation.action.value,
        reason=evaluation.reason,
        execution_permission=None
        if evaluation.execution_permission is None
        else evaluation.execution_permission.value,
        expected_accept=expected_accept,
        labeled_pnl_usdc=_optional_decimal(label.get("pnl_usdc") or label.get("profit_usdc")),
        metadata=_case_metadata(game, market, evaluation, label),
    )


def _load_game(case: Mapping[str, Any]) -> LiveGameState | None:
    game_payload = _mapping(case.get("game"))
    return live_game_state_from_metadata({"live_game": game_payload})


def _load_market_snapshot(case: Mapping[str, Any]) -> SportsMarketSnapshot:
    market_payload = _mapping(case.get("market")) or case
    return SportsMarketSnapshot(
        market_type=SportsMarketType(str(market_payload.get("market_type") or SportsMarketType.TOTALS.value)),
        side=SportsMarketSide(str(market_payload.get("side") or SportsMarketSide.OVER.value)),
        token_id=str(market_payload.get("token_id") or case.get("token_id") or ""),
        line=_optional_decimal(market_payload.get("line")),
        best_ask=_optional_decimal(market_payload.get("best_ask")),
        buyable_liquidity_usdc=_optional_decimal(market_payload.get("buyable_liquidity_usdc")) or Decimal("0"),
        market_slug=_optional_text(market_payload.get("market_slug")),
        metadata=market_payload,
    )


def _case_metadata(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    evaluation: TailEvaluation,
    label: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "league": None if game is None else game.league,
        "home_score": None if game is None else game.home_score,
        "away_score": None if game is None else game.away_score,
        "seconds_remaining": None if game is None else game.seconds_remaining,
        "line": None if market.line is None else str(market.line),
        "best_ask": None if market.best_ask is None else str(market.best_ask),
        "buyable_liquidity_usdc": str(market.buyable_liquidity_usdc),
        "label": jsonable(label),
        "evaluation": jsonable(evaluation.metadata),
    }


def _policy_variants(
    sample: Mapping[str, Any],
    base_policy: TailPolicy,
) -> tuple[tuple[str, TailPolicy], ...]:
    variants = _mapping_list(sample.get("policy_variants"))
    if not variants:
        overrides = _mapping(sample.get("policy"))
        return (("default", _policy_from_overrides(base_policy, overrides)),)
    resolved: list[tuple[str, TailPolicy]] = []
    for index, variant in enumerate(variants):
        name = str(variant.get("name") or f"variant_{index + 1}")
        overrides = _mapping(variant.get("overrides") or variant.get("policy"))
        resolved.append((name, _policy_from_overrides(base_policy, overrides)))
    return tuple(resolved)


def _policy_from_overrides(
    base_policy: TailPolicy,
    overrides: Mapping[str, Any],
) -> TailPolicy:
    supported = {field.name: getattr(base_policy, field.name) for field in fields(TailPolicy)}
    values: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in supported:
            continue
        current = supported[key]
        values[key] = _coerce_policy_value(value, current)
    return replace(base_policy, **values)


def _coerce_policy_value(value: Any, current: Any) -> Any:
    if isinstance(current, Decimal):
        return Decimal(str(value))
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, ExecutionPermission):
        return ExecutionPermission(str(value))
    if isinstance(current, tuple) and current and isinstance(current[0], SportsMarketType):
        items = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else (value,)
        return tuple(SportsMarketType(str(item)) for item in items)
    return value


def _reason_counts(results: Sequence[CalibrationCaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts


def _market_type_summary(results: Sequence[CalibrationCaseResult]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for item in results:
        bucket = summary.setdefault(
            item.market_type,
            {
                "total": 0,
                "accepted": 0,
                "rejected": 0,
                "true_positive": 0,
                "false_positive": 0,
                "true_negative": 0,
                "false_negative": 0,
                "accepted_labeled_pnl_usdc": Decimal("0"),
            },
        )
        bucket["total"] += 1
        bucket["accepted" if item.accepted else "rejected"] += 1
        if item.expected_accept is True and item.accepted:
            bucket["true_positive"] += 1
        elif item.expected_accept is False and item.accepted:
            bucket["false_positive"] += 1
        elif item.expected_accept is False and not item.accepted:
            bucket["true_negative"] += 1
        elif item.expected_accept is True and not item.accepted:
            bucket["false_negative"] += 1
        if item.accepted and item.labeled_pnl_usdc is not None:
            bucket["accepted_labeled_pnl_usdc"] += item.labeled_pnl_usdc
    return {
        market_type: {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in bucket.items()
        }
        for market_type, bucket in summary.items()
    }


def _policy_payload(policy: TailPolicy) -> dict[str, Any]:
    return {
        field.name: jsonable(getattr(policy, field.name))
        for field in fields(TailPolicy)
    }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    return None


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：输出阈值校准 JSON 报告。"""

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m strategies.current.calibration <fixture.json>", file=sys.stderr)
        return 2
    report = run_tail_calibration_file(args[0])
    print(json.dumps(report.as_payload(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口由人工运行
    raise SystemExit(main())
