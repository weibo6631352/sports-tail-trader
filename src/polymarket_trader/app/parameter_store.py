"""实时调参存储 + 应用层 hook。

让 agent 在不重启的前提下调整风控阈值、预算上限、策略关键参数。所有 override
都通过 ``PARAMETER_OVERRIDE_APPLIED`` 事件落 audit_events，保留 ``previous /
new / operator / applied_at`` 完整审计链。

设计取向：
- ``Settings`` / ``CurrentStrategyConfig`` 是启动期不变量；``ParameterStore`` 是
  runtime 覆盖层，受白名单约束。
- 读侧：caller 主动 ``store.get(scope, key, default)`` 取覆盖值；没有 override
  时返回 default。不改变现有 Settings 注入方式，影响面可控。
- 写侧：``set(scope, key, value, *, operator)`` 校验白名单、类型、范围，原子替
  换内存值，发 outbox 事件。``clear(scope, key)`` 同理。
- 重启即丢：当前不持久化 override；要长期生效得改 ``.env`` 或 Settings。这是
  有意的——agent 的实时调整带探索性，不应跨进程默认存活。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """单个可调参数的定义。

    - ``coerce`` 把任意输入归一化为目标类型（如 Decimal / int / bool）；返回
      ``ValueError`` 由调用层映射到 422。
    - ``validator`` 在 coerce 后做语义校验（范围、白名单），不通过抛 ValueError。
    """

    scope: str
    key: str
    description: str
    coerce: Callable[[Any], Any]
    validator: Callable[[Any], None] = field(default=lambda value: None)


def _coerce_decimal_non_negative(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc
    if result < Decimal("0"):
        raise ValueError("must be non-negative")
    return result


def _coerce_positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not an integer: {value!r}") from exc
    if result < 0:
        raise ValueError("must be non-negative")
    return result


def _coerce_probability(value: Any) -> Decimal:
    result = _coerce_decimal_non_negative(value)
    if result > Decimal("1"):
        raise ValueError("must be between 0 and 1")
    return result


def _coerce_decimal_positive_le_one(value: Any) -> Decimal:
    """0 < value ≤ 1 的 Decimal——Kelly κ / max_position_fraction 用。"""

    result = _coerce_decimal_non_negative(value)
    if result <= Decimal("0") or result > Decimal("1"):
        raise ValueError("must be in (0, 1]")
    return result


def _coerce_decimal_positive(value: Any) -> Decimal:
    result = _coerce_decimal_non_negative(value)
    if result <= Decimal("0"):
        raise ValueError("must be positive")
    return result


def _coerce_execution_permission(value: Any) -> str:
    allowed = {"record_only", "alert_only", "manual_confirm", "auto_execute"}
    text = str(value).strip().lower()
    if text not in allowed:
        raise ValueError(f"execution_permission must be one of {sorted(allowed)}, got {value!r}")
    return text


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise ValueError(f"not a boolean: {value!r}")


# 白名单：只有这些 (scope, key) 能调；其他一律 404。新增字段要显式注册，避免
# 后端把 SecretStr / 密钥字段误暴露。
_REGISTRY: dict[tuple[str, str], ParameterSpec] = {}


def _register(spec: ParameterSpec) -> None:
    _REGISTRY[(spec.scope, spec.key)] = spec


# Settings 风控/预算可调字段。Settings 自身保持启动时不可变；读侧通过
# ``ParameterStore.get('settings', key, fallback)`` 取覆盖。
_register(ParameterSpec(
    scope="settings",
    key="portfolio_budget_usdc",
    description="组合总预算上限（USDC）",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_fraction",
    description="Kelly κ：full Kelly 缩放系数。0.25=quarter Kelly（推荐）。",
    coerce=_coerce_decimal_positive_le_one,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_max_position_fraction",
    description="单市场最大仓位 fraction × bankroll；隐式限制并发头寸 ≈ 1/value。",
    coerce=_coerce_decimal_positive_le_one,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_min_edge",
    description="Kelly 最低 edge 要求；edge < value 直接 reject。",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_min_stake_usdc",
    description="框架最低 stake USDC。effective_min = max(此值, market.min_order_size × price)。",
    coerce=_coerce_decimal_positive,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_allow_round_up_to_market_min",
    description="Kelly 推荐 stake < market min 时是否凑齐到 market min（轻度 over-bet）。",
    coerce=_coerce_bool,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_round_up_max_overbet_ratio",
    description="凑齐金额上限：≤ position_cap × ratio。1.0=可达 cap；0.5=仅允许 50% cap。",
    coerce=_coerce_decimal_positive,
))
_register(ParameterSpec(
    scope="settings",
    key="kelly_drawdown_halt_fraction",
    description="bankroll < peak × value 时拒新仓。0=关闭。",
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="settings",
    key="order_retry_limit",
    description="订单重试上限",
    coerce=_coerce_positive_int,
))
_register(ParameterSpec(
    scope="settings",
    key="audit_retention_days",
    description=(
        "audit_events 保留天数。daily purge job 删除 created_at < now - N days 的 row。"
        "默认 14；改 0 等于禁用 retention（不推荐，仅用于离线分析临时保留全量历史）。"
    ),
    coerce=_coerce_positive_int,
))

# 策略级阈值。键名对齐 CurrentStrategyConfig 实际字段——agent 调参时清楚知道
# 自己在调哪一个 frozen 字段的 runtime 覆盖。
_register(ParameterSpec(
    scope="strategy",
    key="tail_outright_min_edge_bps",
    description="Outright 入场最小 edge（bps）；默认 500=5%",
    coerce=_coerce_positive_int,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_outright_max_entry_price",
    description="Outright 最大入场价（0–1，price space）",
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_outright_min_orderbook_depth_usdc",
    description="Outright 入场要求的最小盘口可吃深度（USDC）",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_outright_exit_edge_target",
    description="Outright 退出目标 edge（小数分数，例如 0.03 = 3%）",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_outright_min_profit_per_share",
    description="Outright 退出最小每股利润（USDC/股）",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="entry_no_price_max",
    description="No-side 入场允许的最大价格（0–1）",
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_moneyline_max_entry_price",
    description="Moneyline tail 最大入场价（0–1）",
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_spreads_max_entry_price",
    description="Spread tail 最大入场价（0–1）",
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_min_liquidity_usdc",
    description="通用 tail 入场最小盘口可吃深度（USDC）",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_implied_min_edge_bps",
    description=(
        "Single-game tail implied fair value 公式 cap=fair×(1-edge_required) 中的 edge bps。"
        "默认 500=5%；操盘手运行时调宽（如 200=2%）让更多 cap 边缘市场进入 Kelly 候选。"
    ),
    coerce=_coerce_positive_int,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_implied_prob_confidence",
    description=(
        "tail implied_p 的 base confidence（0-1），后续按盘口 depth/spread 动态衰减。"
        "默认 0.5；操盘手运行时调到 0.7-0.8 提高 implied 信任度（小账户激进期）。"
    ),
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_implied_conf_depth_baseline_usdc",
    description=(
        "tail implied conf depth 衰减 baseline。ask_depth >= 此值时不再缩 conf；不到时按比例缩。"
        "默认 25；小 bankroll 阶段薄盘是常态可调到 5。"
    ),
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_outright_budget_usdc",
    description=(
        "Outright family（赛季冠军 / 球员奖项等长期市场）总预算上限（USDC）。"
        "默认 0 = 全部 outright 拒绝（仅审计、不下单）。要启用 outright 自动交易，"
        "设置该值 ≥ tail_outright_max_per_market_usdc（默认 25）。"
    ),
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_stale_no_live_state_seconds",
    description=(
        "赛事起始超过该秒数且仍无任何直播状态 → 主动 pause stale market。"
        "默认 86400（24h）。改 0 = 关闭 stale 检测。"
    ),
    coerce=_coerce_positive_int,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_series_winner_budget_usdc",
    description=(
        "Series winner（季后赛系列赛胜者）family 总预算上限（USDC）。"
        "默认 0 = 全部拒绝（仅审计）。要启用自动交易，同时需设置 "
        "tail_series_winner_execution_permission=auto_execute 且值 ≥ "
        "tail_series_winner_max_per_market_usdc（默认 25）。"
    ),
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_series_winner_min_edge_bps",
    description="Series winner 入场最小 edge（bps）；默认 800=8%。",
    coerce=_coerce_positive_int,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_series_winner_max_entry_price",
    description="Series winner 最大入场价（0–1，price space）；默认 0.95。",
    coerce=_coerce_probability,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_series_winner_min_orderbook_depth_usdc",
    description="Series winner 入场要求的最小盘口可吃深度（USDC）；默认 50。",
    coerce=_coerce_decimal_non_negative,
))
_register(ParameterSpec(
    scope="strategy",
    key="tail_series_winner_execution_permission",
    description=(
        "Series winner 执行权限：record_only / alert_only / manual_confirm / auto_execute。"
        "要启用自动交易需同时设置 tail_series_winner_budget_usdc > 0。"
    ),
    coerce=_coerce_execution_permission,
))


def list_specs() -> tuple[ParameterSpec, ...]:
    return tuple(_REGISTRY.values())


def get_spec(scope: str, key: str) -> ParameterSpec | None:
    return _REGISTRY.get((scope, key))


@dataclass(slots=True)
class _OverrideEntry:
    value: Any
    operator: str
    applied_at: str
    reason: str | None
    expires_at: str | None


class ParameterStore:
    """Runtime 可变参数覆盖层。

    线程安全性：单进程内由 asyncio 事件循环串行调度——读写不会并发。set/clear
    是 O(1) 字典写。
    """

    def __init__(self, *, event_bus: Any | None = None) -> None:
        self._overrides: dict[tuple[str, str], _OverrideEntry] = {}
        self._event_bus = event_bus

    def bind_event_bus(self, event_bus: Any | None) -> None:
        self._event_bus = event_bus

    def get(self, scope: str, key: str, default: Any = None) -> Any:
        entry = self._overrides.get((scope, key))
        if entry is None:
            return default
        return entry.value

    def has_override(self, scope: str, key: str) -> bool:
        return (scope, key) in self._overrides

    async def set(
        self,
        *,
        scope: str,
        key: str,
        value: Any,
        operator: str = "agent",
        reason: str | None = None,
        expires_at: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        spec = get_spec(scope, key)
        if spec is None:
            raise KeyError(f"unknown parameter: {scope}.{key}")
        coerced = spec.coerce(value)
        spec.validator(coerced)
        previous = self._overrides.get((scope, key))
        previous_value = None if previous is None else previous.value
        entry = _OverrideEntry(
            value=coerced,
            operator=operator,
            applied_at=_utc_now_iso(),
            reason=reason,
            expires_at=expires_at,
        )
        self._overrides[(scope, key)] = entry
        await self._publish_override(
            scope=scope,
            key=key,
            previous_value=previous_value,
            new_value=coerced,
            operator=operator,
            applied_at=entry.applied_at,
            expires_at=expires_at,
            cleared=False,
            trace_id=trace_id,
        )
        return self._as_payload(scope, key, entry, spec)

    async def clear(
        self,
        *,
        scope: str,
        key: str,
        operator: str = "agent",
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        spec = get_spec(scope, key)
        if spec is None:
            raise KeyError(f"unknown parameter: {scope}.{key}")
        previous = self._overrides.pop((scope, key), None)
        applied_at = _utc_now_iso()
        await self._publish_override(
            scope=scope,
            key=key,
            previous_value=None if previous is None else previous.value,
            new_value=None,
            operator=operator,
            applied_at=applied_at,
            expires_at=None,
            cleared=True,
            trace_id=trace_id,
        )
        return {
            "scope": scope,
            "key": key,
            "cleared": True,
            "operator": operator,
            "applied_at": applied_at,
            "previous_value": None if previous is None else _stringify(previous.value),
        }

    def snapshot(self) -> list[dict[str, Any]]:
        """所有当前 override 的快照——用于 GET /parameters 总览。"""

        result: list[dict[str, Any]] = []
        for (scope, key), entry in self._overrides.items():
            spec = get_spec(scope, key)
            if spec is None:
                continue
            result.append(self._as_payload(scope, key, entry, spec))
        return result

    def registry_payload(self) -> list[dict[str, Any]]:
        """所有可调参数定义 + 当前 override（如有）。"""

        result: list[dict[str, Any]] = []
        for spec in list_specs():
            entry = self._overrides.get((spec.scope, spec.key))
            result.append(
                {
                    "scope": spec.scope,
                    "key": spec.key,
                    "description": spec.description,
                    "override": None if entry is None else self._as_payload(spec.scope, spec.key, entry, spec),
                }
            )
        return result

    def _as_payload(
        self,
        scope: str,
        key: str,
        entry: _OverrideEntry,
        spec: ParameterSpec,
    ) -> dict[str, Any]:
        return {
            "scope": scope,
            "key": key,
            "description": spec.description,
            "value": _stringify(entry.value),
            "operator": entry.operator,
            "applied_at": entry.applied_at,
            "expires_at": entry.expires_at,
            "reason": entry.reason,
        }

    async def _publish_override(
        self,
        *,
        scope: str,
        key: str,
        previous_value: Any,
        new_value: Any,
        operator: str,
        applied_at: str,
        expires_at: str | None,
        cleared: bool,
        trace_id: str | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        from polymarket_trader.domain.events import (
            DomainEvent,
            DomainEventType,
            OutboxPriority,
        )

        try:
            await self._event_bus.publish(
                OutboxPriority.P3,
                DomainEvent(
                    # 前端 confirmAction 在 modal mount 时生成 trace_id；提供则用它
                    # 让"操作意图 + 审计事件"串成一条；缺失时本地生成兜底。
                    trace_id=trace_id or f"param-override-{uuid4().hex}",
                    event_type=DomainEventType.PARAMETER_OVERRIDE_APPLIED,
                    event_id=uuid4().hex,
                    reason=f"{scope}.{key}={'<cleared>' if cleared else _stringify(new_value)}",
                    payload={
                        "scope": scope,
                        "key": key,
                        "previous_value": _stringify(previous_value),
                        "new_value": _stringify(new_value),
                        "operator": operator,
                        "applied_at": applied_at,
                        "expires_at": expires_at,
                        "cleared": cleared,
                    },
                ),
            )
        except Exception:
            # 审计落库失败不能反向阻塞调参——caller 已经在内存里看到生效。
            return


def _stringify(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {k: _stringify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify(v) for v in value]
    return value
