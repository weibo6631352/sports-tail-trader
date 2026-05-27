"""HealthReporter —— `/health` 系列端点的数据源。

原架构方案 §11.4。健康检查**不是诊断工具，是自动告警源**——判断逻辑
集中在本模块（如 `live_source last_observed > 60s → status=degraded`），外部监控
直接读 `status` 字段做告警。

# 与 AdminRuntimeView.health_snapshot 区别

- `AdminRuntimeView.health_snapshot()` 给运营人员看的复合视图（包含 phase /
  ready_to_trade / market_sample 等）
- `HealthReporter` 提供 **机器可读的分维度健康**：每维度独立 `status` +
  `last_*_at` + `*_count` 等数值字段，供外部监控系统（Prometheus alertmanager /
  uptime-kuma / pingdom 等）直接告警

# 5 个维度（§11.4 endpoint 对应）

| Endpoint | 方法 | 关键检查 |
|---|---|---|
| `/health` | `overall()` | 任一维度 unhealthy → unhealthy；任一 degraded → degraded |
| `/health/live_sources` | `live_sources()` | bucket stale > 60s → degraded；feeder FAILED → unhealthy |
| `/health/ws` | `ws()` | connected=False 或 last_message > 30s → degraded |
| `/health/decision` | `decision()` | event_bus queue > warn 阈值 → degraded（其他 metric 待 wire） |
| `/health/account` | `account()` | last_update > 300s → degraded；balance < 0 → unhealthy |

# 阈值

集中在本模块的常量（`_STALE_*` / `_DEGRADED_*` 等），改阈值只动一处。所有维度
的 `status` 取值固定 `healthy / degraded / unhealthy`，方便监控规则统一。

# 不阻塞 P0

HealthReporter 只读各组件的 `snapshot()` / 内存状态，无网络 IO，调用成本 ~ms 级。
即使 operator endpoint 每秒被外部监控拉一次也无负担。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents


HealthStatus = Literal["healthy", "degraded", "unhealthy"]


# === 阈值（集中管理，方便按数据观察调） ===

# live_source bucket stale 阈值：observed_at 超过 → degraded
_LIVE_SOURCE_STALE_S: float = 60.0
# ws last_message_at 超过 → degraded
_WS_IDLE_S: float = 30.0
# account_state last_update 超过 → degraded
_ACCOUNT_STALE_S: float = 300.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stale_seconds(ts: datetime | None, *, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    reference = now or _utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (reference - ts).total_seconds()


@dataclass(frozen=True, slots=True)
class HealthReport:
    """单维度健康报告。"""

    status: HealthStatus
    detail: dict[str, Any] = field(default_factory=dict)


def _worst(statuses: list[HealthStatus]) -> HealthStatus:
    if "unhealthy" in statuses:
        return "unhealthy"
    if "degraded" in statuses:
        return "degraded"
    return "healthy"


class HealthReporter:
    """5 维度健康数据聚合。

    构造时只持有 RuntimeComponents 弱引用——本类无状态，每次调用即时读取
    各组件 snapshot。
    """

    def __init__(self, runtime: "RuntimeComponents") -> None:
        self._runtime = runtime

    def overall(self) -> HealthReport:
        live = self.live_sources()
        ws = self.ws()
        account = self.account()
        decision = self.decision()
        overall_status = _worst([live.status, ws.status, account.status, decision.status])
        return HealthReport(
            status=overall_status,
            detail={
                "live_sources": live.status,
                "ws": ws.status,
                "account": account.status,
                "decision": decision.status,
            },
        )

    def live_sources(self) -> HealthReport:
        now = _utc_now()
        buckets = []
        worst: HealthStatus = "healthy"
        try:
            store = self._runtime.live_state_store
            for bucket in store.all_buckets():
                stale_s = _stale_seconds(bucket.observed_at, now=now)
                status: HealthStatus = "healthy"
                if bucket.health.value == "failed":
                    status = "unhealthy"
                elif stale_s is not None and stale_s > _LIVE_SOURCE_STALE_S:
                    status = "degraded"
                buckets.append(
                    {
                        "source": bucket.source.as_label(),
                        "health": bucket.health.value,
                        "stale_seconds": stale_s,
                        "events_count": len(bucket.events),
                        "status": status,
                    }
                )
                worst = _worst([worst, status])
        except Exception as exc:  # noqa: BLE001 — health 检查不允许吞数据外溢
            return HealthReport(status="unhealthy", detail={"error": str(exc)})
        return HealthReport(
            status=worst,
            detail={
                "stale_threshold_s": _LIVE_SOURCE_STALE_S,
                "buckets": buckets,
            },
        )

    def ws(self) -> HealthReport:
        now = _utc_now()
        results: dict[str, Any] = {}
        worst: HealthStatus = "healthy"
        # market_ws 持续推 book/price_change(每秒级),30s idle = 真死
        # user_ws 仅在用户动作(下单/成交/余额变动)时推消息,paper_mode + 无交易期
        # 内长期 idle 是正常态,只看 connected 状态.idle 判断会让 health 长期误判
        # degraded,把真问题(disconnect / 反复重连)淹没.
        # paper_mode 下 user_ws 整个不需要——成交事件走 paper_fill_engine 内部投影,
        # 不依赖链上 user channel.connected=false 是预期行为,不应标 degraded.
        paper_mode = bool(getattr(self._runtime.settings, "paper_trading_mode", False))
        check_idle: dict[str, bool] = {"market_ws": True, "user_ws": False}
        for name, worker in (
            ("market_ws", self._runtime.market_ws_worker),
            ("user_ws", self._runtime.user_ws_worker),
        ):
            try:
                snap = worker.status_snapshot(include_subscriptions=False)
                connected = bool(getattr(snap, "connected", False))
                last_msg = getattr(snap, "last_message_at", None)
                stale_s = _stale_seconds(last_msg, now=now)
                # market_ws 是 demand-driven: 0 订阅时 WS 不需要连接(no token to subscribe).
                # 这是 §17.8 设计上的"idle" 正常态,不应标 degraded.
                # user_ws 与订阅无关(用户账户层),不走这个豁免.
                subscription_count = int(getattr(snap, "subscription_count", 0) or 0)
                idle_due_to_no_demand = name == "market_ws" and subscription_count == 0
                paper_user_ws_not_needed = name == "user_ws" and paper_mode
                status: HealthStatus = "healthy"
                if idle_due_to_no_demand or paper_user_ws_not_needed:
                    # 没 token 可订阅 OR paper_mode 不需要 user_ws,无论 connected 与否都 healthy
                    pass
                elif not connected:
                    status = "degraded"
                elif check_idle[name] and stale_s is not None and stale_s > _WS_IDLE_S:
                    status = "degraded"
                results[name] = {
                    "connected": connected,
                    "subscription_count": subscription_count,
                    "last_message_at": last_msg.isoformat() if last_msg else None,
                    "idle_seconds": stale_s,
                    "idle_checked": check_idle[name],
                    "idle_due_to_no_demand": idle_due_to_no_demand,
                    "paper_user_ws_not_needed": paper_user_ws_not_needed,
                    "status": status,
                }
                worst = _worst([worst, status])
            except Exception as exc:  # noqa: BLE001
                results[name] = {"status": "unhealthy", "error": str(exc)}
                worst = "unhealthy"
        return HealthReport(
            status=worst,
            detail={"idle_threshold_s": _WS_IDLE_S, **results},
        )

    def decision(self) -> HealthReport:
        try:
            depths = self._runtime.event_bus.snapshot()
        except Exception as exc:  # noqa: BLE001
            return HealthReport(status="unhealthy", detail={"error": str(exc)})
        warn_depth = getattr(self._runtime.settings, "trading_queue_warn_depth", 100)
        trading_depth = depths.trading_queue_depth
        status: HealthStatus = (
            "degraded" if trading_depth > warn_depth else "healthy"
        )
        return HealthReport(
            status=status,
            detail={
                "trading_queue_depth": trading_depth,
                "trading_queue_capacity": depths.trading_queue_capacity,
                "maintenance_queue_depth": depths.maintenance_queue_depth,
                "persistence_queue_depth": depths.persistence_queue_depth,
                "warn_depth": warn_depth,
            },
        )

    def account(self) -> HealthReport:
        now = _utc_now()
        try:
            snap = self._runtime.account_state_store.snapshot()
        except Exception as exc:  # noqa: BLE001
            return HealthReport(status="unhealthy", detail={"error": str(exc)})
        last_reconcile = snap.last_reconcile_at
        stale_s = _stale_seconds(last_reconcile, now=now)
        status: HealthStatus = "healthy"
        if snap.balance_usdc < 0:
            status = "unhealthy"
        elif stale_s is not None and stale_s > _ACCOUNT_STALE_S:
            status = "degraded"
        return HealthReport(
            status=status,
            detail={
                "balance_usdc": str(snap.balance_usdc),
                "allowance_usdc": str(snap.allowance_usdc),
                "last_reconcile_at": (
                    last_reconcile.isoformat() if last_reconcile else None
                ),
                "stale_seconds": stale_s,
                "stale_threshold_s": _ACCOUNT_STALE_S,
                "paper_mode": getattr(
                    self._runtime.settings, "paper_trading_mode", False
                ),
            },
        )
