from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

if TYPE_CHECKING:
    from polymarket_trader.app.parameter_store import ParameterStore
from uuid import uuid4

from polymarket_trader.observability.cpu_track import cpu_track

from polymarket_trader.app.trading_decision_service import EntryPlan, TradingDecisionService
from polymarket_trader.app.trading_service import TradingReviewResult, TradingService
from polymarket_trader.app.order_projection import AccountStateProjector
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    ManagedOrderIntent,
    Order,
    OrderResult,
    OrderResultStatus,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.state_machine import MarketLifecycle
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.contracts import TradeAction, DecisionContext, TradingDecision, MarketTokenView
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from .event_payloads import (
    TRADING_DECISION_WORKER_ORIGIN,
    coerce_order_result_from_event,
    is_self_emitted,
    market_from_result,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
    serialize_snapshot,
    snapshot_allowance,
    snapshot_available_usdc,
    snapshot_position,
)
from .order_result_processor import TradingOrderResultProcessor
from .result import TradingDecisionWorkerResult

PositionsProvider = Callable[[], Iterable[Position]]
OpenOrdersProvider = Callable[[], Iterable[Order]]
EntryMetadataProvider = Callable[[DomainEvent, AccountSnapshot | None], Mapping[str, object] | None]
HeartbeatCallback = Callable[..., None]

# Worker 长时间空闲等事件时，仍要定期触发 heartbeat 让 supervisor 区分「卡死」和「空等」。
# 60s 在 N13 观测的"4 分钟无心跳"上下文里足够灵敏，又不会刷屏。
logger = logging.getLogger(__name__)

_TRADING_DECISION_IDLE_HEARTBEAT_SECONDS = 60.0
# _lifecycle_timeline LRU 上限：跟踪市场数超过此值时淘汰最久未更新的条目。
# 每个市场最多保留最近 _LIFECYCLE_HISTORY_PER_MARKET 次转换记录。
_LIFECYCLE_MARKET_CAP = 500
_LIFECYCLE_HISTORY_PER_MARKET = 50
# allocation_decision_recorded 高频事件 dedup 上限：(condition_id, token_id) → state-hash。
# orderbook 每秒上百条更新都跑分配决策；同一市场 candidate 集合 + reason + 是否拿到
# buy budget 没变时不再 emit，避免 audit_events 每天千万条。precise buy_budget_usdc
# 随 bankroll/orderbook 每 tick 抖动，不进 hash——只看"是否真的拿到预算"这个布尔位。
_ALLOCATION_DEDUPE_CAPACITY = 10_000
# market lifecycle / token state dict 容量上限:防 market 长期 active 但 entry
# 停止访问时 dict 永久累积.LRU evict 最久未访问的;market prune callback 同时
# 兜底.5000 market × 2 token + 安全余量 = 12000.
_MARKET_LIFECYCLE_DICT_CAP = 5_000
_TOKEN_STATE_DICT_CAP = 12_000

POSITION_INCREASE_LIFECYCLES = {
    MarketLifecycle.POSITION_OPEN,
    MarketLifecycle.FOLLOW_UP_ORDER_OPEN,
}
# ENTRY_ATTEMPT_LIFECYCLES 集合已删——lifecycle 状态机不再充当"入场是否允许"
# 的决策门控。所有"防重复下单 / 防资金重复占用"由 RiskManager 实时检查
# AccountStateStore（open_orders + positions + balance）+ OrderExecutor 的
# idempotency_index 双重承担。失败积极重试（§17 哲学）：下一次 WS push / signal
# 自然触发新一轮评估，无需 lifecycle 集合判定。
#
# POSITION_INCREASE_LIFECYCLES 仍保留——"已有持仓时，加仓必须有策略显式标记"
# 是真实业务规则（防止策略意图不明的二次买入），不是失败重试 gate。


# 赔率时序 store（module-level，纯观测）：market_slug → deque[(ts_iso, ml_home_p, ml_away_p, tt_over_p, tt_under_p, sp_home_p, sp_away_p)]
# 用于 /runtime/odds-drift API 查看 goalserve 赔率随时间漂移
from collections import deque
_ODDS_DRIFT_STORE: dict[str, deque] = {}
_ODDS_DRIFT_MAX_LEN = 360  # 每 market 360 点 = 30min 数据 (5s 采样)
_ODDS_DRIFT_LAST_AT: dict[str, datetime] = {}
_ODDS_DRIFT_INTERVAL_S = 2  # 2s 采样（贴近 inplay 1.05s 频率，去抖避免重复）


def _record_odds_drift(market_slug: str, goalserve_ml: dict | None, goalserve_totals: dict | None, goalserve_spread: dict | None) -> None:
    """采样赔率到时序 store（节流 5s/market）。"""
    if not market_slug:
        return
    now = datetime.now(timezone.utc)
    last = _ODDS_DRIFT_LAST_AT.get(market_slug)
    if last and (now - last).total_seconds() < _ODDS_DRIFT_INTERVAL_S:
        return
    _ODDS_DRIFT_LAST_AT[market_slug] = now
    sample = {
        "at": now.isoformat(),
        "ml_home_p": (goalserve_ml or {}).get("home_implied_prob"),
        "ml_away_p": (goalserve_ml or {}).get("away_implied_prob"),
        "ml_draw_p": (goalserve_ml or {}).get("draw_implied_prob"),
        "tt_over_p": (goalserve_totals or {}).get("over_implied_prob"),
        "tt_under_p": (goalserve_totals or {}).get("under_implied_prob"),
        "tt_line": (goalserve_totals or {}).get("total_line"),
        "sp_home_p": (goalserve_spread or {}).get("home_implied_prob"),
        "sp_away_p": (goalserve_spread or {}).get("away_implied_prob"),
        "sp_handicap": (goalserve_spread or {}).get("home_handicap"),
    }
    dq = _ODDS_DRIFT_STORE.setdefault(market_slug, deque(maxlen=_ODDS_DRIFT_MAX_LEN))
    dq.append(sample)


def get_odds_drift_store() -> dict[str, deque]:
    """admin API 读取入口"""
    return _ODDS_DRIFT_STORE


def _compute_game_progress_from_dict(live_game: dict) -> dict | None:
    """从 live_game dict 算跨运动统一比赛进度量化（纯统计指标）。

    返回 dict {progress_pct, phase, time_remaining_seconds, segment_label,
    is_critical_moment}。无法估算时各字段为 None / "unknown"。
    设计要点：不参与决策（用户明确要求"只统计不决策"），供 audit 复盘 + 未来
    ML 训练。各运动统一为 [0.0, 1.0] 的 progress 浮点 + 5 类 phase 标签。
    """
    status = (live_game.get("status") or "").lower()
    sport = (live_game.get("sport") or "").lower()
    period = live_game.get("period") or ""
    seconds_remaining = live_game.get("seconds_remaining")
    result: dict = {
        "progress_pct": None,
        "phase": "unknown",
        "time_remaining_seconds": seconds_remaining,
        "segment_label": period,
        "is_critical_moment": False,
    }
    if status == "scheduled":
        result.update({"progress_pct": 0.0, "phase": "pregame"})
        return result
    if status == "ended":
        result.update({"progress_pct": 1.0, "phase": "ended"})
        return result
    if status not in ("live", "paused"):
        return result
    if sport == "baseball":
        bb = live_game.get("baseball_state") or {}
        inning = bb.get("current_inning") or 0
        half = 0.5 if (bb.get("inning_half") or "").lower() == "bottom" else 0.0
        result["progress_pct"] = round(max(0.0, min(1.0, (inning - 1 + half) / 9.0)), 3)
        result["segment_label"] = f"Inning {inning}" + (" Bot" if half else " Top")
        result["is_critical_moment"] = inning >= 9
    elif sport in ("basketball", "basket"):
        bk = live_game.get("basketball_state") or {}
        per = bk.get("current_period") or 0
        result["progress_pct"] = round(max(0.0, min(1.0, (per - 0.5) / 4.0)), 3)
        result["segment_label"] = f"Q{per}"
        result["is_critical_moment"] = per >= 4 and (seconds_remaining is None or seconds_remaining <= 120)
    elif sport == "tennis":
        tn = live_game.get("tennis_state") or {}
        cur = tn.get("current_set") or 0
        bo = tn.get("best_of") or 3
        result["progress_pct"] = round(max(0.0, min(1.0, cur / bo)), 3) if bo > 0 else 0.0
        result["segment_label"] = f"Set {cur}/{bo}"
        result["is_critical_moment"] = cur >= bo
    elif sport == "soccer":
        sc = live_game.get("soccer_state") or {}
        mins = sc.get("clock_minutes") or 0
        soc_period = (sc.get("period") or "").lower()
        base = 0 if "first" in soc_period else (45 if "second" in soc_period else (90 if "extra" in soc_period else 0))
        total = base + mins
        result["progress_pct"] = round(max(0.0, min(1.0, total / 90.0)), 3)
        result["segment_label"] = f"Min {total}"
        result["is_critical_moment"] = total >= 80
    pct = result["progress_pct"]
    if isinstance(pct, (int, float)):
        if pct < 0.25: result["phase"] = "early"
        elif pct < 0.6: result["phase"] = "mid"
        elif pct < 0.9: result["phase"] = "late"
        else: result["phase"] = "final"
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _entry_gate_closed_for_event(
    snapshot: AccountSnapshot | None,
    event: DomainEvent,
) -> bool:
    """账户或单市场入场闸门关闭时,不对高频盘口事件构建交易计划。

    §11 框架不自动 pause,但 admin MANUAL pause 仍是强门禁(运维显式说 stop):
    - 框架自动检测 not_tradable → 不再 pause,让策略 hook 自己看 context 判断
    - admin pause_market_manual → 仍写入 _market_pauses,这里 block entry
    """
    if snapshot is None:
        return False
    if not snapshot.allow_new_entries:
        return True
    if event.condition_id and snapshot.is_market_paused(event.condition_id):
        return True
    return False


