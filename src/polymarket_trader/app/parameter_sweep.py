"""离线参数 sweep：给定一组候选阈值，对历史决策回放算 hypothetical PnL。

回答："如果当时把 min_edge_bps 调到 200、max_entry_price 调到 0.85、liquidity
门槛降到 5 USDC，过去这一段时间的总 PnL 会是多少？"——配合
``missed_opportunities`` / ``edge_realization`` / ``calibration`` 三个端点
形成完整的策略调优闭环。

约束：
- **只读、无副作用**：不下单、不改 ``ParameterStore``、不修改任何 DB 行。
- 候选维度笛卡尔积上限 ``MAX_GRID_COMBINATIONS=1000``——避免运维误传几百维
  把响应体撑爆和 CPU 死循环。
- 每条决策需要至少 ``fair_value`` + ``entry_price``；缺这俩字段的决策
  ``status=unscorable``，不计入 PnL 但保留在 sample 计数里供诊断。
- 命中 ``would_have_entered`` 的决策若 condition_id 还没有 ``market_settled``
  事件，PnL=None、计入 ``pending_unsettled`` 桶。

设计：纯函数 ``build_parameter_sweep``——caller 自行决定 DB join 边界。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from polymarket_trader.app.admin_serialization import decimal_text
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


MAX_GRID_COMBINATIONS = 1_000

# sweep 允许调的参数白名单——按 strategy parameter_store 注册值的子集挑出来。
# 不开放结算 / 退出参数因为它们影响的是离场端，本模块只算入场后到结算的总 PnL。
_SUPPORTED_PARAMETERS: dict[str, str] = {
    "tail_outright_min_edge_bps": "int",
    "tail_outright_max_entry_price": "decimal",
    "tail_outright_min_orderbook_depth_usdc": "decimal",
    "entry_no_price_max": "decimal",
}


def supported_parameter_keys() -> tuple[str, ...]:
    return tuple(sorted(_SUPPORTED_PARAMETERS.keys()))


@dataclass(frozen=True, slots=True)
class _Resolved:
    fair_value: Decimal
    entry_price: Decimal
    liquidity_usdc: Decimal | None
    predicted_edge_bps: Decimal


@dataclass(frozen=True, slots=True)
class SweepCandidateResult:
    parameters: dict[str, Any]
    would_have_entered_count: int
    settled_count: int
    pending_count: int
    win_count: int
    loss_count: int
    hypothetical_pnl_usdc: Decimal
    sum_win_pnl_usdc: Decimal
    sum_loss_pnl_usdc: Decimal

    @property
    def win_rate(self) -> Decimal | None:
        if self.settled_count == 0:
            return None
        return Decimal(self.win_count) / Decimal(self.settled_count)

    @property
    def mean_pnl_per_entered(self) -> Decimal | None:
        if self.would_have_entered_count == 0:
            return None
        return self.hypothetical_pnl_usdc / Decimal(self.would_have_entered_count)

    def as_payload(self) -> dict[str, Any]:
        return {
            "parameters": _stringify_params(self.parameters),
            "would_have_entered_count": self.would_have_entered_count,
            "settled_count": self.settled_count,
            "pending_unsettled_count": self.pending_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "hypothetical_pnl_usdc": decimal_text(self.hypothetical_pnl_usdc),
            "sum_win_pnl_usdc": decimal_text(self.sum_win_pnl_usdc),
            "sum_loss_pnl_usdc": decimal_text(self.sum_loss_pnl_usdc),
            "win_rate": (
                None
                if self.win_rate is None
                else f"{float(self.win_rate):.6f}"
            ),
            "mean_pnl_per_entered_usdc": decimal_text(self.mean_pnl_per_entered),
        }


def build_parameter_sweep(
    *,
    decisions: Sequence[DecisionRecord],
    settlements: Sequence[AuditEvent],
    candidates: Mapping[str, Sequence[Any]],
    per_decision_usdc: Decimal = Decimal("10"),
) -> dict[str, Any]:
    """主入口。校验候选 → 解析决策 → 枚举笛卡尔积 → 排名。

    返回 payload 含：
    - ``candidate_count`` 笛卡尔积维度
    - ``decision_sample_count`` 输入决策数
    - ``scorable_decision_count`` 能解析出 fair_value+entry_price 的决策数
    - ``results`` 全部候选打分结果（按 ``hypothetical_pnl_usdc`` 降序）
    - ``best_by_pnl`` / ``best_by_win_rate`` top-1 索引
    """

    _validate_candidates(candidates)
    grid = _expand_grid(candidates)
    if len(grid) > MAX_GRID_COMBINATIONS:
        raise ValueError(
            f"candidate grid too large: {len(grid)} > {MAX_GRID_COMBINATIONS}"
        )

    settled_winners = _index_settlements(settlements)
    resolved_decisions: list[tuple[DecisionRecord, _Resolved]] = []
    unscorable = 0
    for record in decisions:
        resolved = _resolve_decision(record)
        if resolved is None:
            unscorable += 1
            continue
        resolved_decisions.append((record, resolved))

    results: list[SweepCandidateResult] = []
    for params in grid:
        result = _evaluate_candidate(
            params=params,
            resolved=resolved_decisions,
            settled_winners=settled_winners,
            per_decision_usdc=per_decision_usdc,
        )
        results.append(result)

    results_sorted = sorted(
        results, key=lambda r: r.hypothetical_pnl_usdc, reverse=True
    )

    best_by_pnl = (
        results_sorted[0].as_payload() if results_sorted else None
    )
    best_by_win_rate = None
    settled_results = [r for r in results if r.settled_count > 0]
    if settled_results:
        best = max(settled_results, key=lambda r: (r.win_rate or Decimal("-1"), r.settled_count))
        best_by_win_rate = best.as_payload()

    return {
        "candidate_count": len(grid),
        "decision_sample_count": len(decisions),
        "scorable_decision_count": len(resolved_decisions),
        "unscorable_decision_count": unscorable,
        "per_decision_usdc": str(per_decision_usdc),
        "supported_parameter_keys": list(supported_parameter_keys()),
        "results": [r.as_payload() for r in results_sorted],
        "best_by_pnl": best_by_pnl,
        "best_by_win_rate": best_by_win_rate,
    }


def _validate_candidates(candidates: Mapping[str, Sequence[Any]]) -> None:
    if not candidates:
        raise ValueError("candidates must contain at least one parameter")
    for key, values in candidates.items():
        if key not in _SUPPORTED_PARAMETERS:
            raise ValueError(
                f"unsupported sweep parameter: {key} (allowed: "
                f"{', '.join(supported_parameter_keys())})"
            )
        if not values:
            raise ValueError(f"candidate values for {key} must not be empty")


def _expand_grid(
    candidates: Mapping[str, Sequence[Any]],
) -> list[dict[str, Any]]:
    """笛卡尔积展开 + 类型规范化。"""

    keys = sorted(candidates.keys())  # 排序保证响应稳定可比
    typed_values: list[list[Any]] = []
    for key in keys:
        spec = _SUPPORTED_PARAMETERS[key]
        normalized = [_coerce_value(value, spec) for value in candidates[key]]
        typed_values.append(normalized)
    grid: list[dict[str, Any]] = []
    for combo in itertools.product(*typed_values):
        grid.append({key: combo[idx] for idx, key in enumerate(keys)})
    return grid


def _coerce_value(value: Any, spec: str) -> Any:
    if spec == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected int candidate, got {value!r}") from exc
    if spec == "decimal":
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"expected decimal candidate, got {value!r}") from exc
    raise ValueError(f"unknown spec: {spec}")  # pragma: no cover - guarded by whitelist


def _resolve_decision(record: DecisionRecord) -> _Resolved | None:
    """从 ``decision_output`` 抽 fair_value / entry_price / liquidity。

    entry_price 优先用 metadata 里记录的 best_ask；缺失时退回 entry_price_cap
    （= fair_value * (1 - min_edge/10000) 的策略目标价）；都缺则不可评分。
    """

    if not isinstance(record.decision_output, Mapping):
        return None
    fair_value = _decimal(record.decision_output, "fair_value")
    if fair_value is None or fair_value <= Decimal("0"):
        return None
    entry_price = _decimal(record.decision_output, "entry_price")
    if entry_price is None:
        # outright 评估时 metadata 里通常带 best_ask
        metadata = record.decision_output.get("metadata")
        if isinstance(metadata, Mapping):
            entry_price = _decimal(metadata, "best_ask")
        if entry_price is None:
            entry_price = _decimal(record.decision_output, "entry_price_cap")
    if entry_price is None or entry_price <= Decimal("0"):
        return None
    liquidity_usdc: Decimal | None = None
    metadata = record.decision_output.get("metadata")
    if isinstance(metadata, Mapping):
        liquidity_usdc = _decimal(metadata, "buyable_liquidity_usdc") or _decimal(
            metadata, "liquidity_usdc"
        )
    predicted_edge_bps = (fair_value - entry_price) / fair_value * Decimal("10000")
    return _Resolved(
        fair_value=fair_value,
        entry_price=entry_price,
        liquidity_usdc=liquidity_usdc,
        predicted_edge_bps=predicted_edge_bps,
    )


def _index_settlements(settlements: Sequence[AuditEvent]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for event in settlements:
        if not event.condition_id:
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        winner = payload.get("winning_token_id")
        out[event.condition_id] = None if winner is None else str(winner)
    return out


def _evaluate_candidate(
    *,
    params: Mapping[str, Any],
    resolved: Sequence[tuple[DecisionRecord, _Resolved]],
    settled_winners: Mapping[str, str | None],
    per_decision_usdc: Decimal,
) -> SweepCandidateResult:
    """单个候选组合的全样本评分。"""

    min_edge_bps = params.get("tail_outright_min_edge_bps")
    max_entry_price = params.get("tail_outright_max_entry_price")
    min_depth_usdc = params.get("tail_outright_min_orderbook_depth_usdc")
    abs_price_cap = params.get("entry_no_price_max")

    entered = 0
    settled = 0
    pending = 0
    wins = 0
    losses = 0
    pnl_sum = Decimal("0")
    win_pnl = Decimal("0")
    loss_pnl = Decimal("0")
    for record, info in resolved:
        if not _candidate_passes(
            info=info,
            min_edge_bps=min_edge_bps,
            max_entry_price=max_entry_price,
            abs_price_cap=abs_price_cap,
            min_depth_usdc=min_depth_usdc,
        ):
            continue
        entered += 1
        winner = settled_winners.get(record.condition_id, "__missing__")
        if winner == "__missing__":
            pending += 1
            continue
        settled += 1
        if winner is None or record.token_id is None:
            # 数据残缺：算 PnL 时只能视为该次为 0（保守，不偏向胜也不偏向负）
            continue
        size_shares = per_decision_usdc / info.entry_price
        is_win = record.token_id == winner
        settled_value = Decimal("1") if is_win else Decimal("0")
        pnl = (settled_value - info.entry_price) * size_shares
        pnl_sum += pnl
        if is_win:
            wins += 1
            win_pnl += pnl
        else:
            losses += 1
            loss_pnl += pnl
    return SweepCandidateResult(
        parameters=dict(params),
        would_have_entered_count=entered,
        settled_count=settled,
        pending_count=pending,
        win_count=wins,
        loss_count=losses,
        hypothetical_pnl_usdc=pnl_sum,
        sum_win_pnl_usdc=win_pnl,
        sum_loss_pnl_usdc=loss_pnl,
    )


def _candidate_passes(
    *,
    info: _Resolved,
    min_edge_bps: int | None,
    max_entry_price: Decimal | None,
    abs_price_cap: Decimal | None,
    min_depth_usdc: Decimal | None,
) -> bool:
    if min_edge_bps is not None and info.predicted_edge_bps < Decimal(min_edge_bps):
        return False
    if max_entry_price is not None and info.entry_price > max_entry_price:
        return False
    if abs_price_cap is not None and info.entry_price > abs_price_cap:
        return False
    if min_depth_usdc is not None:
        if info.liquidity_usdc is None:
            # 缺 liquidity 数据时保守不通过——避免假阳性把策略调得过松
            return False
        if info.liquidity_usdc < min_depth_usdc:
            return False
    return True


def _decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _stringify_params(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, Decimal):
            out[key] = str(value)
        else:
            out[key] = value
    return out
