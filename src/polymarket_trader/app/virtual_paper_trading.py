"""基于真实运行态数据的虚拟盘演练。

虚拟盘只替换最后的订单提交端：market、orderbook、账户快照、直播状态 metadata、
策略配置、风控和可用时的订单签名都来自当前运行态；真正 submit 时由
``app/paper`` 撮合引擎模拟 level-by-level 成交 + 官方 fee 公式 + fill 状态机。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

from polymarket_trader.app.paper import PaperSubmitOnlyOrderClient, PaperVirtualLedger
from polymarket_trader.app.trading_decision_service import EntryPlan, TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import OrderResultStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.outbox.local_queue import LocalOutbox
from polymarket_trader.infra.polymarket.auth import PolymarketOrderExecutionClient
from polymarket_trader.infra.polymarket.order_executor import (
    PolymarketOrderExecutor,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.serialization import jsonable
from polymarket_trader.workers.trading_decision import TradingDecisionWorker
from polymarket_trader.workers.trading_decision import TradingDecisionWorkerResult

_REJECTION_SAMPLE_LIMIT = 12


async def run_virtual_paper_trade(
    runtime: Any,
    *,
    condition_id: str | None = None,
    token_id: str | None = None,
    market_slug: str | None = None,
) -> dict[str, Any]:
    """用真实运行态候选执行一次虚拟交易。

    未指定 market/token 时，自动选择当前第一个真实 `auto_execute` 候选；如果没有，
    返回真实候选拒绝原因样本，不伪造行情或直播状态。
    """

    selection = await _select_candidate(
        runtime,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
    )
    if selection.plan is None or not selection.plan.ready_to_trade or selection.plan.intent is None:
        return jsonable(
            {
                "status": "no_trade",
                "reason": selection.reason,
                "data_source": "real_runtime",
                "execution": "not_submitted",
                "selection": selection.as_payload(),
                "opportunity_funnel": selection.opportunity_funnel,
                "rejection_summary": selection.rejection_summary,
                "paper_pnl": _empty_paper_pnl(),
                "rejections": selection.rejections,
            }
        )

    ledger = PaperVirtualLedger()
    ledger.fund(selection.account.available_usdc)
    paper_client = PaperSubmitOnlyOrderClient(
        real_sign_client=_real_sign_client(runtime),
        market_lookup=lambda token_id: _resolve_market_by_token(runtime, token_id),
        orderbook_lookup=lambda token_id: _orderbook(runtime, token_id),
        ledger=ledger,
    )
    outbox = LocalOutbox(max_size=100)
    executor = PolymarketOrderExecutor(
        client=paper_client,
        outbox=outbox,
        sign_timeout_ms=1000,
        submit_timeout_ms=1000,
        critical_lock_timeout_ms=20,
    )
    account_store = _clone_account_store(selection.account)
    try:
        worker = TradingDecisionWorker(
            trading_decision_service=_trading_decision_service(runtime),
            trading_service=TradingService(executor=executor),
            account_state_store=account_store,
            portfolio_budget_usdc=_settings_decimal(runtime, "portfolio_budget_usdc"),
            max_order_usdc=_settings_decimal(runtime, "max_order_usdc"),
            max_market_usdc=_settings_decimal(runtime, "max_market_usdc"),
            max_total_usdc=_settings_decimal(runtime, "max_total_usdc"),
            max_open_orders=_settings_value(runtime, "max_open_orders"),
            order_retry_limit=_settings_value(runtime, "order_retry_limit"),
        )
        event = DomainEvent(
            trace_id=f"paper-{uuid4().hex[:8]}",
            event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
            event_id=f"paper-event-{uuid4().hex[:8]}",
            market_slug=selection.market.market_slug,
            event_slug=selection.market.event_slug,
            condition_id=selection.market.condition_id,
            token_id=selection.token_id,
            payload=selection.metadata,
        )
        result = await asyncio.wait_for(worker.process_event(event), timeout=5)
        return _result_payload(
            result=result,
            event=event,
            selection=selection,
            paper_client=paper_client,
            outbox=outbox,
            account_store=account_store,
            ledger=ledger,
        )
    finally:
        executor.close()


class _CandidateSelection:
    def __init__(
        self,
        *,
        market: Market,
        token_id: str,
        account: AccountSnapshot,
        metadata: Mapping[str, Any],
        plan: EntryPlan | None,
        reason: str,
        rejections: tuple[dict[str, Any], ...] = (),
        opportunity_funnel: Mapping[str, Any] | None = None,
        rejection_summary: Mapping[str, Any] | None = None,
    ) -> None:
        self.market = market
        self.token_id = token_id
        self.account = account
        self.metadata = dict(metadata)
        self.plan = plan
        self.reason = reason
        self.rejections = rejections
        self.opportunity_funnel = dict(opportunity_funnel or _empty_opportunity_funnel())
        self.rejection_summary = dict(rejection_summary or _rejection_summary(()))

    def as_payload(self) -> dict[str, Any]:
        summary = None if self.plan is None else self.plan.summary
        extras = dict(summary.extras) if summary is not None else {}
        return {
            "condition_id": self.market.condition_id,
            "market_slug": self.market.market_slug,
            "event_slug": self.market.event_slug,
            "token_id": self.token_id,
            "plan_ready": None if self.plan is None else self.plan.ready_to_trade,
            "plan_reason": None if self.plan is None else self.plan.reason,
            "action": None if summary is None else summary.action or None,
            "reason": None if summary is None else summary.reason or None,
            "execution_permission": extras.get("execution_permission"),
            "metadata": self.metadata,
        }


async def _select_candidate(
    runtime: Any,
    *,
    condition_id: str | None,
    token_id: str | None,
    market_slug: str | None,
) -> _CandidateSelection:
    account = _account_snapshot(runtime)
    explicit_market = _resolve_market(
        runtime,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
    )
    source_markets = (explicit_market,) if explicit_market is not None else _registry_markets(runtime)
    opportunity_funnel = _empty_opportunity_funnel(
        scan_scope="explicit" if explicit_market is not None else "registry",
        source_market_count=len(source_markets),
        source_token_count=sum(len(market.token_ids) for market in source_markets if market is not None),
    )
    all_rejections: list[dict[str, Any]] = []
    first_selection: _CandidateSelection | None = None
    for index, market in enumerate(source_markets, start=1):
        if market is None:
            continue
        if index % 20 == 0:
            await asyncio.sleep(0)
        metadata = _entry_metadata_for_market(runtime, market)
        for outcome in market.outcomes:
            if token_id is not None and outcome.token_id != token_id:
                continue
            orderbook = _orderbook(runtime, outcome.token_id)
            if orderbook is None:
                rejection = _rejection(
                    market,
                    outcome.token_id,
                    reason="orderbook_unavailable",
                    stage="orderbook",
                )
                _record_rejection(opportunity_funnel, rejection)
                all_rejections.append(rejection)
                continue
            opportunity_funnel["orderbook_available_count"] += 1
            record = _entry_metadata_record_for_market(runtime, market)
            if record is not None and record.live_state_payload:
                opportunity_funnel["metadata_available_count"] += 1
            plan = _build_plan(
                runtime,
                market=market,
                token_id=outcome.token_id,
                orderbook=orderbook,
                account=account,
                metadata=metadata,
            )
            _record_plan(opportunity_funnel, plan)
            summary = plan.summary
            action = "" if summary is None else str(summary.action or "")
            sports_reason = plan.reason or ("" if summary is None else str(summary.reason or ""))
            rejection_summary = _rejection_summary(tuple(all_rejections))
            selection = _CandidateSelection(
                market=market,
                token_id=outcome.token_id,
                account=account,
                metadata=metadata,
                plan=plan,
                reason=sports_reason,
                opportunity_funnel=opportunity_funnel,
                rejection_summary=rejection_summary,
            )
            if sports_reason and _selection_is_better(selection, first_selection):
                first_selection = selection
            if plan.ready_to_trade and plan.intent is not None and action == "auto_execute":
                opportunity_funnel["selected_for_execution_count"] += 1
                opportunity_funnel["scan_completed"] = False
                selection.opportunity_funnel = dict(opportunity_funnel)
                selection.rejection_summary = _rejection_summary(tuple(all_rejections))
                return selection
            if sports_reason:
                rejection = _rejection(market, outcome.token_id, reason=sports_reason, plan=plan, stage="plan")
                _record_rejection(opportunity_funnel, rejection)
                all_rejections.append(rejection)
    if first_selection is not None:
        first_selection.rejections = _sample_rejections(tuple(all_rejections))
        first_selection.reason = first_selection.reason or "no_auto_execute_candidate"
        opportunity_funnel["scan_completed"] = True
        first_selection.opportunity_funnel = dict(opportunity_funnel)
        first_selection.rejection_summary = _rejection_summary(tuple(all_rejections))
        return first_selection
    market = explicit_market or next((item for item in source_markets if item is not None), _empty_market())
    opportunity_funnel["scan_completed"] = True
    return _CandidateSelection(
        market=market,
        token_id=token_id or (market.token_ids[0] if market.token_ids else ""),
        account=account,
        metadata={},
        plan=None,
        reason="no_candidate_with_live_metadata",
        rejections=_sample_rejections(tuple(all_rejections)),
        opportunity_funnel=opportunity_funnel,
        rejection_summary=_rejection_summary(tuple(all_rejections)),
    )


def _build_plan(
    runtime: Any,
    *,
    market: Market,
    token_id: str,
    orderbook: Any,
    account: AccountSnapshot,
    metadata: Mapping[str, Any],
) -> EntryPlan:
    return _trading_decision_service(runtime).build_entry_plan(
        market=market,
        orderbook=orderbook,
        token_id=token_id,
        trace_id=f"paper-plan-{uuid4().hex[:8]}",
        portfolio_budget_usdc=_settings_decimal(runtime, "portfolio_budget_usdc"),
        available_usdc=account.available_usdc,
        max_order_usdc=_settings_decimal(runtime, "max_order_usdc"),
        max_market_usdc=_settings_decimal(runtime, "max_market_usdc"),
        max_total_usdc=_settings_decimal(runtime, "max_total_usdc"),
        positions=account.positions,
        open_orders=account.open_orders,
        metadata=metadata,
    )


def _result_payload(
    *,
    result: TradingDecisionWorkerResult | None,
    event: DomainEvent,
    selection: _CandidateSelection,
    paper_client: PaperSubmitOnlyOrderClient,
    outbox: LocalOutbox,
    account_store: AccountStateStore,
    ledger: PaperVirtualLedger,
) -> dict[str, Any]:
    plan = None if result is None else result.plan
    review = None if result is None else result.review
    follow_up_review = None if result is None or not result.follow_up_reviews else result.follow_up_reviews[0]
    entry_order = None if review is None else review.order_result
    follow_up_order = None if follow_up_review is None else follow_up_review.order_result
    entry_success = bool(
        plan is not None
        and plan.ready_to_trade
        and review is not None
        and review.risk_decision is not None
        and review.risk_decision.passed
        and entry_order is not None
        and entry_order.status == OrderResultStatus.FULL_FILL
    )
    follow_up_required = bool(result is not None and result.follow_up_intents)
    follow_up_success = bool(
        not follow_up_required
        or (
            follow_up_review is not None
            and follow_up_review.risk_decision is not None
            and follow_up_review.risk_decision.passed
            and follow_up_order is not None
            and follow_up_order.status == OrderResultStatus.LIVE
        )
    )
    success = entry_success and follow_up_success
    return jsonable(
        {
            "status": "ok" if success else "failed",
            "data_source": "real_runtime",
            "execution": "paper_submit_only",
            "virtual_boundary": "order_submission_payment",
            "trace_id": event.trace_id,
            "signing": {
                "source": paper_client.signing_source,
                "real_signing_used": paper_client.signing_source == "real_trading_client",
            },
            "selection": selection.as_payload(),
            "opportunity_funnel": selection.opportunity_funnel,
            "rejection_summary": selection.rejection_summary,
            "paper_pnl": _paper_pnl_payload(
                entry_order=entry_order,
                follow_up_intent=None if result is None or not result.follow_up_intents else result.follow_up_intents[0],
                follow_up_order=follow_up_order,
                ledger=ledger,
            ),
            "paper_ledger": {
                "available_usdc": _decimal_text(ledger.available_usdc),
                "fees_accrued_usdc": _decimal_text(ledger.fees_accrued_usdc),
                "positions": {token: _decimal_text(shares) for token, shares in ledger.positions.items()},
                "cost_basis_usdc": {
                    token: _decimal_text(cost) for token, cost in ledger.cost_basis_usdc.items()
                },
            },
            "paper_simulations": [
                {
                    "trace_id": trace_id,
                    "status": outcome.response.status if isinstance(outcome.response.status, str)
                    else getattr(outcome.response.status, "value", None),
                    "reason": outcome.response.reason,
                    "matched_shares": _decimal_text(outcome.response.matched_shares),
                    "spent_usdc": _decimal_text(outcome.response.spent_usdc),
                    "fee_usdc": None if outcome.fee_quote is None else _decimal_text(outcome.fee_quote.fee_usdc),
                    "fee_shares": (
                        None if outcome.fee_quote is None or outcome.fee_quote.fee_shares is None
                        else _decimal_text(outcome.fee_quote.fee_shares)
                    ),
                    "consumed_levels": (
                        []
                        if outcome.match_result is None
                        else [
                            {
                                "price": _decimal_text(level.price),
                                "shares": _decimal_text(level.shares),
                                "notional_usdc": _decimal_text(level.notional_usdc),
                            }
                            for level in outcome.match_result.consumed_levels
                        ]
                    ),
                }
                for trace_id, outcome in paper_client.simulations
            ],
            "summary": {
                "plan_ready": None if plan is None else plan.ready_to_trade,
                "entry_action": None if plan is None or plan.summary is None else (plan.summary.action or None),
                "entry_reason": None if plan is None or plan.summary is None else (plan.summary.reason or None),
                "entry_order_status": None if entry_order is None else entry_order.status,
                "follow_up_count": 0 if result is None else len(result.follow_up_intents),
                "follow_up_price": None if result is None or not result.follow_up_intents else result.follow_up_intents[0].price,
                "follow_up_order_status": None if follow_up_order is None else follow_up_order.status,
                "outbox_events": len(outbox.pending_events()),
            },
            "steps": _steps(result),
            "order_requests": [
                {
                    "phase": phase,
                    "action": request.action,
                    "virtual": phase in {"submit", "cancel", "replace"},
                    "side": None if request.side is None else request.side.value,
                    "price": request.price or request.new_price,
                    "amount_usdc": request.amount_usdc,
                    "size_shares": request.size_shares,
                    "order_type": None if request.order_type is None else request.order_type.value,
                    "idempotency_key": request.idempotency_key,
                }
                for phase, request in paper_client.requests
            ],
            "outbox_events": [
                {
                    "event_type": item.event_type,
                    "reason": item.reason,
                    "condition_id": item.condition_id,
                    "token_id": item.token_id,
                    "payload": item.payload,
                }
                for item in outbox.pending_events()
            ],
            "virtual_account_positions": account_store.snapshot().positions,
            "raw": {
                "plan": plan,
                "review": review,
                "follow_up_reviews": () if result is None else result.follow_up_reviews,
            },
        }
    )


def _steps(result: TradingDecisionWorkerResult | None) -> tuple[dict[str, Any], ...]:
    if result is None:
        return ({"key": "worker", "label": "交易 worker", "status": "failed", "detail": "worker 未返回结果"},)
    plan = result.plan
    review = result.review
    entry_order = None if review is None else review.order_result
    follow_up_review = result.follow_up_reviews[0] if result.follow_up_reviews else None
    follow_up_order = None if follow_up_review is None else follow_up_review.order_result
    follow_up_intent = result.follow_up_intents[0] if result.follow_up_intents else None
    if follow_up_intent is None:
        return (
            {"key": "real_data", "label": "真实候选数据", "status": "success", "detail": "来自当前 registry/orderbook/live metadata"},
            {"key": "plan", "label": "入场计划", "status": "success" if plan is not None and plan.ready_to_trade else "failed", "detail": None if plan is None else plan.reason or (plan.summary.reason if plan.summary else "")},
            {"key": "entry_risk", "label": "BUY 风控", "status": _risk_status(review), "detail": _risk_reason(review)},
            {"key": "entry_order", "label": "BUY 虚拟提交", "status": "success" if entry_order is not None and entry_order.status == OrderResultStatus.FULL_FILL else "failed", "detail": None if entry_order is None else entry_order.reason},
            {"key": "settlement", "label": "等待结算", "status": "success", "detail": "不自动挂 follow-up SELL"},
        )
    return (
        {"key": "real_data", "label": "真实候选数据", "status": "success", "detail": "来自当前 registry/orderbook/live metadata"},
        {"key": "plan", "label": "入场计划", "status": "success" if plan is not None and plan.ready_to_trade else "failed", "detail": None if plan is None else plan.reason or (plan.summary.reason if plan.summary else "")},
        {"key": "entry_risk", "label": "BUY 风控", "status": _risk_status(review), "detail": _risk_reason(review)},
        {"key": "entry_order", "label": "BUY 虚拟提交", "status": "success" if entry_order is not None and entry_order.status == OrderResultStatus.FULL_FILL else "failed", "detail": None if entry_order is None else entry_order.reason},
        {"key": "follow_up", "label": "跟单 SELL 计划", "status": "success" if follow_up_intent is not None else "failed", "detail": None if follow_up_intent is None else f"GTC SELL @ {follow_up_intent.price}"},
        {"key": "follow_up_risk", "label": "SELL 风控", "status": _risk_status(follow_up_review), "detail": _risk_reason(follow_up_review)},
        {"key": "exit_order", "label": "SELL 虚拟提交", "status": "success" if follow_up_order is not None and follow_up_order.status == OrderResultStatus.LIVE else "failed", "detail": None if follow_up_order is None else follow_up_order.reason},
    )


def _risk_status(review: Any | None) -> str:
    if review is None or review.risk_decision is None:
        return "failed"
    return "success" if review.risk_decision.passed else "failed"


def _risk_reason(review: Any | None) -> str:
    if review is None or review.risk_decision is None:
        return "missing_review"
    return review.risk_decision.reason


def _rejection(
    market: Market,
    token_id: str,
    *,
    reason: str,
    stage: str,
    plan: EntryPlan | None = None,
) -> dict[str, Any]:
    summary = None if plan is None else plan.summary
    extras = dict(summary.extras) if summary is not None else {}
    return {
        "condition_id": market.condition_id,
        "market_slug": market.market_slug,
        "token_id": token_id,
        "stage": stage,
        "reason": reason,
        "plan_ready": None if plan is None else plan.ready_to_trade,
        "plan_reason": None if plan is None else plan.reason,
        "action": None if summary is None else (summary.action or None),
        "summary_reason": None if summary is None else (summary.reason or None),
        "risk_reason": extras.get("risk_reason"),
        "execution_permission": extras.get("execution_permission"),
        "market_family": extras.get("market_family"),
        "market_type": None if summary is None else (summary.market_type or None),
        "game_status": extras.get("game_status"),
        "best_ask": None if summary is None or summary.best_ask is None else str(summary.best_ask),
        "line": None if summary is None or summary.line is None else str(summary.line),
    }


def _empty_opportunity_funnel(
    *,
    scan_scope: str = "registry",
    source_market_count: int = 0,
    source_token_count: int = 0,
) -> dict[str, Any]:
    """返回虚拟盘机会漏斗的初始结构。"""

    return {
        "scan_scope": scan_scope,
        "scan_completed": False,
        "source_market_count": source_market_count,
        "source_token_count": source_token_count,
        "evaluated_token_count": 0,
        "orderbook_available_count": 0,
        "metadata_available_count": 0,
        "plan_built_count": 0,
        "plan_ready_count": 0,
        "auto_execute_count": 0,
        "selected_for_execution_count": 0,
        "market_family_counts": {},
        "market_type_counts": {},
        "game_status_counts": {},
        "action_counts": {},
        "execution_permission_counts": {},
    }


def _record_plan(opportunity_funnel: dict[str, Any], plan: EntryPlan) -> None:
    summary = plan.summary
    extras = dict(summary.extras) if summary is not None else {}
    action = "" if summary is None else str(summary.action or "")
    opportunity_funnel["evaluated_token_count"] += 1
    opportunity_funnel["plan_built_count"] += 1
    if plan.ready_to_trade:
        opportunity_funnel["plan_ready_count"] += 1
    if action == "auto_execute":
        opportunity_funnel["auto_execute_count"] += 1
    _increment_count(opportunity_funnel["market_family_counts"], extras.get("market_family"))
    _increment_count(opportunity_funnel["market_type_counts"], None if summary is None else summary.market_type)
    _increment_count(opportunity_funnel["game_status_counts"], extras.get("game_status"))
    _increment_count(opportunity_funnel["action_counts"], action or None)
    _increment_count(opportunity_funnel["execution_permission_counts"], extras.get("execution_permission"))


def _record_rejection(opportunity_funnel: dict[str, Any], rejection: Mapping[str, Any]) -> None:
    if rejection.get("stage") == "orderbook":
        _increment_count(opportunity_funnel["action_counts"], "orderbook_unavailable")


def _sample_rejections(rejections: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """按诊断价值抽样拒绝原因，优先展示真实单场市场。"""

    ranked = tuple(sorted(rejections, key=_rejection_rank))
    samples: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for rejection in ranked:
        key = (
            str(rejection.get("reason") or ""),
            str(rejection.get("market_family") or ""),
            str(rejection.get("stage") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        samples.append(rejection)
        if len(samples) >= _REJECTION_SAMPLE_LIMIT:
            return tuple(samples)
    for rejection in ranked:
        if rejection in samples:
            continue
        samples.append(rejection)
        if len(samples) >= _REJECTION_SAMPLE_LIMIT:
            break
    return tuple(samples)


def _rejection_rank(rejection: Mapping[str, Any]) -> tuple[int, int, int, str]:
    family = str(rejection.get("market_family") or "")
    game_status = str(rejection.get("game_status") or "")
    reason = str(rejection.get("reason") or "")
    family_rank = 0 if family == "single_game" else (1 if family else 2)
    return (family_rank, _game_status_rank(game_status), _reason_diagnostic_rank(reason), reason)


def _selection_is_better(candidate: _CandidateSelection, current: _CandidateSelection | None) -> bool:
    if current is None:
        return True
    return _selection_rank(candidate) < _selection_rank(current)


def _selection_rank(selection: _CandidateSelection) -> tuple[int, int, int, str]:
    summary = None if selection.plan is None else selection.plan.summary
    extras = dict(summary.extras) if summary is not None else {}
    family = str(extras.get("market_family") or "")
    game_status = str(extras.get("game_status") or "")
    reason = str(selection.reason or "")
    family_rank = 0 if family == "single_game" else (1 if family else 2)
    return (family_rank, _game_status_rank(game_status), _reason_diagnostic_rank(reason), reason)


def _game_status_rank(game_status: str) -> int:
    """虚拟盘排障优先展示已经开赛的真实候选。"""

    normalized = game_status.strip().lower()
    if normalized == "live":
        return 0
    if normalized == "paused":
        return 1
    if normalized == "scheduled":
        return 2
    if normalized in {"ended", "cancelled", "postponed", "retired"}:
        return 3
    if normalized and normalized != "unknown":
        return 4
    return 5


def _reason_diagnostic_rank(reason: str) -> int:
    """同一比赛状态下优先展示最能指导下一步排障的拒绝原因。"""

    normalized = reason.strip().lower()
    priority = {
        "stale_game_state": 0,
        "missing_live_game_state": 1,
        "missing_best_ask": 2,
        "liquidity_below_min": 3,
        "spread_above_max": 4,
        "price_above_entry_max": 5,
        "tennis_not_late_enough": 6,
        "outcome_not_locked": 7,
        "open_exit_detected": 8,
        "game_not_live": 9,
    }
    return priority.get(normalized, 10)


def _increment_count(bucket: dict[str, int], key: Any) -> None:
    label = str(key or "unknown")
    bucket[label] = int(bucket.get(label, 0)) + 1


def _rejection_summary(rejections: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    """聚合未执行原因，避免只看前几条样本误判机会来源。"""

    return {
        "total": len(rejections),
        "truncated": len(rejections) > 12,
        "by_reason": _counter_payload(rejection.get("reason") for rejection in rejections),
        "by_action": _counter_payload(rejection.get("action") for rejection in rejections),
        "by_execution_permission": _counter_payload(
            rejection.get("execution_permission") for rejection in rejections
        ),
        "by_stage": _counter_payload(rejection.get("stage") for rejection in rejections),
    }


def _counter_payload(values: Any) -> dict[str, int]:
    counter = Counter(str(value or "unknown") for value in values)
    return dict(sorted(counter.items()))


def _empty_paper_pnl() -> dict[str, Any]:
    return {
        "basis": "not_submitted",
        "realized": False,
        "profitable": False,
        "entry_price": None,
        "entry_spent_usdc": None,
        "entry_size_shares": None,
        "exit_price": None,
        "exit_order_status": None,
        "projected_exit_value_usdc": None,
        "projected_net_pnl_usdc": None,
        "projected_return_pct": None,
        "fees_paid_usdc": None,
        "warning": "no_virtual_entry_order",
    }


def _paper_pnl_payload(
    *,
    entry_order: Any | None,
    follow_up_intent: Any | None,
    follow_up_order: Any | None,
    ledger: PaperVirtualLedger,
) -> dict[str, Any]:
    """计算虚拟盘 P&L。

    BUY 实际花费来自撮合 raw_response 中的 ``gross_spent_usdc``（含 fee 之前的口袋
    花费）；账户净持有份额 = 撮合份额 - fee_shares，已落到 ledger。SELL 仍使用
    follow-up limit 价做投影（GTC 未实际成交），fee 仅含 buy 侧。
    """

    if entry_order is None:
        return _empty_paper_pnl()
    entry_spent = Decimal(str(getattr(entry_order, "spent_usdc", "0") or "0"))
    # entry_shares 已是 net of buy-side share fee（fill_engine 中 matched_shares 扣过）
    entry_shares = Decimal(str(getattr(entry_order, "matched_shares", "0") or "0"))
    fees_paid = ledger.fees_accrued_usdc
    if follow_up_intent is None:
        projected_exit_value = entry_shares
        projected_pnl = projected_exit_value - entry_spent
        projected_return_pct = Decimal("0") if entry_spent <= Decimal("0") else projected_pnl / entry_spent * Decimal("100")
        return {
            "basis": "entry_fill_waiting_for_settlement_net_after_taker_fees",
            "realized": False,
            "profitable": projected_pnl > Decimal("0"),
            "entry_price": _decimal_text(getattr(entry_order, "price", None)),
            "entry_spent_usdc": _decimal_text(entry_spent),
            "entry_size_shares": _decimal_text(entry_shares),
            "exit_price": _decimal_text(Decimal("1")),
            "exit_order_status": None,
            "projected_exit_value_usdc": _decimal_text(projected_exit_value),
            "projected_net_pnl_usdc": _decimal_text(projected_pnl),
            "projected_return_pct": _decimal_text(projected_return_pct),
            "fees_paid_usdc": _decimal_text(fees_paid),
            "warning": "projected_settlement_profit_not_realized_until_resolution",
        }
    exit_price = Decimal(str(getattr(follow_up_intent, "price", "0") or "0"))
    projected_exit_value = entry_shares * exit_price
    projected_pnl = projected_exit_value - entry_spent
    projected_return_pct = Decimal("0") if entry_spent <= Decimal("0") else projected_pnl / entry_spent * Decimal("100")
    return {
        "basis": "entry_fill_vs_follow_up_limit_net_after_taker_fees",
        "realized": False,
        "profitable": projected_pnl > Decimal("0"),
        "entry_price": _decimal_text(getattr(entry_order, "price", None)),
        "entry_spent_usdc": _decimal_text(entry_spent),
        "entry_size_shares": _decimal_text(entry_shares),
        "exit_price": _decimal_text(exit_price),
        "exit_order_status": None if follow_up_order is None else follow_up_order.status,
        "projected_exit_value_usdc": _decimal_text(projected_exit_value),
        "projected_net_pnl_usdc": _decimal_text(projected_pnl),
        "projected_return_pct": _decimal_text(projected_return_pct),
        "fees_paid_usdc": _decimal_text(fees_paid),
        "warning": "projected_limit_profit_not_realized_until_exit_fill_or_settlement",
    }


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(decimal_value.quantize(Decimal("0.000000000000000001")))


def _clone_account_store(snapshot: AccountSnapshot) -> AccountStateStore:
    store = AccountStateStore()
    store.update_balances(balance_usdc=snapshot.balance_usdc, allowance_usdc=snapshot.allowance_usdc)
    store.replace_positions(snapshot.positions)
    store.replace_open_orders(snapshot.open_orders)
    store.replace_fills(snapshot.fills)
    for pause in snapshot.market_pauses:
        store.pause_market(
            pause.condition_id,
            reason=pause.reason,
            source=pause.source,
            recoverable=pause.recoverable,
        )
    store.mark_user_ws_connected(snapshot.user_ws_connected)
    if snapshot.last_reconcile_at is not None:
        store.mark_reconciled(snapshot.last_reconcile_at)
    store.set_allow_new_entries(snapshot.allow_new_entries)
    return store


def _account_snapshot(runtime: Any) -> AccountSnapshot:
    account_state = getattr(runtime, "account_state_store", None)
    if account_state is not None:
        return account_state.snapshot()
    return AccountSnapshot()


def _registry_markets(runtime: Any) -> tuple[Market, ...]:
    registry = getattr(runtime, "registry", None)
    if registry is None:
        return ()
    return tuple(registry.snapshot().markets)


def _resolve_market(
    runtime: Any,
    *,
    condition_id: str | None = None,
    token_id: str | None = None,
    market_slug: str | None = None,
) -> Market | None:
    registry = getattr(runtime, "registry", None)
    if registry is None:
        return None
    if condition_id:
        market = registry.get_by_condition_id(condition_id)
        if market is not None:
            return market
    if token_id:
        market = registry.get_by_token_id(token_id)
        if market is not None:
            return market
    if market_slug:
        return registry.get_by_slug(market_slug)
    return None


def _orderbook(runtime: Any, token_id: str) -> OrderbookSnapshot | None:
    worker = getattr(runtime, "market_ws_worker", None)
    snapshot = None if worker is None else getattr(worker, "snapshot", None)
    if not callable(snapshot):
        return None
    return snapshot(token_id)


def _resolve_market_by_token(runtime: Any, token_id: str) -> Market | None:
    registry = getattr(runtime, "registry", None)
    if registry is None:
        return None
    return registry.get_by_token_id(token_id)


def _entry_metadata_for_market(runtime: Any, market: Market) -> dict[str, Any]:
    store = getattr(runtime, "entry_metadata_store", None)
    if store is None:
        return {}
    return dict(
        store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )
    )


def _entry_metadata_record_for_market(runtime: Any, market: Market):
    store = getattr(runtime, "entry_metadata_store", None)
    if store is None:
        return None
    return store.find(
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        event_slug=market.event_slug,
    )


def _trading_decision_service(runtime: Any) -> TradingDecisionService:
    service = getattr(runtime, "trading_decision_service", None)
    if service is None:
        raise RuntimeError("trading_decision_service unavailable")
    return service


def _real_sign_client(runtime: Any) -> PolymarketOrderExecutionClient | None:
    trading_client = getattr(runtime, "trading_client", None)
    if trading_client is None:
        return None
    return PolymarketOrderExecutionClient(trading_client)


def _settings_decimal(runtime: Any, name: str) -> Decimal:
    value = _settings_value(runtime, name)
    return value if isinstance(value, Decimal) else Decimal(str(value or "0"))


def _settings_value(runtime: Any, name: str) -> Any:
    settings = getattr(runtime, "settings", None)
    return None if settings is None else getattr(settings, name, None)


def _empty_market() -> Market:
    return Market(condition_id="", market_slug="", outcomes=())
