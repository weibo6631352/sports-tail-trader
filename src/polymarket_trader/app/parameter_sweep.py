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
from enum import StrEnum
from typing import Any, Mapping, Sequence

from polymarket_trader.app.admin_serialization import decimal_text
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


MAX_GRID_COMBINATIONS = 1_000


class SweepSampleQuality(StrEnum):
    """entry_price 抽取来源的质量等级——决定该样本是否进 PnL 偏差告警。

    - ``REAL_BEST_ASK``：从 decision_output / metadata（含 outright 嵌套元数据）
      抽到了 evaluator 当时记录的真实 best_ask；PnL 数学口径正确。
    - ``ENTRY_PRICE_CAP_FALLBACK``：仅有策略目标价 ``entry_price_cap``
      （= fair * (1 - min_edge_bps/10000)），用它当作 entry_price 算 PnL
      会系统性偏高（cap < 实际 best_ask）。fallback count 在响应里暴露，
      非零时调用侧应当审视样本来源是否缺写 best_ask。
    """

    REAL_BEST_ASK = "real_best_ask"
    ENTRY_PRICE_CAP_FALLBACK = "entry_price_cap_fallback"

@dataclass(frozen=True, slots=True)
class _ParameterSpec:
    """sweep 候选值类型 + 金融取值范围 + UI 展示元数据。

    sweep 输出会被人工搬到真实 ParameterStore，所以接受 ``-0.5`` / ``2.5``
    这种物理上不可达的值会污染调优结论。范围检查在 ``_coerce_value`` 内立即做。

    ``label`` / ``input_hint`` / ``example`` 由 API 暴露给前端，避免前端复述
    一份参数列表（单一来源——前端从 ``GET /parameter-sweep/params`` 拉取）。
    """

    type_name: str  # "int" 或 "decimal"
    minimum: Decimal | None = None            # 含端点（>=）
    minimum_exclusive: Decimal | None = None  # 严格大于（>）
    maximum: Decimal | None = None            # 含端点（<=）
    maximum_exclusive: Decimal | None = None  # 严格小于（<）
    label: str = ""
    input_hint: str = ""
    example: str = ""

    def as_api_dict(self, key: str) -> dict[str, object]:
        return {
            "key": key,
            "type": self.type_name,
            "label": self.label,
            "input_hint": self.input_hint,
            "example": self.example,
            "minimum": str(self.minimum) if self.minimum is not None else None,
            "minimum_exclusive": str(self.minimum_exclusive) if self.minimum_exclusive is not None else None,
            "maximum": str(self.maximum) if self.maximum is not None else None,
            "maximum_exclusive": str(self.maximum_exclusive) if self.maximum_exclusive is not None else None,
            "scope": "strategy",
        }


# sweep 允许调的参数白名单——按 strategy parameter_store 注册值的子集挑出来。
# 不开放结算 / 退出参数因为它们影响的是离场端，本模块只算入场后到结算的总 PnL。
_SUPPORTED_PARAMETERS: dict[str, _ParameterSpec] = {
    "tail_outright_min_edge_bps": _ParameterSpec(
        "int",
        minimum=Decimal("0"),
        label="最小 edge (bps)",
        input_hint="int 整数；缩小 = 入场门槛降低",
        example="300, 400, 500, 600",
    ),
    "tail_outright_max_entry_price": _ParameterSpec(
        "decimal",
        minimum_exclusive=Decimal("0"),
        maximum_exclusive=Decimal("1"),
        label="最大入场价 (0–1)",
        input_hint="decimal；扩大会接到更贵的标的",
        example="0.50, 0.60, 0.70",
    ),
    "tail_outright_min_orderbook_depth_usdc": _ParameterSpec(
        "decimal",
        minimum=Decimal("0"),
        label="最小盘口深度 (USDC)",
        input_hint="decimal；为空字段的样本算作不通过",
        example="5, 10, 20",
    ),
    "entry_no_price_max": _ParameterSpec(
        "decimal",
        minimum_exclusive=Decimal("0"),
        maximum=Decimal("1"),
        label="No-side 价格上限 (0–1)",
        input_hint="decimal；安全阈值",
        example="0.45, 0.55",
    ),
}


def supported_parameter_keys() -> tuple[str, ...]:
    return tuple(sorted(_SUPPORTED_PARAMETERS.keys()))


def supported_parameter_specs() -> list[dict[str, object]]:
    """按字典序返回所有可 sweep 参数的 API 表示，供前端渲染 UI。"""
    return [spec.as_api_dict(key) for key, spec in sorted(_SUPPORTED_PARAMETERS.items())]


@dataclass(frozen=True, slots=True)
class _Resolved:
    fair_value: Decimal
    entry_price: Decimal
    liquidity_usdc: Decimal | None
    predicted_edge_bps: Decimal
    entry_price_source: SweepSampleQuality


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

    fallback_count = sum(
        1
        for _record, info in resolved_decisions
        if info.entry_price_source is SweepSampleQuality.ENTRY_PRICE_CAP_FALLBACK
    )

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
        # 警告 caller：entry_price_cap fallback 的样本数；非零时这部分 PnL 数字
        # 偏高（cap 比真实 best_ask 系统性更低）。reader 已经会从 outright 嵌套
        # metadata、tail price 字段抽真实 best_ask，剩余 fallback 通常意味着
        # evaluator 没写 best_ask（数据残缺）或老格式 record。
        "entry_price_cap_fallback_count": fallback_count,
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
        normalized = [_coerce_value(key, value, spec) for value in candidates[key]]
        typed_values.append(normalized)
    grid: list[dict[str, Any]] = []
    for combo in itertools.product(*typed_values):
        grid.append({key: combo[idx] for idx, key in enumerate(keys)})
    return grid