class TradingDecisionWorker:
    priority = "P0"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        trading_decision_service: TradingDecisionService | None = None,
        trading_service: TradingService | None = None,
        positions_provider: PositionsProvider | None = None,
        open_orders_provider: OpenOrdersProvider | None = None,
        account_state_store: AccountStateStore | None = None,
        portfolio_budget_usdc: Decimal = Decimal("0"),
        kelly_fraction: Decimal = Decimal("0.25"),
        kelly_max_position_fraction: Decimal = Decimal("0.10"),
        kelly_min_edge: Decimal = Decimal("0.02"),
        kelly_min_stake_usdc: Decimal = Decimal("1"),
        kelly_allow_round_up_to_market_min: bool = True,
        kelly_round_up_max_overbet_ratio: Decimal = Decimal("1"),
        entry_metadata_provider: EntryMetadataProvider | None = None,
        orderbook_direction_signal_reader: "Callable[..., Any] | None" = None,
        parameter_store: "ParameterStore | None" = None,
        heartbeat: HeartbeatCallback | None = None,
        idle_heartbeat_seconds: float = _TRADING_DECISION_IDLE_HEARTBEAT_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        if trading_decision_service is None:
            raise ValueError("trading_decision_service is required")
        self._trading_decision_service = trading_decision_service
        self._trading_service = trading_service or TradingService()
        self._account_state_store = account_state_store
        self._positions_provider = positions_provider or self._build_positions_provider()
        self._open_orders_provider = open_orders_provider or self._build_open_orders_provider()
        # 静态启动值（来自 Settings）；运行时通过 ``parameter_store`` 的 override
        # 覆盖。每次 review 前用 property 读出当前值——这样 agent PUT 后立刻生效。
        self._portfolio_budget_usdc_default = portfolio_budget_usdc
        self._kelly_fraction_default = kelly_fraction
        self._kelly_max_position_fraction_default = kelly_max_position_fraction
        self._kelly_min_edge_default = kelly_min_edge
        self._kelly_min_stake_usdc_default = kelly_min_stake_usdc
        self._kelly_allow_round_up_default = kelly_allow_round_up_to_market_min
        self._kelly_round_up_max_overbet_ratio_default = kelly_round_up_max_overbet_ratio
        self._parameter_store = parameter_store
        self._entry_metadata_provider = entry_metadata_provider
        # 注入 OrderbookDeltaStore.direction_signal callable（保留 layering：
        # worker 不直接 import runtime/orderbook_delta 类型，仅通过 callable 取 dict）。
        self._orderbook_direction_signal_reader = orderbook_direction_signal_reader
        # supervisor 注入的轻量回调；worker 不直接持有 Supervisor，避免 P0 模块反向耦合到 runtime。
        self._heartbeat = heartbeat
        # 单测可以设 < 1s 让 idle heartbeat 路径快速触发；运行时仍用 60s 默认。
        self._idle_heartbeat_seconds = max(0.001, float(idle_heartbeat_seconds))
        # OrderedDict + LRU cap:即使 market 长期 active(reconcile 不 prune),
        # 长期不被访问的 entry 也会自动从 LRU 尾部淘汰,防 dict 永久膨胀.
        self._market_lifecycle: OrderedDict[str, MarketLifecycle] = OrderedDict()
        # token_id → 上次 *真实* ORDERBOOK_SNAPSHOT_UPDATED 事件处理时间戳。
        # main.py position_exit_evaluator 5s 周期 publish 合成 event，但合成
        # 事件处理时检查：该 token 5s 内已有真实 event 就 skip，避免重复评估。
        self._token_last_real_orderbook_at: OrderedDict[str, datetime] = OrderedDict()
        # token_id → 最近一次 decide_exit 决策的完整 metadata 快照。
        # /positions/signals admin endpoint 从此读取，给 UI/操盘人实时展示
        # 5 类投票 + 流动性 tier + math_lock 是否支持 + fair_value 来源。
        # 仅持仓 token 写入，无持仓 token 不会有 entry，自动 LRU 由内存压力管理。
        self._token_position_signals: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # (strategy_id, reason) → count，供 admin/observability 查询哪个策略因何跳过了多少次。
        # (strategy_id, reason) → count 计数器.reason 是 enum-like 字符串
        # (< 100 种内建),strategy_id 由 framework 固定,组合上限 < 500;不需要 LRU.
        # 如果 reason 包含动态文本(本来不该如此),应在 source 端归一化,而非 LRU 兜底.
        self._skip_reason_histogram: dict[tuple[str, str], int] = {}
        # condition_id → [(lifecycle, timestamp), …]，记录每次状态转换的时间点。
        # OrderedDict + cap = LRU 防止无限增长（见 _LIFECYCLE_MARKET_CAP）。
        self._lifecycle_timeline: OrderedDict[str, list[tuple[MarketLifecycle, datetime]]] = OrderedDict()
        # allocation_decision_recorded dedup：(condition_id, token_id) → 上次 emit 的
        # state-hash。同一 key 状态未变跳过 publish，依然计入 _suppressed_allocation_emits。
        self._last_allocation_state_hash: OrderedDict[tuple[str | None, str | None], str] = OrderedDict()
        self._suppressed_allocation_emits: int = 0
        self._order_result_processor = TradingOrderResultProcessor(
            host=self,
            trading_decision_service=self._trading_decision_service,
            trading_service=self._trading_service,
            account_state_store=self._account_state_store,
        )

    def evict_market(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        """registry prune callback:清自己的 cid/token 索引 dict,防内存泄漏.

        4 个 dict:
        - _market_lifecycle (cid)
        - _token_last_real_orderbook_at (token)
        - _token_position_signals (token)
        - _last_allocation_state_hash (cid+token tuple key,有 cap 但 prune 联动更干净)
        """
        self._market_lifecycle.pop(condition_id, None)
        for tok in token_ids:
            self._token_last_real_orderbook_at.pop(tok, None)
            self._token_position_signals.pop(tok, None)
            self._last_allocation_state_hash.pop((condition_id, tok), None)
        # _lifecycle_timeline 已有 LRU cap,但 prune 联动让其立即清:
        self._lifecycle_timeline.pop(condition_id, None)

    def _fetch_orderbook_direction(self, token_id: str | None) -> dict[str, Any] | None:
        """从 OrderbookDeltaStore 取 10s 窗口方向信号，序列化成 dict 注入
        DecisionContext.metadata['orderbook_direction']。

        策略消费归一化复合信号（direction_score / price_momentum / flow_imbalance /
        direction_label / confidence），替代单时点 bid/ask 深度比 imbalance ratio——
        后者会被 MM 假墙骗，flow_imbalance 是窗口内 best 价位移 + real_depth 消耗
        的真实订单流方向，更可靠。
        """
        if self._orderbook_direction_signal_reader is None or not token_id:
            return None
        try:
            signal = self._orderbook_direction_signal_reader(token_id, window_seconds=10.0)
        except Exception:
            return None
        if signal is None:
            return None
        as_metadata = getattr(signal, "as_metadata", None)
        if callable(as_metadata):
            return dict(as_metadata())
        return None

    def _param_override(self, key: str, default: Any) -> Any:
        store = self._parameter_store
        if store is None:
            return default
        return store.get("settings", key, default=default)

    @property
    def _portfolio_budget_usdc(self) -> Decimal:
        return self._param_override("portfolio_budget_usdc", self._portfolio_budget_usdc_default)

    @property
    def _kelly_fraction(self) -> Decimal:
        return self._param_override("kelly_fraction", self._kelly_fraction_default)

    @property
    def _kelly_max_position_fraction(self) -> Decimal:
        return self._param_override(
            "kelly_max_position_fraction", self._kelly_max_position_fraction_default
        )

    @property
    def _kelly_min_edge(self) -> Decimal:
        return self._param_override("kelly_min_edge", self._kelly_min_edge_default)

    @property
    def _kelly_min_stake_usdc(self) -> Decimal:
        return self._param_override(
            "kelly_min_stake_usdc", self._kelly_min_stake_usdc_default
        )

    @property
    def _kelly_allow_round_up(self) -> bool:
        return self._param_override(
            "kelly_allow_round_up_to_market_min", self._kelly_allow_round_up_default
        )

    @property
    def _kelly_round_up_max_overbet_ratio(self) -> Decimal:
        return self._param_override(
            "kelly_round_up_max_overbet_ratio",
            self._kelly_round_up_max_overbet_ratio_default,
        )

    async def run(self) -> None:
        if self._event_bus is None:
            raise RuntimeError("TradingDecisionWorker requires an EventBus to run")
        self._emit_heartbeat(detail=self._idle_detail("running"))
        while True:
            await self.run_once()

    async def run_once(self) -> "TradingDecisionWorkerResult | None":
        if self._event_bus is None:
            raise RuntimeError("TradingDecisionWorker requires an EventBus to run")
        # 空闲等事件时仍要让 supervisor 区分「卡死」和「无事可做」——超时后只 heartbeat，
        # 不向上抛错，下一轮继续等。idle 时不消耗 CPU；只有真到 timeout 才唤醒一次。
        while True:
            try:
                event = await asyncio.wait_for(
                    self._event_bus.next_trading_event(),
                    timeout=self._idle_heartbeat_seconds,
                )
            except asyncio.TimeoutError:
                self._emit_heartbeat(detail=self._idle_detail("idle"))
                continue
            break
        result = await self.process_event(event)
        self._emit_heartbeat(detail=self._processed_detail(event))
        return result

    @cpu_track("trading_decision")
    async def process_event(self, event: DomainEvent) -> "TradingDecisionWorkerResult | None":
        if is_self_emitted(event):
            return None
        import time as _time
        _event_start = _time.time()

        event_name = str(event.event_type)
        snapshot = self._snapshot()
        try:
            result = await self._process_event_inner(event, event_name, snapshot)
            return result
        finally:
            try:
                from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
                SystemPerfMonitor.get().record_event_latency(
                    event_type=event_name,
                    latency_ms=(_time.time() - _event_start) * 1000,
                )
            except Exception:
                pass

    async def _process_event_inner(self, event, event_name, snapshot):
        if event_name in {
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
            DomainEventType.ENTRY_SIGNAL_TRIGGERED.value,
        }:
            return await self._handle_orderbook_snapshot_updated(event, snapshot)
        order_result = coerce_order_result_from_event(event)
        if order_result is not None:
            return await self._handle_order_result(
                source_event=event,
                order_result=order_result,
                snapshot=snapshot,
            )

        if event_name == DomainEventType.POSITION_UPDATED.value:
            return await self._handle_position_updated(event, snapshot)

        return None

    async def _handle_orderbook_snapshot_updated(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> "TradingDecisionWorkerResult | None":
        # 合成事件 dedupe：position_exit_evaluator 每 5s 发合成 event 兜底薄盘
        # 没人推 orderbook 的场景。但同 token 5s 内已有真实 orderbook event 处理过
        # → 该 token 不缺数据，跳过合成 event 避免重复评估。
        is_synthetic = (event.payload or {}).get("synthetic") is True
        if (
            is_synthetic
            and event.token_id
            and event.token_id in self._token_last_real_orderbook_at
        ):
            last_real = self._token_last_real_orderbook_at[event.token_id]
            if (_utc_now() - last_real).total_seconds() < 5.0:
                return None
        if not is_synthetic and event.token_id:
            self._token_last_real_orderbook_at[event.token_id] = _utc_now()
            self._token_last_real_orderbook_at.move_to_end(event.token_id)
            while len(self._token_last_real_orderbook_at) > _TOKEN_STATE_DICT_CAP:
                self._token_last_real_orderbook_at.popitem(last=False)
        # 订阅驱动 reprice：每个 orderbook tick 检查该 token 是否有持仓，
        # 有 → 跑 decide_exit 让 _maybe_reprice_stale_sell 用最新 best_bid/fair_value
        # 评估是否 cancel-replace stale SELL。不依赖 60s reconcile 周期。
        # 在 entry 路径之前先 reprice，确保盘口快速反弹时 SELL 立即跟价。
        has_snapshot = snapshot is not None
        has_cid = bool(event.condition_id)
        has_tid = bool(event.token_id)
        if has_snapshot and has_cid and has_tid:
            position = snapshot.get_position(event.condition_id, event.token_id)
            if position is not None and position.shares > Decimal("0"):
                # 订阅驱动 MTM 刷新：从 hot orderbook 拿 best_bid 即时更新
                # position.current_value / cash_pnl，不等 reconcile 60s 周期。
                # 这让 portfolio / admin / 净值显示实时看到当前真实可实现价值。
                # 关键：best_bid=None（冷板凳无买家）→ MTM=$0，强制覆盖
                # data-api 返回的 stale curPrice，避免虚假浮盈（Kalinina case
                # 显示 cv=\$59 但实际 best_bid=None 全损 \$6.24 cost）。
                orderbook = self._trading_decision_service.lookup_orderbook(event.token_id)
                if orderbook is not None:
                    # MTM 用 best_bid（立即可成交价上限），但用 sell_actionable
                    # 守门：NO_BID / CEILING_ONLY / DUST_BID 都视为"无真实可实现
                    # 价值"——CEILING_ONLY 是 MM 在 0.99 接 SELL 的天花板单（不算
                    # 真买家），DUST_BID 是 best 一档 < $5 USDC（穿一笔就没）。
                    if orderbook.sell_actionable and orderbook.best_bid is not None:
                        refreshed = position.with_mark_to_market(orderbook.best_bid)
                    else:
                        refreshed = position.with_mark_to_market(Decimal("0"))
                    self._account_state_store.upsert_position(refreshed)
                    position = refreshed
                logger.info(
                    "tick_reprice_triggered",
                    extra={
                        "condition_id": event.condition_id,
                        "token_id": event.token_id,
                        "position_shares": str(position.shares),
                        "open_sell_shares": str(position.open_sell_shares),
                        "current_value": str(position.current_value) if position.current_value is not None else None,
                    },
                )
                await self._execute_position_exit_if_needed(
                    event=event,
                    snapshot=snapshot,
                    position=position,
                )
            else:
                logger.info(
                    "tick_reprice_skip",
                    extra={
                        "reason": "no_position" if position is None else "zero_shares",
                        "condition_id": event.condition_id,
                        "token_id": event.token_id,
                    },
                )
        else:
            logger.info(
                "tick_reprice_skip",
                extra={
                    "reason": "missing_event_fields",
                    "has_snapshot": has_snapshot,
                    "has_cid": has_cid,
                    "has_tid": has_tid,
                },
            )

        if _entry_gate_closed_for_event(snapshot, event):
            return None
        plan = self._trading_decision_service.build_entry_plan(
            trace_id=event.trace_id,
            condition_id=event.condition_id,
            token_id=event.token_id,
            account_snapshot=snapshot,
            portfolio_budget_usdc=self._portfolio_budget_usdc,
            available_usdc=snapshot_available_usdc(snapshot),
            kelly_fraction=self._kelly_fraction,
            kelly_max_position_fraction=self._kelly_max_position_fraction,
            kelly_min_edge=self._kelly_min_edge,
            kelly_min_stake_usdc=self._kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=self._kelly_allow_round_up,
            kelly_round_up_max_overbet_ratio=self._kelly_round_up_max_overbet_ratio,
            positions=(snapshot.positions if snapshot is not None else tuple(self._positions_provider())),
            open_orders=(
                snapshot.open_orders if snapshot is not None else tuple(self._open_orders_provider())
            ),
            metadata=self._entry_metadata(event, snapshot),
        )
        # AllocationPlan 决策过程结构化落库——payload 含每个候选的 reason /
        # release_reason / target_budget / buy_budget，回答"为什么选这个市场
        # 不选那个"。P3 异步，失败静默；策略层不感知。
        await self._publish_allocation_decision(event=event, plan=plan)
        if plan.market is None or plan.orderbook is None or event.token_id != plan.orderbook.token_id:
            # silent skip 历史上让"candidate ready=True 却无 order_created"难诊断；
            # 这里 INFO 日志带 plan 是否 ready_to_trade + buy_budget，下次排查时直接 grep
            # entry_dispatch_skipped condition_id=<...> 就能区分是 plan 缺数据还是 token 不匹配。
            logger.info(
                "entry_dispatch_skipped",
                extra={
                    "skip_reason": (
                        "plan_market_none" if plan.market is None
                        else "plan_orderbook_none" if plan.orderbook is None
                        else "event_token_mismatch"
                    ),
                    "condition_id": event.condition_id,
                    "event_token_id": event.token_id,
                    "plan_orderbook_token_id": plan.orderbook.token_id if plan.orderbook is not None else None,
                    "plan_ready_to_trade": plan.ready_to_trade,
                    "allocation_buy_budget_usdc": str(plan.allocation.buy_budget_usdc) if plan.allocation is not None else None,
                    "plan_reason": plan.reason or "",
                },
            )
            return None

        state = self._state_for_market(plan.market)
        if state is None:
            self._transition_market(plan.market, MarketLifecycle.WATCHING_ORDERBOOK)
        elif not _state_allows_entry_attempt(
            state,
            plan,
        ):
            logger.info(
                "entry_dispatch_skipped",
                extra={
                    "skip_reason": "lifecycle_not_attemptable",
                    "condition_id": plan.market.condition_id,
                    "event_token_id": event.token_id,
                    "lifecycle_state": state.value,
                    "plan_ready_to_trade": plan.ready_to_trade,
                    "allocation_buy_budget_usdc": str(plan.allocation.buy_budget_usdc) if plan.allocation is not None else None,
                },
            )
            return None
        return await self._execute_entry_plan(event=event, snapshot=snapshot, plan=plan)

    async def _execute_entry_plan(
        self,
        *,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
        plan: EntryPlan,
    ) -> "TradingDecisionWorkerResult":
        positions = snapshot.positions if snapshot is not None else tuple(self._positions_provider())
        open_orders = (
            snapshot.open_orders if snapshot is not None else tuple(self._open_orders_provider())
        )
        if snapshot is not None and not snapshot.allow_new_entries:
            return await self._emit_entry_skip(
                event=event,
                plan=plan,
                trace_id=event.trace_id,
                reason="entry_paused",
                lifecycle=MarketLifecycle.PAUSED,
                extra_payload={"account_snapshot": serialize_snapshot(snapshot)},
                emit_in_tuple=False,
            )

        if not plan.ready_to_trade or plan.intent is None or plan.market is None or plan.orderbook is None:
            return await self._emit_entry_skip(
                event=event,
                plan=plan,
                trace_id=plan.trace_id,
                reason=plan.reason or "entry_not_ready",
                lifecycle=MarketLifecycle.WATCHING_ORDERBOOK,
                extra_payload=None,
                emit_in_tuple=True,
            )

        focus_token_id = plan.intent.token_id
        focus_position = _match_position(positions, plan.market.condition_id, focus_token_id)
        focus_open_orders = _match_open_orders(open_orders, plan.market.condition_id, focus_token_id)
        self._transition_market(plan.market, MarketLifecycle.ENTRY_SUBMITTING)
        review = await self._trading_service.review_intent(
            plan.intent,
            market=plan.market,
            orderbook=plan.orderbook,
            position=focus_position,
            open_orders=focus_open_orders,
            condition_open_orders=_condition_orders(snapshot, plan.market.condition_id),
            condition_positions=_condition_positions(snapshot, plan.market.condition_id),
            allocation_plan=plan.allocation_plan,
            classification_passed=True,
            balance_usdc=snapshot_available_usdc(snapshot),
            allowance_usdc=snapshot_allowance(snapshot),
            bankroll_usdc=_resolve_bankroll_for_review(
                portfolio_budget_usdc=self._portfolio_budget_usdc,
                snapshot=snapshot,
            ),
            kelly_max_position_fraction=self._kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=self._kelly_round_up_max_overbet_ratio,

            operation=plan.intent.side.value.lower(),
        )
        risk_event = await self._publish(
            DomainEventType.RISK_CHECK_PASSED
            if review.risk_decision is not None and review.risk_decision.passed
            else DomainEventType.RISK_CHECK_FAILED,
            trace_id=plan.trace_id,
            market_slug=plan.market.market_slug,
            condition_id=plan.market.condition_id,
            token_id=plan.intent.token_id,
            reason="risk_decision_unavailable" if review.risk_decision is None else review.risk_decision.reason,
            payload={
                "entry_event_id": event.event_id,
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "allocation_plan": serialize_allocation_plan(plan),
                "allocation": serialize_allocation(plan),
                "plan_metadata": serialize_plan_metadata(plan),
                "intent": serialize_intent(plan.intent),
                "review": serialize_review(review),
            },
        )
        result = await self._handle_order_result(
            source_event=event,
            order_result=review.order_result,
            snapshot=snapshot,
            execution=review,
            plan=plan,
        )
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=plan,
            review=review,
            emitted_event=risk_event,
            emitted_events=(risk_event, *result.emitted_events),
            follow_up_intents=result.follow_up_intents,
            follow_up_reviews=result.follow_up_reviews,
            state_before=result.state_before,
            state_after=result.state_after,
        )

    async def _handle_order_result(
        self,
        *,
        source_event: DomainEvent,
        order_result: OrderResult | None,
        snapshot: AccountSnapshot | None,
        execution: TradingReviewResult | None = None,
        plan: EntryPlan | None = None,
    ) -> "TradingDecisionWorkerResult":
        return await self._order_result_processor.handle(
            source_event=source_event,
            order_result=order_result,
            snapshot=snapshot,
            execution=execution,
            plan=plan,
        )

    async def _handle_position_updated(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> "TradingDecisionWorkerResult":
        if snapshot is None:
            return TradingDecisionWorkerResult(
                entry_event=event,
                plan=None,
                review=None,
                emitted_event=event,
                state_after=None,
            )
        market = self._market_fromsnapshot_position(snapshot, event.condition_id, event.token_id)
        position = _match_position_for_event(snapshot, event)
        if market is not None:
            self._transition_market(market, MarketLifecycle.POSITION_OPEN)
        if position is None:
            return TradingDecisionWorkerResult(
                entry_event=event,
                plan=None,
                review=None,
                emitted_event=event,
                state_after=self._state_for_market(market),
            )

        exit_result = await self._execute_position_exit_if_needed(
            event=event,
            snapshot=snapshot,
            position=position,
        )
        if exit_result is not None:
            return exit_result
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=None,
            review=None,
            emitted_event=event,
            state_after=self._state_for_market(market),
        )

    async def _execute_position_exit_if_needed(
        self,
        *,
        event: DomainEvent,
        snapshot: AccountSnapshot,
        position: Position,
    ) -> "TradingDecisionWorkerResult | None":
        """在真实持仓更新后让策略决定是否需要退出保护单。

        用户 WS 的成交可能晚于初次下单响应到达。此时热态里已经有持仓，但
        同步 BUY 结果不一定触发跟单 SELL；这里以 position/open_orders 为事实，
        调策略 ``decide_exit``。当前策略默认等待结算会返回 SKIP；如策略返回
        SELL intent，仍走统一风控和执行器。
        """

        market = self._trading_decision_service.resolve_market(
            condition_id=position.condition_id,
            token_id=position.token_id,
        )
        open_orders = snapshot.open_orders_for_market(position.condition_id, position.token_id)
        exit_metadata: dict[str, Any] = {
            "exit_trigger": "position_updated",
            "source_event_id": event.event_id,
            "source_reason": event.reason,
        }
        direction = self._fetch_orderbook_direction(position.token_id)
        if direction is not None:
            exit_metadata["orderbook_direction"] = direction
        quant_decision = self._trading_decision_service.quant_decide(
            DecisionContext(
                trace_id=event.trace_id,
                strategy_id=self._trading_decision_service.strategy_id,
                market=market,
                token_id=position.token_id,
                orderbook=self._trading_decision_service.lookup_orderbook(position.token_id),
                market_token_views=tuple(
                    MarketTokenView(
                        token_id=outcome.token_id,
                        outcome=outcome.outcome,
                    )
                    for outcome in (() if market is None else market.outcomes)
                ),
                account_snapshot=snapshot,
                position=position,
                open_orders=open_orders,
                quant_trigger_kind="market_tick",
                metadata=exit_metadata,
            )
        )
        # market_tick 路径下最多产出 1 个 action；为兼容旧 SELL/REPLACE
        # 分发，把 QuantDecision 拆成单一 decision 给后续 intent 转换。
        if quant_decision.actions:
            decision = quant_decision.actions[0]
        else:
            decision = TradingDecision.skip(reason=quant_decision.reason or "quant_no_action")
        # 缓存决策 metadata 供 /positions/signals admin endpoint 暴露。每次
        # decide_exit 后更新当前 token 的 signals 快照，UI/操盘人可实时看到
        # 5 类投票 + 流动性 tier + math_lock 是否支持 + fair_value 来源。
        if position.token_id and decision.metadata:
            self._token_position_signals[position.token_id] = {
                "condition_id": position.condition_id,
                "token_id": position.token_id,
                "market_slug": (market.market_slug if market is not None else position.market_slug),
                "evaluated_at": _utc_now().isoformat(),
                "decision_action": decision.action.value,
                "decision_reason": decision.reason,
                "decision_price": str(decision.price) if decision.price is not None else None,
                "metadata": {k: v for k, v in decision.metadata.items() if k.startswith("dynamic_exit_")},
            }
            self._token_position_signals.move_to_end(position.token_id)
            while len(self._token_position_signals) > _TOKEN_STATE_DICT_CAP:
                self._token_position_signals.popitem(last=False)
        # SELL 直接挂；REPLACE 是 reprice 路径（_maybe_reprice_stale_sell 把 stale
        # $0.99 SELL cancel-replace 到 fair_value × 0.97），不接 REPLACE 会让订阅
        # 触发的 reprice 决策静默丢弃。
        if decision.action not in {TradeAction.SELL, TradeAction.REPLACE}:
            return None
        intent = self._trading_decision_service.build_intent_from_decision(
            trace_id=event.trace_id,
            condition_id=position.condition_id,
            market_slug=event.market_slug or position.market_slug or (
                None if market is None else market.market_slug
            ),
            default_token_id=position.token_id,
            decision=decision,
        )
        if intent is None:
            return None

        review = await self._execute_managed_intent(intent, snapshot=snapshot)
        emitted = await self._publish(
            DomainEventType.ORDER_SUBMITTED,
            trace_id=intent.trace_id,
            market_slug=intent.market_slug,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            reason="order_result_unavailable" if review.order_result is None else review.order_result.reason,
            payload={
                "phase": "position_exit",
                "source_event_id": event.event_id,
                "decision_metadata": dict(decision.metadata),
                "intent": serialize_intent(intent),
                "review": serialize_review(review),
            },
        )
        self._apply_position_exit_result(
            review,
            intent=intent,
            snapshot=snapshot,
        )
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=None,
            review=review,
            emitted_event=emitted,
            emitted_events=(emitted,),
            follow_up_intents=(intent,),
            follow_up_reviews=(review,),
            state_after=self._state_for_market_by_key(position.condition_id),
        )

    def _apply_position_exit_result(
        self,
        review: TradingReviewResult,
        *,
        intent: ManagedOrderIntent,
        snapshot: AccountSnapshot,
    ) -> AccountSnapshot:
        """把持仓触发的退出单结果投影回热态。"""

        if review.order_result is None:
            return snapshot
        self._transition_from_order_result(review.order_result)
        projector = self._account_projector()
        if projector is not None and isinstance(intent, SellOrderIntent):
            projector.apply_sell_result(
                review.order_result,
                snapshot=snapshot,
                intent=intent,
            )
        if projector is not None:
            projector.apply_result_flags(review.order_result, snapshot=snapshot)
        if self._account_state_store is not None:
            return self._account_state_store.snapshot()
        return snapshot

    async def _emit_entry_skip(
        self,
        *,
        event: DomainEvent,
        plan: EntryPlan,
        trace_id: str,
        reason: str,
        lifecycle: MarketLifecycle,
        extra_payload: Mapping[str, object] | None,
        emit_in_tuple: bool,
    ) -> "TradingDecisionWorkerResult":
        """统一发布入场 SKIP 事件 + 状态转换 + 构造 result。

        ``emit_in_tuple`` 控制是否把 SKIP 事件同时写入 ``emitted_events`` 元组。
        历史上两条入场跳过路径（entry_paused / entry_not_ready）只在该字段、
        ``trace_id`` 来源、``reason`` 和 ``extra_payload`` 上有差别，其余完全相同。
        """

        payload: dict[str, object] = {
            "entry_event_id": event.event_id,
            "origin": TRADING_DECISION_WORKER_ORIGIN,
            "reason": reason,
            "allocation_plan": serialize_allocation_plan(plan),
            "allocation": serialize_allocation(plan),
            "plan_metadata": serialize_plan_metadata(plan),
        }
        if extra_payload:
            payload.update(extra_payload)
        strategy_id = self._trading_decision_service.strategy_id or "unknown"
        key = (strategy_id, reason or "unknown_reason")
        self._skip_reason_histogram[key] = self._skip_reason_histogram.get(key, 0) + 1
        skipped = await self._publish(
            DomainEventType.SKIPPED,
            trace_id=trace_id,
            market_slug=event.market_slug,
            condition_id=event.condition_id,
            token_id=event.token_id,
            reason=reason,
            payload=payload,
            priority=OutboxPriority.P3,
        )
        self._transition_market(plan.market, lifecycle if plan.market else None)
        emitted_events_tuple: tuple[DomainEvent, ...] = (skipped,) if emit_in_tuple else ()
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=plan,
            review=None,
            emitted_event=skipped,
            emitted_events=emitted_events_tuple,
            state_after=self._state_for_market(plan.market),
        )

    async def _publish_allocation_decision(
        self,
        *,
        event: DomainEvent,
        plan: EntryPlan,
    ) -> None:
        """投递 ALLOCATION_DECISION_RECORDED 事件。失败静默，不阻塞主链路。

        emit guard：只有当 ``allocation_plan.allocations`` 实际产生了候选时才
        发——orderbook 每次更新都触发本路径，空 plan 不发避免 audit 表暴涨。
        """

        if self._event_bus is None or plan.allocation_plan is None:
            return
        allocation_plan = plan.allocation_plan
        if not allocation_plan.allocations:
            return
        candidates = []
        for allocation in allocation_plan.allocations:
            candidates.append(
                {
                    "condition_id": allocation.condition_id,
                    "token_id": allocation.token_id,
                    "market_slug": allocation.market_slug,
                    "target_budget_usdc": str(allocation.target_budget_usdc),
                    "buy_budget_usdc": str(allocation.buy_budget_usdc),
                    "current_exposure_usdc": str(allocation.current_exposure_usdc),
                    "released_budget_usdc": str(allocation.released_budget_usdc),
                    "reason": allocation.reason,
                    "release_reason": allocation.release_reason,
                }
            )
        skipped_reasons: dict[str, int] = {}
        for allocation in allocation_plan.allocations:
            r = allocation.release_reason or allocation.reason or ""
            if r:
                skipped_reasons[r] = skipped_reasons.get(r, 0) + 1
        selected = [
            allocation.condition_id
            for allocation in allocation_plan.allocations
            if allocation.buy_budget_usdc > 0
        ]
        # dedup hash:**只看当前 event 自己的 (cid,token) 自身决策状态**.
        # 旧 bug:hash 含 sorted(整个 plan 的 allocations)+selected,A 市场 allocation
        # 变化让 B 市场 hash 也变 → "状态不变"被误判 "状态变了"重发.实测同 (cid,token)
        # 60s 内 3-4 条都是同一 reason,纯粹是兄弟市场扰动.
        # 修复:只 hash 我自己这条 allocation + plan_reason(说明全局上下文).
        my_alloc = next(
            (a for a in allocation_plan.allocations
             if a.condition_id == event.condition_id and a.token_id == event.token_id),
            None,
        )
        candidate_state = (
            my_alloc.reason if my_alloc else None,
            my_alloc.release_reason if my_alloc else None,
            (my_alloc.buy_budget_usdc > 0) if my_alloc else False,
        )
        # hash 只看自己,不含 plan-level 全局信号(selected_count 等会因兄弟
        # market 选中而抖动,让稳定 reason="no_eligible_market" 的市场也被重复 emit).
        # plan_reason 是 plan 顶层 reason(很少变,保留作为决策上下文).
        hash_payload = json.dumps(
            {"self": candidate_state, "plan_reason": allocation_plan.reason or ""},
            sort_keys=True,
            default=str,
        )
        state_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
        dedup_key = (event.condition_id, event.token_id)
        previous_hash = self._last_allocation_state_hash.get(dedup_key)
        if previous_hash == state_hash:
            self._last_allocation_state_hash.move_to_end(dedup_key)
            self._suppressed_allocation_emits += 1
            return
        self._last_allocation_state_hash[dedup_key] = state_hash
        self._last_allocation_state_hash.move_to_end(dedup_key)
        while len(self._last_allocation_state_hash) > _ALLOCATION_DEDUPE_CAPACITY:
            self._last_allocation_state_hash.popitem(last=False)
        # decision_snapshot：把"做决策时所看到的真实数据"嵌入 audit payload，
        # 供事后复盘"为什么这笔在这个价位下单/不下单"。否则只看 candidate 的
        # reason/budget 是黑盒——无法分辨：信号触发是因为盘口真错位 vs Goalserve
        # odds stale vs orderbook 已单边下杀策略没看。
        # 体积控制：只摘关键字段（best_bid/ask/sizes/spread/depth/tick + 关键
        # 直播/赔率字段），不存全部 bids/asks 层级（每秒变化高频）。
        decision_snapshot: dict[str, Any] = {}
        if plan.orderbook is not None:
            ob = plan.orderbook
            top_bids = sorted(ob.bids, key=lambda lvl: lvl.price, reverse=True)[:5]
            top_asks = sorted(ob.asks, key=lambda lvl: lvl.price)[:5]
            # 多档 imbalance：1/3/5 层各计算（不同时间尺度的买卖压）
            def _imbalance(b: list, a: list) -> str | None:
                bn = sum((lvl.size * lvl.price for lvl in b), Decimal("0"))
                an = sum((lvl.size * lvl.price for lvl in a), Decimal("0"))
                tot = bn + an
                if tot <= 0: return None
                return str((bn / tot).quantize(Decimal("0.001")))
            imb_1 = _imbalance(top_bids[:1], top_asks[:1])
            imb_3 = _imbalance(top_bids[:3], top_asks[:3])
            imb_5 = _imbalance(top_bids[:5], top_asks[:5])
            # book entropy（5 层 size 分布的均匀度，越接近 1 越平均，越接近 0 越集中）
            def _entropy(levels: list) -> str | None:
                import math
                sizes = [float(lvl.size) for lvl in levels if lvl.size > 0]
                if not sizes: return None
                total = sum(sizes)
                if total <= 0: return None
                probs = [s / total for s in sizes]
                ent = -sum(p * math.log2(p) for p in probs if p > 0)
                max_ent = math.log2(len(sizes)) if len(sizes) > 1 else 1
                return str(round(ent / max_ent, 3)) if max_ent > 0 else "0"
            bid_entropy = _entropy(top_bids)
            ask_entropy = _entropy(top_asks)
            # price impact: 买 $X 推 best_ask 上涨 Y bps（模拟逐档吃 ask）
            def _buy_impact(asks: list, target_usdc: Decimal) -> dict | None:
                if not asks: return None
                remaining = target_usdc
                spent = Decimal("0")
                filled_shares = Decimal("0")
                last_price = asks[0].price
                for lvl in asks:
                    if remaining <= 0: break
                    max_shares = remaining / lvl.price
                    use_shares = min(lvl.size, max_shares)
                    spent += use_shares * lvl.price
                    filled_shares += use_shares
                    remaining -= use_shares * lvl.price
                    last_price = lvl.price
                if filled_shares <= 0: return None
                avg_fill = spent / filled_shares
                first_ask = asks[0].price
                slippage_bps = int(((avg_fill - first_ask) / first_ask) * 10000) if first_ask > 0 else 0
                return {
                    "target_usdc": str(target_usdc),
                    "filled_shares": str(filled_shares.quantize(Decimal("0.01"))),
                    "avg_fill_price": str(avg_fill.quantize(Decimal("0.0001"))),
                    "last_level_price": str(last_price),
                    "slippage_bps": slippage_bps,
                    "fully_filled": remaining <= Decimal("0.01"),
                }
            decision_snapshot["orderbook"] = {
                "best_bid": str(ob.best_bid) if ob.best_bid is not None else None,
                "best_ask": str(ob.best_ask) if ob.best_ask is not None else None,
                "best_bid_size": str(ob.best_bid_size) if ob.best_bid_size is not None else None,
                "best_ask_size": str(ob.best_ask_size) if ob.best_ask_size is not None else None,
                "spread": str(ob.spread) if ob.spread is not None else None,
                "microprice": str(ob.microprice) if ob.microprice is not None else None,
                "ask_depth": str(ob.buyable_ask_depth()),
                "tick_size": str(ob.tick_size) if ob.tick_size is not None else None,
                "received_at": ob.received_at.isoformat() if ob.received_at is not None else None,
                "top_bids": [{"price": str(lvl.price), "size": str(lvl.size)} for lvl in top_bids],
                "top_asks": [{"price": str(lvl.price), "size": str(lvl.size)} for lvl in top_asks],
                "bid_total_depth_5": str(sum((lvl.size * lvl.price for lvl in top_bids), Decimal("0"))),
                "ask_total_depth_5": str(sum((lvl.size * lvl.price for lvl in top_asks), Decimal("0"))),
                # 多档统计（新加 P3）
                "imbalance_1": imb_1,
                "imbalance_3": imb_3,
                "imbalance_5": imb_5,
                "bid_entropy": bid_entropy,
                "ask_entropy": ask_entropy,
                "price_impact_5usdc": _buy_impact(top_asks, Decimal("5")),
                "price_impact_25usdc": _buy_impact(top_asks, Decimal("25")),
                "price_impact_100usdc": _buy_impact(top_asks, Decimal("100")),
            }
        if plan.metadata is not None:
            # 完整透传所有策略输出的信号字段（不需逐项列）：goalserve odds、
            # orderbook_direction（OFI/microprice 漂移）、live_game（含 baseball/
            # basketball/tennis state 各局段比分）、math_lock_prob、kelly 决策、
            # 排名信号、家族识别等。自由 dict 透传到 audit payload 供复盘 + 训练。
            metadata_keys_of_interest = (
                "orderbook_direction",
                "goalserve_moneyline",
                "goalserve_totals",
                "goalserve_spread",
                "goalserve_halftime",
                "live_game",
                "math_lock_prob",
                "math_lock_metadata",
                "live_match",
                "tail_metadata",
                "kelly_stake",
                "kelly_f_star",
                "edge_net",
                "edge_gross",
                "fair_value",
                "fair_value_source",
                "true_p",
                "devig",
                "score_breakdown",
                "scope",
            )
            for key in metadata_keys_of_interest:
                value = plan.metadata.get(key)
                if value is not None:
                    decision_snapshot[key] = value
            # 比赛进度量化（纯统计指标，不参与决策）：跨运动统一 progress_pct +
            # phase + critical_moment + segment_label。复盘/未来 ML 训练用。
            live_game = decision_snapshot.get("live_game")
            if isinstance(live_game, dict):
                progress = _compute_game_progress_from_dict(live_game)
                if progress:
                    decision_snapshot["game_progress"] = progress
            # 赔率时序采样：把 goalserve_ml/totals/spread 推到 _ODDS_DRIFT_STORE
            # 5s 节流，供 /runtime/odds-drift 查询漂移趋势
            market_slug = plan.market.market_slug if plan.market is not None else None
            if market_slug:
                _record_odds_drift(
                    market_slug,
                    decision_snapshot.get("goalserve_moneyline"),
                    decision_snapshot.get("goalserve_totals"),
                    decision_snapshot.get("goalserve_spread"),
                )
            # 兜底：所有以 "goalserve_" / "dynamic_" / "live_state_" 前缀的字段全透
            for key, value in plan.metadata.items():
                if value is None or key in decision_snapshot:
                    continue
                if key.startswith(("goalserve_", "dynamic_", "live_state_", "ofi_", "math_")):
                    decision_snapshot[key] = value
        try:
            await self._event_bus.publish(
                OutboxPriority.P3,
                DomainEvent(
                    trace_id=plan.trace_id or event.trace_id,
                    event_type=DomainEventType.ALLOCATION_DECISION_RECORDED,
                    event_id=uuid4().hex,
                    market_slug=plan.market.market_slug if plan.market is not None else None,
                    condition_id=event.condition_id,
                    token_id=event.token_id,
                    reason=allocation_plan.reason or "",
                    payload={
                        "candidates": candidates,
                        "selected_condition_ids": selected,
                        "skipped_reasons": skipped_reasons,
                        "total_budget_usdc": str(allocation_plan.total_budget_usdc),
                        "buy_budget_usdc": str(allocation_plan.allocated_budget_usdc),
                        "allocator": TRADING_DECISION_WORKER_ORIGIN,
                        "decision_snapshot": decision_snapshot,
                    },
                ),
            )
        except Exception:
            return

    async def _publish(
        self,
        event_type: DomainEventType,
        *,
        trace_id: str,
        market_slug: str | None,
        condition_id: str | None,
        token_id: str | None,
        reason: str = "",
        payload: Mapping[str, object] | None = None,
        priority: OutboxPriority = OutboxPriority.P0,
    ) -> DomainEvent:
        payload_dict: dict[str, object] = {"origin": TRADING_DECISION_WORKER_ORIGIN}
        if payload is not None:
            payload_dict.update(payload)
        event = DomainEvent(
            trace_id=trace_id,
            event_type=event_type,
            event_id=uuid4().hex,
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
            reason=reason,
            created_at=_utc_now(),
            payload=payload_dict,
        )
        if self._event_bus is not None:
            await self._event_bus.publish(priority, event)
        return event

    def _entry_metadata(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> dict[str, object]:
        """构造入场策略 metadata。

        Worker 只合并事件事实和外部 provider 提供的补充事实，不解释具体策略字段。
        """

        metadata: dict[str, object] = dict(event.payload)
        # 注入 orderbook_direction（10s 窗口 OFI/microprice/momentum 综合方向信号），
        # 与 exit_metadata 同源——入场决策必须能识别"盘口已单边下杀"场景，否则
        # 仅看 best_ask < fair_value 会在 ask 厚 bid 薄、microprice 快速下走时
        # 强行进场，落地即亏（实战案例：mlb spread BUY @ 0.31 → 20s 后 SELL @ 0.26）。
        direction = self._fetch_orderbook_direction(event.token_id)
        if direction is not None:
            metadata["orderbook_direction"] = direction
        if self._entry_metadata_provider is None:
            return metadata
        try:
            extra_metadata = self._entry_metadata_provider(event, snapshot)
        except Exception as exc:
            metadata["entry_metadata_provider_error"] = str(exc)
            return metadata
        if extra_metadata:
            metadata.update(dict(extra_metadata))
        return metadata

    def bind_heartbeat(self, heartbeat: HeartbeatCallback | None) -> None:
        """Runtime 装配 supervisor.heartbeat_worker 的轻量适配点；测试可注入假回调。"""

        self._heartbeat = heartbeat

    @property
    def suppressed_allocation_emits(self) -> int:
        """已被 dedup 抑制的 allocation_decision_recorded 事件数，供 observability/admin 观察。"""

        return self._suppressed_allocation_emits

    def _emit_heartbeat(self, *, detail: str) -> None:
        """对外发心跳——supervisor 看不到心跳就视为 worker 卡死。

        任何回调异常都吞掉：观测路径绝不能反向阻塞 P0 主链路（CLAUDE.md §7）。
        """

        callback = self._heartbeat
        if callback is None:
            return
        try:
            callback(detail=detail)
        except Exception:
            # 故意吞掉异常：心跳是观测副作用，不能影响交易决策路径。
            return

    def _idle_detail(self, prefix: str) -> str:
        return f"{prefix} qd={self._trading_queue_depth_safe()}"

    def _processed_detail(self, event: DomainEvent) -> str:
        return f"processed event_type={event.event_type} qd={self._trading_queue_depth_safe()}"

    def _trading_queue_depth_safe(self) -> int:
        bus = self._event_bus
        if bus is None:
            return 0
        try:
            return int(bus.trading_queue_depth())
        except Exception:
            return 0

    def _snapshot(self) -> AccountSnapshot | None:
        if self._account_state_store is not None:
            return self._account_state_store.snapshot()
        return None

    def _build_positions_provider(self) -> PositionsProvider:
        if self._account_state_store is None:
            return lambda: ()
        account_state_store = self._account_state_store
        return lambda: account_state_store.snapshot().positions

    def _build_open_orders_provider(self) -> OpenOrdersProvider:
        if self._account_state_store is None:
            return lambda: ()
        account_state_store = self._account_state_store
        return lambda: account_state_store.snapshot().open_orders

    def _transition_market(self, market: Market | None, lifecycle: MarketLifecycle | None) -> None:
        if market is None or lifecycle is None:
            return
        self._market_lifecycle[market.condition_id] = lifecycle
        self._market_lifecycle.move_to_end(market.condition_id)
        while len(self._market_lifecycle) > _MARKET_LIFECYCLE_DICT_CAP:
            self._market_lifecycle.popitem(last=False)
        self._record_lifecycle(market.condition_id, lifecycle)

    def _record_lifecycle(self, condition_id: str, lifecycle: MarketLifecycle) -> None:
        if condition_id not in self._lifecycle_timeline:
            if len(self._lifecycle_timeline) >= _LIFECYCLE_MARKET_CAP:
                self._lifecycle_timeline.popitem(last=False)
            self._lifecycle_timeline[condition_id] = []
        else:
            self._lifecycle_timeline.move_to_end(condition_id)
        history = self._lifecycle_timeline[condition_id]
        history.append((lifecycle, _utc_now()))
        if len(history) > _LIFECYCLE_HISTORY_PER_MARKET:
            del history[: len(history) - _LIFECYCLE_HISTORY_PER_MARKET]

    def _transition_market_by_result(self, order_result: OrderResult, lifecycle: MarketLifecycle) -> None:
        market = market_from_result(order_result)
        self._transition_market(market, lifecycle)

    def _transition_from_order_result(self, order_result: OrderResult) -> None:
        if order_result.side is None:
            return
        side = str(order_result.side).upper()
        if side == "BUY":
            if order_result.status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}:
                self._transition_market_by_result(order_result, MarketLifecycle.POSITION_OPEN)
            elif order_result.status == OrderResultStatus.LIVE:
                # 买单挂单未成交 → 暂停该市场直到 reconciler 处理。与 _pause_market 保持
                # 同步，确保 AccountStateStore.is_market_paused 返回 True，让 admin 可见。
                self._pause_market(order_result.condition_id, reason="resting_buy_order")
            elif order_result.status == OrderResultStatus.NO_FILL:
                self._transition_market_by_result(order_result, MarketLifecycle.ENTRY_READY)
            # REJECTED/FAILED/UNKNOWN_TIMEOUT 不再触发 lifecycle 转换——失败本身已
            # 通过 order_rejected/order_state_updated 落 audit，下次 WS push 会重新
            # 评估，RiskManager 看 AccountStateStore 真实状态决定能否再下单。
            # lifecycle 保持上次成功状态（如 WATCHING_ORDERBOOK / ENTRY_READY），
            # 不再写 ENTRY_REJECTED 死状态。
        elif side == "SELL":
            if order_result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}:
                self._transition_market_by_result(order_result, MarketLifecycle.FOLLOW_UP_ORDER_OPEN)
            elif order_result.status == OrderResultStatus.FULL_FILL:
                self._transition_market_by_result(order_result, MarketLifecycle.POSITION_OPEN)

    def _pause_market(self, condition_id: str | None, *, reason: str) -> None:
        """§11 框架不自动 pause:仅在 worker 内部 lifecycle 标 PAUSED(影响 worker 决策),
        不再调 account_state.pause_market(避免框架自动写入 _market_pauses).
        admin 仍可通过 /markets/{cid}/pause 手动 pause.
        """
        if condition_id is None:
            return
        self._market_lifecycle[condition_id] = MarketLifecycle.PAUSED
        self._market_lifecycle.move_to_end(condition_id)
        while len(self._market_lifecycle) > _MARKET_LIFECYCLE_DICT_CAP:
            self._market_lifecycle.popitem(last=False)
        self._record_lifecycle(condition_id, MarketLifecycle.PAUSED)

    def _state_for_market(self, market: Market | None) -> MarketLifecycle | None:
        if market is None:
            return None
        return self._market_lifecycle.get(market.condition_id)

    def _state_for_market_by_key(self, condition_id: str | None) -> MarketLifecycle | None:
        if condition_id is None:
            return None
        return self._market_lifecycle.get(condition_id)

    @property
    def skip_reason_histogram(self) -> dict[tuple[str, str], int]:
        """P3.5: (strategy_id, reason) → count。只读快照，admin 查询用。"""
        return dict(self._skip_reason_histogram)

    @property
    def lifecycle_timeline(self) -> dict[str, list[tuple[MarketLifecycle, datetime]]]:
        """P3.6: condition_id → [(lifecycle, utc_timestamp), …]。只读快照，admin 查询用。"""
        return {cid: list(entries) for cid, entries in self._lifecycle_timeline.items()}

    def _market_fromsnapshot_position(
        self,
        snapshot: AccountSnapshot,
        condition_id: str | None,
        token_id: str | None,
    ) -> Market | None:
        if condition_id is None or token_id is None:
            return None
        position = snapshot.get_position(condition_id, token_id)
        if position is None:
            return None
        market = self._trading_decision_service.resolve_market(condition_id=condition_id, token_id=token_id)
        return market

    async def _execute_managed_intent(
        self,
        intent: ManagedOrderIntent,
        *,
        snapshot: AccountSnapshot | None,
    ) -> TradingReviewResult:
        market = self._trading_decision_service.resolve_market(
            condition_id=intent.condition_id,
            token_id=intent.token_id,
        )
        if isinstance(intent, CancelOrderIntent):
            return await self._trading_service.cancel(intent)
        if isinstance(intent, ReplaceOrderIntent):
            result = await self._trading_service.replace(intent)
            logger.info(
                "replace_intent_result",
                extra={
                    "order_id": intent.order_id,
                    "new_price": str(intent.new_price),
                    "size_shares": str(intent.size_shares),
                    "submitted": result.submitted,
                    "submission_error": result.submission_error,
                    "order_result_status": (
                        result.order_result.status.value if result.order_result is not None and result.order_result.status is not None else None
                    ),
                    "order_result_reason": (
                        result.order_result.reason if result.order_result is not None else None
                    ),
                },
            )
            return result
        return await self._trading_service.review_intent(
            intent,
            market=market,
            orderbook=self._trading_decision_service.lookup_orderbook(intent.token_id),
            position=snapshot_position(snapshot, intent.condition_id, intent.token_id),
            open_orders=(
                snapshot.open_orders_for_market(intent.condition_id, intent.token_id)
                if snapshot is not None
                else ()
            ),
            condition_open_orders=_condition_orders(snapshot, intent.condition_id),
            condition_positions=_condition_positions(snapshot, intent.condition_id),
            classification_passed=True,
            balance_usdc=snapshot_available_usdc(snapshot),
            allowance_usdc=snapshot_allowance(snapshot),
            bankroll_usdc=_resolve_bankroll_for_review(
                portfolio_budget_usdc=self._portfolio_budget_usdc,
                snapshot=snapshot,
            ),
            kelly_max_position_fraction=self._kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=self._kelly_round_up_max_overbet_ratio,

            operation=intent.side.value.lower(),
        )

    def _account_projector(self) -> AccountStateProjector | None:
        if self._account_state_store is None:
            return None
        return AccountStateProjector(
            self._account_state_store,
            strategy_id=self._trading_decision_service.strategy_id,
        )


