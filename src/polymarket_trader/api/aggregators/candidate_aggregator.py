"""CandidateAggregator —— 策略候选投影（运营查询类，DataGraph + 3s TTL 缓存）。

按 docs/新架构方案.md §12.2 ① 运营查询类（走 DataGraph / 内存 + 短 TTL 缓存）。

# Endpoint 对应

| Endpoint | 方法 |
|---|---|
| `GET /candidates` | `list_strategy_candidates(...)` |

# 设计

- 候选生成是 CPU 密集（1500 markets × 2 outcome = 3000 次 build_entry_plan），
  3s TTL 缓存避免多客户端并发把 event loop 卡死。
- 入口 build_entry_plan 走 `runtime.decision_context_builder`；helpers 全部
  从 runtime 拿（不依赖 AdminService 实例）。
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone
from typing import Any

from polymarket_trader.serialization import decimal_text, jsonable, page_payload
from polymarket_trader.app.admin_service_helpers import _candidate_matches_filters
from polymarket_trader.app.decision_serialization import serialize_intent
from polymarket_trader.app.market_tracking_policy import market_outside_trade_window
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.observability.cpu_track import cpu_track


# module-level cache (TTL 3s) keyed by (cid, token, slug).
_CANDIDATE_CACHE: dict[
    tuple[str | None, str | None, str | None],
    tuple[float, list[dict[str, Any]], int],
] = {}


def reset_candidate_cache() -> None:
    _CANDIDATE_CACHE.clear()


class CandidateAggregator:
    def __init__(self, *, runtime: Any) -> None:
        self._runtime = runtime

    def _account_snapshot(self) -> AccountSnapshot:
        store = getattr(self._runtime, "account_state_store", None)
        return store.snapshot() if store is not None else AccountSnapshot()

    def _market_ws_snapshot(self, token_id: str):
        worker = getattr(self._runtime, "market_ws_worker", None)
        return worker.snapshot(token_id) if worker is not None else None

    def _entry_metadata_store(self) -> Any | None:
        return getattr(self._runtime, "market_metadata_store", None)

    def _resolve_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Market | None:
        registry = getattr(self._runtime, "registry", None)
        if registry is None:
            return None
        if condition_id is not None:
            m = registry.get_by_condition_id(condition_id)
            if m is not None:
                return m
        if token_id is not None:
            m = registry.get_by_token_id(token_id)
            if m is not None:
                return m
        if market_slug is not None:
            m = registry.get_by_slug(market_slug)
            if m is not None:
                return m
        return None

    def _candidate_source_markets(
        self,
        *,
        condition_id: str | None,
        token_id: str | None,
        market_slug: str | None,
    ) -> tuple[Market, ...]:
        if condition_id is not None or token_id is not None or market_slug is not None:
            market = self._resolve_market(
                condition_id=condition_id, token_id=token_id, market_slug=market_slug
            )
            return () if market is None else (market,)

        store = self._entry_metadata_store()
        registry = getattr(self._runtime, "registry", None)
        if store is None or registry is None:
            return ()
        now = datetime.now(timezone.utc)
        markets: dict[str, Market] = {}
        for record in store.records():
            has_series = bool(
                record.metadata.get("series_state")
                or record.metadata.get("game_odds")
                or record.metadata.get("season_odds_snapshot")
            )
            if not record.live_state_payload and not has_series:
                continue
            market = None
            if record.condition_id:
                market = registry.get_by_condition_id(record.condition_id)
            if market is None and record.market_slug:
                market = registry.get_by_slug(record.market_slug)
            if market is None and record.event_slug:
                market = registry.get_by_slug(record.event_slug)
            if market is None:
                continue
            if market_outside_trade_window(market, now=now):
                continue
            markets[market.condition_id] = market
        return tuple(markets.values())

    def _entry_metadata_for_market(self, market: Market) -> dict[str, Any]:
        store = self._entry_metadata_store()
        if store is None:
            return {}
        return store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )

    def _build_entry_plan(
        self,
        *,
        market: Market,
        token_id: str,
        orderbook,
        account: AccountSnapshot,
    ):
        settings = self._runtime.settings
        strategy_config = self._runtime.workflow.config
        return self._runtime.decision_context_builder.build_entry_plan(
            market=market,
            orderbook=orderbook,
            account_snapshot=None,
            token_id=token_id,
            trace_id=None,
            portfolio_budget_usdc=settings.portfolio_budget_usdc,
            available_usdc=account.available_usdc,
            kelly_fraction=strategy_config.kelly_fraction,
            kelly_max_position_fraction=strategy_config.kelly_max_position_fraction,
            kelly_min_edge=strategy_config.kelly_min_edge,
            kelly_min_stake_usdc=strategy_config.kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=strategy_config.kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=strategy_config.kelly_round_up_max_overbet_ratio,
            positions=account.positions,
            open_orders=account.open_orders,
            metadata=self._entry_metadata_for_market(market),
            manual_confirmation=None,
        )

    def _candidate_payload(self, market: Market, token_id: str, plan) -> dict[str, Any]:
        outcome = market.get_outcome_by_token_id(token_id)
        summary = plan.summary
        extras = dict(summary.extras) if summary is not None else {}
        execution_permission = extras.get("execution_permission") if extras else None
        strategy_action = summary.action if summary is not None else ""
        if strategy_action == "auto_execute" and not plan.ready_to_trade:
            action_label = "reject"
            block_reason = ""
            allocation = plan.allocation
            if allocation is not None:
                block_reason = str(allocation.release_reason or allocation.reason or "")
            reason_text = block_reason or plan.reason or (summary.reason if summary is not None else "")
        else:
            action_label = strategy_action
            reason_text = (summary.reason if summary is not None else "") or plan.reason or ""
        accepted = bool(action_label and action_label != "reject")
        confirmable = (
            execution_permission == "manual_confirm"
            and action_label == "manual_confirm"
            and not (summary.manual_confirmed if summary is not None else False)
        )
        signal_allowed: bool | None = None
        live_state_age_ms: int | None = None
        live_state_source: str | None = None
        try:
            meta_store = self._entry_metadata_store()
            if meta_store is not None:
                meta_rec = next(
                    (r for r in meta_store.records() if r.condition_id == market.condition_id),
                    None,
                )
                if meta_rec is not None:
                    signal_allowed = meta_rec.live_state_signal_allowed
                    live_state_source = meta_rec.source
                    if meta_rec.updated_at is not None:
                        live_state_age_ms = int(
                            (datetime.now(timezone.utc) - meta_rec.updated_at).total_seconds() * 1000
                        )
        except Exception:  # noqa: BLE001
            pass
        return {
            "candidate_id": f"{plan.trace_id}:{market.condition_id}:{token_id}",
            "trace_id": plan.trace_id,
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "token_id": token_id,
            "outcome": None if outcome is None else outcome.outcome,
            "ready_to_trade": plan.ready_to_trade,
            "accepted": accepted,
            "confirmable": confirmable,
            "decision_kind": None if plan.decision_kind is None else plan.decision_kind.value,
            "reason": reason_text,
            "action": action_label,
            "strategy_action": strategy_action,
            "execution_permission": execution_permission,
            "label": summary.label if summary is not None else "",
            "market_type": summary.market_type if summary is not None else "",
            "side": summary.side if summary is not None else "",
            "line": (
                decimal_text(summary.line) if summary is not None and summary.line is not None else None
            ),
            "best_ask": (
                decimal_text(summary.best_ask) if summary is not None and summary.best_ask is not None else None
            ),
            "manual_confirmed": summary.manual_confirmed if summary is not None else False,
            "confirmed_by": summary.confirmed_by if summary is not None else "",
            "confirm_reason": summary.confirm_reason if summary is not None else "",
            "extras": jsonable(extras),
            "allocation": None if plan.allocation is None else {
                "target_budget_usdc": decimal_text(plan.allocation.target_budget_usdc),
                "buy_budget_usdc": decimal_text(plan.allocation.buy_budget_usdc),
                "reason": plan.allocation.reason,
                "release_reason": plan.allocation.release_reason,
            },
            "intent": None if plan.intent is None else serialize_intent(plan.intent),
            "payload": jsonable(plan.metadata or {}),
            "signal_allowed": signal_allowed,
            "live_state_age_ms": live_state_age_ms,
            "live_state_source": live_state_source,
        }

    @cpu_track("candidates_evaluation")
    async def list_strategy_candidates(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
        market_type: str | None = None,
        game_status: str | None = None,
        action: str | None = None,
        execution_permission: str | None = None,
        accepted: bool | None = None,
        confirmable: bool | None = None,
        league: str | None = None,
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        account = self._account_snapshot()
        # 3s TTL cache —— 候选评估是 CPU 密集，多客户端共享一次评估。
        now_mono = _time.monotonic()
        cache_key = (condition_id, token_id, market_slug)
        cached = _CANDIDATE_CACHE.get(cache_key)
        all_candidates: list[dict[str, Any]] | None
        source_count: int
        if cached is not None and (now_mono - cached[0]) < 3.0:
            all_candidates = cached[1]
            source_count = cached[2]
        else:
            all_candidates = None
            source_count = 0

        if all_candidates is None:
            source_markets = self._candidate_source_markets(
                condition_id=condition_id, token_id=token_id, market_slug=market_slug
            )
            source_count = len(source_markets)
            all_candidates = []
            for index, market in enumerate(source_markets, start=1):
                if index % 20 == 0:
                    await asyncio.sleep(0)
                for outcome in market.outcomes:
                    if token_id is not None and outcome.token_id != token_id:
                        continue
                    orderbook = self._market_ws_snapshot(outcome.token_id)
                    if orderbook is None:
                        continue
                    plan = self._build_entry_plan(
                        market=market,
                        token_id=outcome.token_id,
                        orderbook=orderbook,
                        account=account,
                    )
                    summary = plan.summary
                    if summary is None or not summary.reason:
                        continue
                    all_candidates.append(
                        self._candidate_payload(market, outcome.token_id, plan)
                    )
            _CANDIDATE_CACHE[cache_key] = (now_mono, all_candidates, source_count)
            while len(_CANDIDATE_CACHE) > 200:
                oldest = next(iter(_CANDIDATE_CACHE))
                _CANDIDATE_CACHE.pop(oldest, None)

        for candidate in all_candidates:
            if _candidate_matches_filters(
                candidate,
                market_type=market_type,
                game_status=game_status,
                action=action,
                execution_permission=execution_permission,
                accepted=accepted,
                confirmable=confirmable,
                league=league,
            ):
                candidates.append(candidate)

        if limit <= 0:
            limit = 100
        if offset < 0:
            offset = 0
        sliced = tuple(candidates[offset : offset + limit])
        page: RepositoryPage[Any] = RepositoryPage(
            items=sliced, total=len(candidates), limit=limit, offset=offset
        )
        payload = page_payload(page, serializer=lambda item: item)
        payload["has_more"] = offset + len(page.items) < page.total
        payload["source_markets"] = source_count
        return payload