def _coerce_value(key: str, value: Any, spec: _ParameterSpec) -> Any:
    if spec.type_name == "int":
        try:
            coerced: Any = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected int candidate, got {value!r}") from exc
    elif spec.type_name == "decimal":
        try:
            coerced = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"expected decimal candidate, got {value!r}") from exc
    else:
        raise ValueError(f"unknown spec: {spec.type_name}")
    _check_range(key, coerced, spec)
    return coerced


def _check_range(key: str, value: Any, spec: _ParameterSpec) -> None:
    """金融取值范围 guard。值非法直接 ValueError——sweep 不接受物理不可达参数。"""

    numeric = value if isinstance(value, Decimal) else Decimal(value)
    if spec.minimum is not None and numeric < spec.minimum:
        raise ValueError(f"{key} candidate {value!r} < minimum {spec.minimum}")
    if spec.minimum_exclusive is not None and numeric <= spec.minimum_exclusive:
        raise ValueError(
            f"{key} candidate {value!r} must be > {spec.minimum_exclusive}"
        )
    if spec.maximum is not None and numeric > spec.maximum:
        raise ValueError(f"{key} candidate {value!r} > maximum {spec.maximum}")
    if spec.maximum_exclusive is not None and numeric >= spec.maximum_exclusive:
        raise ValueError(
            f"{key} candidate {value!r} must be < {spec.maximum_exclusive}"
        )


def _resolve_decision(record: DecisionRecord) -> _Resolved | None:
    """从 ``decision_output`` 抽 fair_value / entry_price / liquidity。

    实际生产 decision_output 有三种来源 shape：
    1. tail entry (non-outright)：``decision_output["price"]`` 就是 best_ask，
       evaluator 直接把盘口价当 entry_price 写入 ``TradingDecision.buy``。
    2. outright accepted/skip：evaluator 写 ``decision_output["metadata"]
       ["outright_metadata"]["best_ask"]``（嵌套），同时把 ``fair_value`` 也放
       在嵌套里；顶层 ``decision_output["price"]`` 是 entry_price_cap，不是
       盘口价。
    3. 简化测试 / 历史 record：直接顶层 ``fair_value`` + ``entry_price``。

    抽取顺序：
    - fair_value：顶层 → ``metadata.outright_metadata.fair_value`` →
      ``metadata.outright_fair_value``。
    - entry_price（按真实 best_ask 优先级）：顶层 ``entry_price`` →
      ``metadata.outright_metadata.best_ask`` → ``metadata.best_ask`` →
      ``decision_output["price"]``（仅当不是 outright，避免把 cap 误当 ask）。
    - 全部缺失才退到 ``entry_price_cap`` 并标 fallback——cap < 实际 best_ask 会
      让 PnL 系统性偏高。
    """

    if not isinstance(record.decision_output, Mapping):
        return None
    metadata = record.decision_output.get("metadata")
    metadata_map: Mapping[str, Any] = metadata if isinstance(metadata, Mapping) else {}
    outright_meta = metadata_map.get("outright_metadata")
    outright_meta_map: Mapping[str, Any] = (
        outright_meta if isinstance(outright_meta, Mapping) else {}
    )
    is_outright = bool(outright_meta_map) or metadata_map.get("market_family") == "outright"

    fair_value = (
        _decimal(record.decision_output, "fair_value")
        or _decimal(outright_meta_map, "fair_value")
        or _decimal(metadata_map, "outright_fair_value")
    )
    if fair_value is None or fair_value <= Decimal("0"):
        return None

    entry_price_source = SweepSampleQuality.REAL_BEST_ASK
    entry_price = (
        _decimal(record.decision_output, "entry_price")
        or _decimal(outright_meta_map, "best_ask")
        or _decimal(metadata_map, "best_ask")
    )
    if entry_price is None and not is_outright:
        # tail (非 outright) 决策的 price 字段就是 best_ask（hooks.decide_entry
        # 把 best_ask 直接写进 TradingDecision.price）；outright 不能这样退，
        # 否则会把 entry_price_cap 误当成 best_ask 抽出来。
        entry_price = _decimal(record.decision_output, "price")
    if entry_price is None:
        # 退回到 entry_price_cap 是有偏的——cap 是策略目标价
        # (fair * (1 - min_edge_bps/10000))，比实际 best_ask 系统性更低，会
        # 让 hypothetical PnL 偏高。生产环境跑 sweep 前应让 evaluator 把
        # best_ask 真实值写到 metadata；这里只是 last-resort 保底。
        cap = (
            _decimal(record.decision_output, "entry_price_cap")
            or _decimal(outright_meta_map, "entry_price_cap")
        )
        if cap is not None and cap > Decimal("0"):
            entry_price = cap
            entry_price_source = SweepSampleQuality.ENTRY_PRICE_CAP_FALLBACK
    if entry_price is None or entry_price <= Decimal("0"):
        return None

    liquidity_usdc: Decimal | None = (
        _decimal(outright_meta_map, "buyable_liquidity_usdc")
        or _decimal(outright_meta_map, "liquidity_usdc")
        or _decimal(metadata_map, "buyable_liquidity_usdc")
        or _decimal(metadata_map, "liquidity_usdc")
    )
    predicted_edge_bps = (fair_value - entry_price) / fair_value * Decimal("10000")
    return _Resolved(
        fair_value=fair_value,
        entry_price=entry_price,
        liquidity_usdc=liquidity_usdc,
        predicted_edge_bps=predicted_edge_bps,
        entry_price_source=entry_price_source,
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