def _match_position(
    positions: tuple[Position, ...],
    condition_id: str,
    token_id: str,
) -> Position | None:
    for position in positions:
        if position.condition_id == condition_id and position.token_id == token_id:
            return position
    return None


def _plan_allows_position_increase(plan: EntryPlan) -> bool:
    """判断计划是否是策略显式标记的受控加仓。

    依据策略在决策对象上声明的 intent_tags（含 ``"scale_in"``）+ intent
    自身的 ``allow_open_exit_overlap`` 双重标记，避免读策略私有 metadata 字符串。
    """

    intent = plan.intent
    if intent is None or not intent.allow_open_exit_overlap:
        return False
    tags = intent.intent_tags or frozenset()
    return "scale_in" in tags


def _state_allows_position_increase(state: MarketLifecycle, plan: EntryPlan) -> bool:
    """只有持仓相关生命周期允许策略受控加仓继续走主链路。"""

    return state in POSITION_INCREASE_LIFECYCLES and _plan_allows_position_increase(plan)


def _state_allows_entry_attempt(state: MarketLifecycle, plan: EntryPlan) -> bool:
    """是否允许新的入场信号进入主链路。

    简化后只保留两条硬规则：
    - PAUSED：人工/auto 暂停明确表达"系统不该对该 market 下单"，必须 block。
    - 已有持仓（POSITION_OPEN / FOLLOW_UP_ORDER_OPEN）：加仓必须由策略 plan
      显式标记，防止意图不明的二次买入。

    其余状态（WATCHING_ORDERBOOK / ENTRY_READY / 失败后未转换的状态）一律允许
    重新评估——失败重试由 §17 哲学 + RiskManager 实时看 AccountStateStore 把关，
    不再用 lifecycle 状态机 lock 死。
    """

    if state == MarketLifecycle.PAUSED:
        return False
    if state in POSITION_INCREASE_LIFECYCLES:
        return _plan_allows_position_increase(plan)
    return True


def _match_open_orders(
    open_orders: tuple[Order, ...],
    condition_id: str,
    token_id: str,
) -> tuple[Order, ...]:
    return tuple(
        order
        for order in open_orders
        if order.condition_id == condition_id and order.token_id == token_id
    )


def _condition_orders(
    snapshot: AccountSnapshot | None,
    condition_id: str | None,
) -> tuple[Order, ...]:
    """同 condition_id 的全部 open orders（跨 token），用于 NEG_RISK 互斥检测。"""

    if snapshot is None or not condition_id:
        return ()
    return tuple(order for order in snapshot.open_orders if order.condition_id == condition_id)


def _condition_positions(
    snapshot: AccountSnapshot | None,
    condition_id: str | None,
) -> tuple[Position, ...]:
    """同 condition_id 的全部持仓（跨 token），用于 NEG_RISK 互斥检测。"""

    if snapshot is None or not condition_id:
        return ()
    return tuple(p for p in snapshot.positions if p.condition_id == condition_id)


def _resolve_bankroll_for_review(
    *,
    portfolio_budget_usdc: Decimal,
    snapshot: AccountSnapshot | None,
) -> Decimal:
    """与 EntryPlanner 内 ``_resolve_bankroll`` 一致的口径，避免 RiskManager 与
    EntryPlanner 用不同的 bankroll 数。"""

    if snapshot is None:
        bankroll = portfolio_budget_usdc
    else:
        bankroll = min(snapshot.available_usdc, portfolio_budget_usdc)
    if bankroll < Decimal("0"):
        return Decimal("0")
    return bankroll


def _match_position_for_event(
    snapshot: AccountSnapshot,
    event: DomainEvent,
) -> Position | None:
    """从当前热态中找到 position update 对应的持仓。"""

    if event.condition_id is not None and event.token_id is not None:
        return snapshot.get_position(event.condition_id, event.token_id)
    payload_position = event.payload.get("position")
    if isinstance(payload_position, Mapping):
        condition_id = payload_position.get("condition_id")
        token_id = payload_position.get("token_id")
        if condition_id is not None and token_id is not None:
            return snapshot.get_position(str(condition_id), str(token_id))
    payload_positions = event.payload.get("positions")
    if isinstance(payload_positions, (list, tuple)):
        for payload_item in payload_positions:
            if not isinstance(payload_item, Mapping):
                continue
            condition_id = payload_item.get("condition_id")
            token_id = payload_item.get("token_id")
            if condition_id is None or token_id is None:
                continue
            position = snapshot.get_position(str(condition_id), str(token_id))
            if position is not None:
                return position
    return None
