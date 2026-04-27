from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr

from polymarket_trader.api.app import create_app
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.app.reconcile_service import (
    ReconcileAction,
    ReconcileActionType,
    ReconcileMarketPlan,
    ReconcilePlan,
)
from polymarket_trader.config import Settings
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import (
    ExecutionTimestamps,
    OrderRecord,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    ReplaceOrderIntent,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.outbox.local_queue import LocalOutbox
from polymarket_trader.infra.polymarket.schemas import (
    normalize_orderbook_payload,
    normalize_position_payload,
    normalize_price_history_payload,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.status import ReadinessSnapshot, RuntimePhase, RuntimeSnapshot
from polymarket_trader.main import FullMarketDiscoveryState
from tests.helpers.markets import binary_market_outcomes, build_binary_market

EXPECTED_ADMIN_ROUTES = {
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/runtime"),
    ("GET", "/workers"),
    ("GET", "/metrics"),
    ("GET", "/audit-events"),
    ("GET", "/allocations"),
    ("GET", "/markets"),
    ("GET", "/markets/detail"),
    ("GET", "/markets/orderbook"),
    ("GET", "/markets/midpoint"),
    ("GET", "/markets/prices-history"),
    ("GET", "/orders"),
    ("POST", "/orders/replace"),
    ("GET", "/fills"),
    ("GET", "/positions"),
    ("GET", "/portfolio"),
    ("GET", "/outbox/pending"),
    ("POST", "/operations/reconcile"),
}


@dataclass(frozen=True, slots=True)
class FakeConfigReadiness:
    ready_to_trade: bool
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "ready_to_trade": self.ready_to_trade,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


@dataclass(frozen=True, slots=True)
class FakePersistenceSnapshot:
    outbox_depth: int
    last_error: str | None = None
    last_persisted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FakeTradeReview:
    operation: str
    submitted: bool
    risk_decision: object | None
    order_result: OrderResult | None
    submission_error: str | None = None


class FakeMarketWsWorker:
    def __init__(self, snapshots: dict[str, OrderbookSnapshot]) -> None:
        self._snapshots = snapshots

    def snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        return self._snapshots.get(token_id)


class FakeSupervisor:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot


class FakeDataClient:
    def __init__(
        self,
        *,
        positions: tuple[object, ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._positions = positions or (
            normalize_position_payload(
                {
                    "proxyWallet": "0x1111111111111111111111111111111111111111",
                    "asset": "no-token-sample",
                    "conditionId": "0x" + "1" * 64,
                    "size": 5,
                    "avgPrice": 0.5,
                    "initialValue": 2.5,
                    "currentValue": 3,
                    "cashPnl": 0.5,
                    "percentPnl": 20,
                    "totalBought": 5,
                    "realizedPnl": 0.1,
                    "percentRealizedPnl": 2,
                    "curPrice": 0.6,
                    "redeemable": False,
                    "mergeable": False,
                    "title": "Sample Market A",
                    "slug": "sample-market-a",
                    "icon": "https://example.com/icon.png",
                    "eventSlug": "sample-event-a",
                    "outcome": "No",
                    "outcomeIndex": 1,
                    "oppositeOutcome": "Yes",
                    "oppositeAsset": "yes-token-sample",
                    "endDate": "2026-02-01T00:00:00Z",
                    "negativeRisk": False,
                }
            ),
        )
        self._error = error
        self.position_calls: list[dict[str, object]] = []

    async def list_positions(self, **kwargs: object) -> tuple[object, ...]:
        self.position_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._positions


@dataclass(frozen=True, slots=True)
class FakePublicProfile:
    proxy_wallet: str | None = "0x2222222222222222222222222222222222222222"
    profile_image: str | None = "https://example.com/avatar.png"
    display_username_public: bool | None = True
    name: str | None = "FDV Trader"
    pseudonym: str | None = "Poly-2222"
    x_username: str | None = "fdv_trader"
    verified_badge: bool | None = True


class FakeGammaClient:
    def __init__(self) -> None:
        self.profile_calls: list[str] = []

    async def get_public_profile(self, address: str, **kwargs: object) -> FakePublicProfile:
        self.profile_calls.append(address)
        return FakePublicProfile()


class FakeClobClient:
    def __init__(
        self,
        *,
        history: object | None = None,
        orderbooks: dict[str, object] | None = None,
        midpoint: Decimal | None = None,
        error: Exception | None = None,
        wallet_address: str | None = "0x1111111111111111111111111111111111111111",
    ) -> None:
        self._history = history or normalize_price_history_payload(
            {
                "history": [
                    {"t": 1704100800, "p": 0.52},
                    {"t": 1704104400, "p": 0.54},
                ]
            }
        )
        self._orderbooks = orderbooks or {
            "no-token-sample": normalize_orderbook_payload(
                {
                    "market": "condition-sample",
                    "asset_id": "no-token-sample",
                    "timestamp": 1704100800,
                    "bids": [{"price": "0.55", "size": "100"}],
                    "asks": [{"price": "0.59", "size": "200"}],
                    "min_order_size": "1",
                    "tick_size": "0.01",
                    "last_trade_price": "0.54",
                },
                token_id="no-token-sample",
                market_slug="sample-market-a",
                condition_id="condition-sample",
            ),
            "yes-token-sample": normalize_orderbook_payload(
                {
                    "market": "condition-sample",
                    "asset_id": "yes-token-sample",
                    "timestamp": 1704100801,
                    "bids": [{"price": "0.95", "size": "80"}],
                    "asks": [{"price": "0.99", "size": "120"}],
                    "min_order_size": "1",
                    "tick_size": "0.01",
                    "last_trade_price": "0.97",
                },
                token_id="yes-token-sample",
                market_slug="sample-market-a",
                condition_id="condition-sample",
            ),
        }
        self._midpoint = midpoint or Decimal("0.57")
        self._error = error
        self.default_wallet_address = wallet_address
        self.history_calls: list[dict[str, object]] = []
        self.orderbook_calls: list[dict[str, object]] = []
        self.midpoint_calls: list[dict[str, object]] = []

    async def get_orderbook(self, token_id: str, **kwargs: object) -> object:
        self.orderbook_calls.append({"token_id": token_id, **kwargs})
        if self._error is not None:
            raise self._error
        return self._orderbooks[token_id]

    async def get_midpoint(self, token_id: str, **kwargs: object) -> Decimal:
        self.midpoint_calls.append({"token_id": token_id, **kwargs})
        if self._error is not None:
            raise self._error
        return self._midpoint

    async def get_prices_history(self, token_id: str, **kwargs: object) -> object:
        self.history_calls.append({"token_id": token_id, **kwargs})
        if self._error is not None:
            raise self._error
        return self._history


class FakeReconcileWorker:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str | None, tuple[str, ...] | None]] = []

    async def reconcile_once(
        self,
        *,
        trace_id: str | None = None,
        condition_ids: tuple[str, ...] | None = None,
    ) -> object:
        self.calls.append((trace_id, condition_ids))
        return self.result


class FakeTradingService:
    def __init__(self) -> None:
        self.replace_calls: list[ReplaceOrderIntent] = []

    async def replace(self, intent: ReplaceOrderIntent, *, operation: str = "replace") -> FakeTradeReview:
        self.replace_calls.append(intent)
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = OrderResult(
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            status=OrderResultStatus.LIVE,
            intent=intent,
            order_id=f"{intent.trace_id}-replace",
            price=intent.new_price,
            requested_size_shares=intent.size_shares,
            matched_shares=Decimal("0"),
            remaining_shares=intent.size_shares,
            notional_usdc=intent.new_price * intent.size_shares,
            reason="replace_submitted",
            timestamps=ExecutionTimestamps(
                queued_at=now,
                sign_started_at=now,
                signed_at=now,
                submitted_at=now,
                ack_at=now,
            ),
        )
        return FakeTradeReview(
            operation=operation,
            submitted=True,
            risk_decision=None,
            order_result=result,
        )


def _market() -> Market:
    return build_binary_market(
        condition_id="condition-sample",
        market_slug="sample-market-a",
        no_token_id="no-token-sample",
        yes_token_id="yes-token-sample",
        event_id="event-1",
        event_title="Sample Market A",
        event_slug="sample-event-a",
        icon_url="https://example.com/icon.png",
        end_date=datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        fees_enabled=True,
        maker_base_fee_bps=0,
        taker_base_fee_bps=100,
        fee_rate_bps=125,
        fee_rate_updated_at=datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc),
        category="Crypto",
        matched_keywords=("sample", "market", "threshold"),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _no_token_id(market: Market) -> str:
    return market.require_token_id("NO")


def _yes_token_id(market: Market) -> str:
    return market.require_token_id("YES")


def _orderbook(market: Market) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=_no_token_id(market),
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.59"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.59"), size=Decimal("200")),),
        received_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("200"),
        tick_size=market.tick_size,
    )


def _yes_orderbook(market: Market) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=_yes_token_id(market),
        best_bid=Decimal("0.95"),
        best_ask=Decimal("0.99"),
        bids=(PriceLevel(price=Decimal("0.95"), size=Decimal("80")),),
        asks=(PriceLevel(price=Decimal("0.99"), size=Decimal("120")),),
        received_at=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        best_bid_size=Decimal("80"),
        best_ask_size=Decimal("120"),
        tick_size=market.tick_size,
    )


def _token_view(payload: dict[str, object], outcome: str) -> dict[str, object]:
    token_views = payload["token_views"]
    assert isinstance(token_views, list)
    for item in token_views:
        if isinstance(item, dict) and item.get("outcome") == outcome:
            return item
    raise AssertionError(f"missing token view for {outcome}")


def _reconcile_result(market: Market) -> object:
    action = ReconcileAction(
        action_type=ReconcileActionType.CANCEL_ORDER,
        trace_id="trace-reconcile",
        condition_id=market.condition_id,
        token_id=_no_token_id(market),
        market_slug=market.market_slug,
        reason="open_buy_detected",
        source_order_id="buy-1",
        target_size_shares=Decimal("5"),
    )
    plan = ReconcilePlan(
        trace_id="trace-reconcile",
        generated_at=datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc),
        market_plans=(
            ReconcileMarketPlan(
                trace_id="trace-reconcile",
                market=market,
                position=None,
                open_orders=(),
                actions=(action,),
                pause_trading=False,
            ),
        ),
        total_actions=1,
        paused_market_count=0,
    )
    return SimpleNamespace(
        trace_id="trace-reconcile",
        plan=plan,
        applied_actions=(action,),
        failed_actions=(),
    )


def _build_runtime(*, ready: bool = True) -> SimpleNamespace:
    market = _market()
    settings = Settings(
        _env_file=None,
        portfolio_budget_usdc=Decimal("100"),
        max_order_usdc=Decimal("25"),
        max_market_usdc=Decimal("50"),
        max_total_usdc=Decimal("100"),
        max_open_orders=10,
        wallet_private_key=SecretStr("super-secret"),
        polymarket_funder_address="0x2222222222222222222222222222222222222222",
    )
    registry = MarketRegistry()
    registry.upsert(market)
    account_state_store = AccountStateStore()
    account_state_store.update_balances(balance_usdc=Decimal("100"), allowance_usdc=Decimal("90"))
    account_state_store.upsert_position(
        Position(
            condition_id=market.condition_id,
            token_id=_no_token_id(market),
            market_slug=market.market_slug,
            shares=Decimal("5"),
            cost_usdc=Decimal("3"),
        )
    )
    account_state_store.upsert_order(
        OrderRecord(
            trace_id="trace-buy",
            condition_id=market.condition_id,
            token_id=_no_token_id(market),
            market_slug=market.market_slug,
            side=OrderSide.BUY,
            order_type=OrderType.FAK,
            price=Decimal("0.43"),
            amount_usdc=Decimal("10"),
            order_id="buy-1",
            status=OrderStatus.LIVE,
            remaining_shares=Decimal("5"),
            idempotency_key="buy-1",
            reason="open_buy_detected",
        )
    )
    account_state_store.upsert_order(
        OrderRecord(
            trace_id="trace-sell",
            condition_id=market.condition_id,
            token_id=_no_token_id(market),
            market_slug=market.market_slug,
            side=OrderSide.SELL,
            order_type=OrderType.GTC,
            price=Decimal("0.78"),
            size_shares=Decimal("5"),
            order_id="sell-1",
            status=OrderStatus.LIVE,
            remaining_shares=Decimal("5"),
            idempotency_key="sell-1",
            reason="existing_sell",
        )
    )
    account_state_store.record_fill(
        Fill(
            trace_id="trace-fill",
            event_type="trade_confirmed",
            market_slug=market.market_slug,
            condition_id=market.condition_id,
            token_id=_no_token_id(market),
            order_id="sell-1",
            trade_id="trade-1",
            side="SELL",
            price=Decimal("0.78"),
            size=Decimal("5"),
            notional_usdc=Decimal("3.5"),
            confirmed_at=datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc),
        )
    )
    account_state_store.mark_user_ws_connected(ready)
    if ready:
        account_state_store.mark_reconciled(datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc))

    event_bus = EventBus()
    orderbook = _orderbook(market)
    fake_market_ws_worker = FakeMarketWsWorker(
        {
            _no_token_id(market): orderbook,
            _yes_token_id(market): _yes_orderbook(market),
        }
    )
    readiness = FakeConfigReadiness(ready_to_trade=True)
    runtime_readiness = ReadinessSnapshot(
        phase=RuntimePhase.TRADING_ENABLED if ready else RuntimePhase.RECOVERING_SNAPSHOT,
        live=True,
        ready=ready,
        automatic_trading_enabled=ready,
        config_ready=True,
        db_ready=True,
        trading_client_ready=True,
        market_ws_connected=True,
        user_ws_connected=ready,
        reconcile_fresh=ready,
        outbox_backlog_ok=True,
        low_priority_paused=False,
        blocking_reasons=() if ready else ("user_ws_not_connected", "reconcile_pending"),
        warnings=(),
        last_reconcile_at=account_state_store.snapshot().last_reconcile_at,
    )
    runtime_snapshot = RuntimeSnapshot(
        phase=RuntimePhase.TRADING_ENABLED if ready else RuntimePhase.RECOVERING_SNAPSHOT,
        automatic_trading_enabled=ready,
        low_priority_paused=False,
        live=True,
        manual_pause_reason=None,
        degraded_reason=None,
        readiness=runtime_readiness,
        settings_readiness=readiness.as_dict(),
        queue_depths=event_bus.snapshot(),
        scheduler=None,
        worker_health=(),
        account=account_state_store.snapshot(),
        market_ws={"connected": True, "subscribed_count": 1},
        user_ws={"connected": ready, "last_error": None if ready else "disconnected"},
        reconcile={
            "last_completed_at": (
                None if account_state_store.snapshot().last_reconcile_at is None
                else account_state_store.snapshot().last_reconcile_at.isoformat()
            ),
            "last_status": "ok" if ready else "pending",
        },
        persistence=FakePersistenceSnapshot(outbox_depth=0),
        metrics={
            "gauges": {"entry_signal_to_submit_ms": 42},
            "queue_depths": {"trading_queue_depth": 0},
        },
    )
    runtime = SimpleNamespace(
        settings=settings,
        readiness=readiness,
        registry=registry,
        market_discovery_scan=FullMarketDiscoveryState(
            round_id=4,
            round_started_at=datetime(2026, 1, 1, 12, 3, 0, tzinfo=timezone.utc),
            last_round_completed_at=datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc),
            last_completed_round_pages=26,
            last_completed_round_markets=13000,
            pages_scanned_in_round=2,
            markets_seen_in_round=1000,
            last_page_size=500,
            last_tick_started_at=datetime(2026, 1, 1, 12, 3, 10, tzinfo=timezone.utc),
            last_tick_completed_at=datetime(2026, 1, 1, 12, 3, 10, tzinfo=timezone.utc),
            last_tick_requests=2,
            last_tick_markets=1000,
        ),
        gamma_client=FakeGammaClient(),
        clob_client=FakeClobClient(),
        data_client=FakeDataClient(),
        account_state_store=account_state_store,
        market_ws_worker=fake_market_ws_worker,
        event_bus=event_bus,
        persistence_worker=SimpleNamespace(snapshot=lambda: FakePersistenceSnapshot(outbox_depth=0)),
        supervisor=FakeSupervisor(runtime_snapshot),
        trading_service=FakeTradingService(),
        reconcile_worker=FakeReconcileWorker(_reconcile_result(market)),
        bootstrap_summary={"loaded_reference": {"markets": 1, "positions": 1}},
        db_session_factory=None,
    )
    return runtime


def run_admin_api_exposes_hot_state_and_readiness_routes() -> None:
    runtime = _build_runtime(ready=True)
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        runtime_payload = client.get("/runtime").json()
        workers = client.get("/workers").json()
        metrics = client.get("/metrics").json()
        markets = client.get("/markets").json()
        market_detail = client.get("/markets/detail", params={"market_slug": "sample-market-a"}).json()
        market_orderbook = client.get(
            "/markets/orderbook",
            params={"market_slug": "sample-market-a", "token_id": "no-token-sample"},
        ).json()
        market_midpoint = client.get(
            "/markets/midpoint",
            params={"market_slug": "sample-market-a", "token_id": "no-token-sample"},
        ).json()
        market_prices_history = client.get(
            "/markets/prices-history",
            params={
                "token_id": "no-token-sample",
                "start_ts": 1704100800,
                "end_ts": 1704104400,
                "interval": "1h",
                "fidelity": 60,
            },
        ).json()
        orders = client.get("/orders").json()
        hot_positions = client.get("/positions").json()
        fills = client.get("/fills").json()
        portfolio = client.get("/portfolio").json()

        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        assert ready.status_code == 200
        assert ready.json()["ready_to_trade"] is True
        assert ready.json()["phase"] == "trading_enabled"

        assert runtime_payload["registry"]["market_count"] == 1
        assert runtime_payload["settings"]["wallet_private_key"] == "***"
        assert runtime_payload["identity"]["wallet_address"] == "0x1111111111111111111111111111111111111111"
        assert runtime_payload["identity"]["funder_address"] == "0x2222222222222222222222222222222222222222"
        assert runtime_payload["identity"]["profile_address"] == "0x2222222222222222222222222222222222222222"
        assert runtime_payload["identity"]["profile_name"] == "FDV Trader"
        assert runtime_payload["identity"]["profile_pseudonym"] == "Poly-2222"
        assert runtime_payload["identity"]["profile_image"] == "https://example.com/avatar.png"
        assert runtime_payload["identity"]["profile_verified"] is True
        assert runtime_payload["identity"]["profile_x_username"] == "fdv_trader"
        assert runtime_payload["market_discovery"]["round_id"] == 4
        assert runtime_payload["market_discovery"]["last_completed_round_markets"] == 13000
        assert runtime_payload["market_discovery"]["pages_scanned_in_round"] == 2
        assert runtime_payload["market_discovery"]["cursor_active"] is False
        assert runtime_payload["readiness"]["ready"] is True
        runtime_market = runtime_payload["markets"][0]
        runtime_no_view = _token_view(runtime_market, "NO")
        assert runtime_payload["markets"][0]["market"]["market_slug"] == "sample-market-a"
        assert runtime_payload["markets"][0]["market"]["icon_url"] == "https://example.com/icon.png"
        assert runtime_payload["markets"][0]["market"]["end_date"] == "2026-02-01T00:00:00+00:00"
        assert runtime_payload["markets"][0]["market"]["fees"]["taker_base_fee_bps"] == 100
        assert runtime_payload["markets"][0]["market"]["fees"]["fee_rate_bps"] == 125
        assert runtime_no_view["orderbook"]["best_ask"] == "0.59"
        assert runtime_no_view["fee_preview"]["fee_rate_bps"] == 125
        assert runtime_no_view["fee_preview"]["buy"]["fee_usdc"] == "3.02375"
        assert runtime_no_view["fee_preview"]["buy"]["price_source"] == "best_ask"
        assert runtime_no_view["fee_preview"]["sell"]["fee_usdc"] == "3.09375"
        assert runtime_no_view["fee_preview"]["sell"]["price_source"] == "best_bid"
        assert workers["phase"] == "trading_enabled"
        assert "status_reason" not in workers
        assert "status_reason" not in metrics
        assert isinstance(workers["workers"], list)
        assert metrics["metrics"]["gauges"]["entry_signal_to_submit_ms"] == 42

        assert markets["total"] == 1
        market_item = markets["items"][0]
        no_view = _token_view(market_item, "NO")
        yes_view = _token_view(market_item, "YES")
        assert market_item["market"]["condition_id"] == "condition-sample"
        assert market_item["market"]["icon_url"] == "https://example.com/icon.png"
        assert market_item["market"]["end_date"] == "2026-02-01T00:00:00+00:00"
        assert market_item["market"]["fees"]["enabled"] is True
        assert market_item["market"]["fees"]["maker_base_fee_bps"] == 0
        assert market_item["market"]["fees"]["fee_rate_updated_at"] == "2026-01-01T12:02:00+00:00"
        assert no_view["token_id"] == "no-token-sample"
        assert no_view["outcome"] == "NO"
        assert no_view["fee_preview"]["basis_size_shares"] == "100"
        assert no_view["fee_preview"]["buy"]["fee_usdc"] == "3.02375"
        assert no_view["fee_preview"]["sell"]["fee_usdc"] == "3.09375"
        assert yes_view["token_id"] == "yes-token-sample"
        assert yes_view["outcome"] == "YES"
        assert yes_view["orderbook"]["token_id"] == "yes-token-sample"
        assert yes_view["best_ask"] == "0.99"
        assert yes_view["best_bid"] == "0.95"
        assert yes_view["fee_preview"]["basis_size_shares"] == "100"
        assert yes_view["fee_preview"]["buy"]["fee_usdc"] == "0.12375"
        assert yes_view["fee_preview"]["sell"]["fee_usdc"] == "0.59375"
        assert market_detail["market"]["condition_id"] == "condition-sample"
        assert market_detail["market"]["market_slug"] == "sample-market-a"
        assert market_detail["market"]["icon_url"] == "https://example.com/icon.png"
        assert market_detail["market"]["end_date"] == "2026-02-01T00:00:00+00:00"
        assert _token_view(market_detail, "NO")["fee_preview"]["buy"]["fee_shares"] == "5.12500"
        assert _token_view(market_detail, "NO")["token_id"] == "no-token-sample"
        assert _token_view(market_detail, "YES")["token_id"] == "yes-token-sample"
        assert _token_view(market_detail, "YES")["fee_preview"]["buy"]["fee_shares"] == "0.12500"
        assert market_orderbook["token_id"] == "no-token-sample"
        assert market_orderbook["source"] == "hot"
        assert market_orderbook["orderbook"]["best_bid"] == "0.55"
        assert market_orderbook["orderbook"]["best_ask"] == "0.59"
        assert market_midpoint["token_id"] == "no-token-sample"
        assert market_midpoint["source"] == "hot"
        assert market_midpoint["midpoint"] == "0.57"
        assert market_midpoint["spread"] == "0.04"
        assert market_prices_history["token_id"] == "no-token-sample"
        assert market_prices_history["interval"] == "1h"
        assert market_prices_history["fidelity"] == 60
        assert market_prices_history["history"][0]["timestamp"] == "2024-01-01T09:20:00+00:00"
        assert market_prices_history["history"][1]["price"] == "0.54"
        assert runtime.clob_client.orderbook_calls == []
        assert runtime.clob_client.history_calls[0]["token_id"] == "no-token-sample"
        assert runtime.clob_client.history_calls[0]["start_ts"] == 1704100800.0
        assert runtime.clob_client.history_calls[0]["end_ts"] == 1704104400.0

        assert orders["total"] == 2
        assert orders["items"][0]["order_id"] == "buy-1"
        assert orders["items"][0]["event_slug"] == "sample-event-a"
        assert orders["items"][1]["side"] == "SELL"

        assert hot_positions["total"] == 1
        assert hot_positions["items"][0]["shares"] == "5"
        assert hot_positions["items"][0]["event_slug"] == "sample-event-a"

        assert fills["total"] == 1
        assert fills["items"][0]["trade_id"] == "trade-1"
        assert fills["items"][0]["event_slug"] == "sample-event-a"

        assert portfolio["allow_new_entries"] is True
        assert portfolio["markets_tracked"] == 1
        assert portfolio["position_count"] == 1
        assert portfolio["balance_usdc"] == "100"
        assert portfolio["allowance_usdc"] == "90"
        assert portfolio["available_usdc"] == "90"


def run_create_app_registers_expected_routes() -> None:
    app = create_app(runtime=_build_runtime(ready=True), admin_service=AdminService())

    actual_routes = {
        (method, route.path)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"GET", "POST"}
    }

    assert actual_routes == EXPECTED_ADMIN_ROUTES


def run_admin_api_exposes_openapi_and_docs_routes() -> None:
    app = create_app(runtime=_build_runtime(ready=True), admin_service=AdminService())

    with TestClient(app) as client:
        openapi_response = client.get("/openapi.json")
        docs_response = client.get("/docs")
        oauth_redirect_response = client.get("/docs/oauth2-redirect")
        redoc_response = client.get("/redoc")

        assert openapi_response.status_code == 200
        schema = openapi_response.json()
        assert schema["info"]["title"] == "Polymarket Trader Admin API"
        documented_paths = {
            path
            for _, path in EXPECTED_ADMIN_ROUTES
            if path not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        }
        assert set(schema["paths"]) == documented_paths
        assert schema["paths"]["/orders/replace"]["post"]
        assert schema["paths"]["/operations/reconcile"]["post"]

        assert docs_response.status_code == 200
        assert "Swagger UI" in docs_response.text

        assert oauth_redirect_response.status_code == 200
        assert "oauth2" in oauth_redirect_response.text.lower()

        assert redoc_response.status_code == 200
        assert "ReDoc" in redoc_response.text


def run_admin_api_supports_reconcile_and_replace_routes() -> None:
    runtime = _build_runtime(ready=True)
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        reconcile_response = client.post(
            "/operations/reconcile",
            json={
                "trace_id": "trace-manual-reconcile",
                "condition_ids": ["condition-sample"],
            },
        )
        replace_response = client.post(
            "/orders/replace",
            json={
                "order_id": "sell-1",
                "new_price": "0.78",
                "operator": "manual",
                "reason": "admin_reprice",
                "trace_id": "trace-reprice",
            },
        )

        assert reconcile_response.status_code == 200
        reconcile_payload = reconcile_response.json()
        assert reconcile_payload["status"] == "ok"
        assert reconcile_payload["plan"]["total_actions"] == 1
        assert reconcile_payload["plan"]["market_plans"][0]["actions"][0]["action_type"] == "cancel_order"
        assert runtime.reconcile_worker.calls == [("trace-manual-reconcile", ("condition-sample",))]

        assert replace_response.status_code == 200
        replace_payload = replace_response.json()
        assert replace_payload["status"] == "ok"
        assert replace_payload["replace_order_submitted"]["status"] == "live"
        assert replace_payload["replace_order_submitted"]["price"] == "0.78"
        assert replace_payload["order"]["order_id"] == "sell-1"
        assert runtime.trading_service.replace_calls

        updated_portfolio = client.get("/portfolio").json()
        assert updated_portfolio["open_order_count"] == 2


def run_admin_api_supports_fee_filters_and_sorting() -> None:
    runtime = _build_runtime(ready=True)
    runtime.db_session_factory = object()
    second_market = replace(
        _market(),
        condition_id="condition-1b",
        market_slug="sample-market-b",
        outcomes=binary_market_outcomes(
            no_token_id="no-token-1b",
            yes_token_id="yes-token-1b",
        ),
        fee_rate_bps=200,
        taker_base_fee_bps=150,
        maker_base_fee_bps=5,
        fee_rate_updated_at=datetime(2026, 1, 1, 12, 3, 0, tzinfo=timezone.utc),
    )
    runtime.registry.upsert(second_market)
    runtime.market_ws_worker._snapshots[_no_token_id(second_market)] = _orderbook(second_market)
    runtime.market_ws_worker._snapshots[_yes_token_id(second_market)] = _yes_orderbook(second_market)

    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        filtered = client.get(
            "/markets",
            params={
                "fees_enabled": "true",
                "fee_rate_bps_min": 150,
                "taker_base_fee_bps_min": 120,
                "sort_by": "fee_rate_bps",
                "sort_direction": "desc",
            },
        ).json()
        sorted_markets = client.get(
            "/markets",
            params={
                "sort_by": "fee_rate_bps",
                "sort_direction": "desc",
            },
        ).json()

        assert filtered["total"] == 1
        assert filtered["items"][0]["market"]["condition_id"] == "condition-1b"
        assert filtered["items"][0]["market"]["fees"]["fee_rate_bps"] == 200

        assert sorted_markets["total"] == 2
        assert [item["market"]["condition_id"] for item in sorted_markets["items"]] == [
            "condition-1b",
            "condition-sample",
        ]


def run_markets_orderbook_falls_back_to_clob_when_hot_snapshot_missing() -> None:
    runtime = _build_runtime(ready=True)
    runtime.market_ws_worker._snapshots.clear()
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        payload = client.get(
            "/markets/orderbook",
            params={"token_id": "no-token-sample"},
        ).json()

        assert payload["token_id"] == "no-token-sample"
        assert payload["source"] == "rest"
        assert payload["orderbook"]["last_trade_price"] == "0.54"
        assert runtime.clob_client.orderbook_calls[0]["token_id"] == "no-token-sample"


def run_markets_midpoint_falls_back_to_clob_when_hot_snapshot_missing() -> None:
    runtime = _build_runtime(ready=True)
    runtime.market_ws_worker._snapshots.clear()
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        payload = client.get(
            "/markets/midpoint",
            params={"token_id": "no-token-sample"},
        ).json()

        assert payload["token_id"] == "no-token-sample"
        assert payload["source"] == "rest"
        assert payload["midpoint"] == "0.57"
        assert runtime.clob_client.midpoint_calls[0]["token_id"] == "no-token-sample"


def run_admin_ready_route_reports_blockers_when_runtime_is_not_ready() -> None:
    runtime = _build_runtime(ready=False)
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        ready = client.get("/ready")
        runtime_payload = client.get("/runtime").json()

        assert ready.status_code == 200
        assert ready.json()["ready_to_trade"] is False
        assert ready.json()["blocking_issues"]
        assert "user_ws_not_connected" in ready.json()["runtime"]["blocking_reasons"]
        assert runtime_payload["runtime"]["ready_to_trade"] is False
        assert runtime_payload["readiness"]["ready"] is False


def run_admin_ready_route_exposes_config_blockers_without_runtime_translation() -> None:
    runtime = _build_runtime(ready=False)
    runtime.readiness = FakeConfigReadiness(
        ready_to_trade=False,
        blocking_issues=(
            {
                "field": "wallet_private_key",
                "code": "missing_secret",
                "message": "密钥未配置，启动阶段禁止自动下单",
            },
        ),
    )
    runtime.supervisor._snapshot = replace(
        runtime.supervisor._snapshot,
        readiness=replace(
            runtime.supervisor._snapshot.readiness,
            blocking_reasons=(
                "config_not_ready",
                "trading_client_not_ready",
                "user_ws_not_connected",
                "reconcile_not_fresh",
                "trading_client_unavailable",
            ),
        ),
    )
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        payload = client.get("/ready").json()

    fields = [issue["field"] for issue in payload["blocking_issues"]]
    messages = [issue["message"] for issue in payload["blocking_issues"]]

    assert fields == ["wallet_private_key"]
    assert "密钥未配置，启动阶段禁止自动下单" in messages
    assert all(issue["field"] != "runtime" for issue in payload["blocking_issues"])
    assert all("config_not_ready" not in message for message in messages)
    assert all("trading_client_not_ready" not in message for message in messages)
    assert all("trading_client_unavailable" not in message for message in messages)


def run_admin_ready_route_exposes_runtime_blocking_reasons_after_config_is_ready() -> None:
    runtime = _build_runtime(ready=False)
    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        payload = client.get("/ready").json()

    assert payload["blocking_reasons"] == ["user_ws_not_connected", "reconcile_pending"]
    assert [issue["code"] for issue in payload["blocking_issues"]] == [
        "user_ws_not_connected",
        "reconcile_pending",
    ]


def run_admin_api_exposes_audit_allocations_outbox_and_order_id_filter(monkeypatch) -> None:
    runtime = _build_runtime(ready=True)
    runtime.outbox = LocalOutbox(max_size=8)
    runtime.outbox.put_nowait(
        OutboxEvent(
            trace_id="trace-outbox",
            event_type="order_submitted",
            idempotency_key="outbox-1",
            event_id="outbox-event-1",
            market_slug="sample-market-a",
            event_slug="sample-event-a",
            condition_id="condition-sample",
            token_id="no-token-sample",
            reason="submit",
            priority="P1",
            payload={"order_id": "buy-1"},
        )
    )
    runtime.db_session_factory = object()

    audit_events = (
        AuditEvent(
            event_title="order_cancelled",
            trace_id="trace-audit",
            event_id="audit-1",
            market_slug="sample-market-a",
            event_slug="sample-event-a",
            condition_id="condition-sample",
            token_id="no-token-sample",
            order_id="buy-1",
            status="cancelled",
            reason="manual_cancel",
        ),
        AuditEvent(
            event_title="market_discovered",
            trace_id="trace-audit-no-slug",
            event_id="audit-no-slug",
            market_slug="sample-market-a",
            condition_id="condition-sample",
            reason="missing_event_slug",
        ),
    )
    allocations = (
        Allocation(
            condition_id="condition-sample",
            market_slug="sample-market-a",
            token_id="no-token-sample",
            target_budget_usdc=Decimal("50"),
            buy_budget_usdc=Decimal("25"),
            current_exposure_usdc=Decimal("10"),
            released_budget_usdc=Decimal("5"),
            reason="equal_weight",
            release_reason="no_fill",
            idempotency_key="alloc-1",
        ),
    )
    async def _list_audit_events_snapshot(
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_title: str | None = None,
    ) -> RepositoryPage[AuditEvent]:
        items = [
            event
            for event in audit_events
            if (trace_id is None or event.trace_id == trace_id)
            and (event_title is None or event.event_title == event_title)
        ]
        return RepositoryPage(items=tuple(items[offset : offset + limit]), total=len(items), limit=limit, offset=offset)

    async def _list_allocations_snapshot(
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> RepositoryPage[Allocation]:
        del trace_id
        items = [
            allocation
            for allocation in allocations
            if (condition_id is None or allocation.condition_id == condition_id)
            and (token_id is None or allocation.token_id == token_id)
            and (market_slug is None or allocation.market_slug == market_slug)
        ]
        return RepositoryPage(items=tuple(items[offset : offset + limit]), total=len(items), limit=limit, offset=offset)

    async def _fake_with_repositories(self, callback):
        repositories = SimpleNamespace(
            audit=SimpleNamespace(list_audit_events_snapshot=_list_audit_events_snapshot),
            allocation=SimpleNamespace(list_allocations_snapshot=_list_allocations_snapshot),
            outbox=None,
            market=SimpleNamespace(
                get_by_condition_id=lambda condition_id: None,
                get_by_market_slug=lambda market_slug: None,
                get_by_token_id=lambda token_id: None,
            ),
            order=None,
            fill=None,
            position=None,
        )
        return await callback(repositories)

    monkeypatch.setattr(AdminService, "_with_repositories", _fake_with_repositories)

    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        audit_payload = client.get(
            "/audit-events",
            params={"trace_id": "trace-audit", "event_title": "order_cancelled"},
        ).json()
        audit_without_slug_payload = client.get(
            "/audit-events",
            params={"trace_id": "trace-audit-no-slug"},
        ).json()
        allocations_payload = client.get(
            "/allocations",
            params={"condition_id": "condition-sample"},
        ).json()
        outbox_payload = client.get(
            "/outbox/pending",
            params={"trace_id": "trace-outbox"},
        ).json()
        filtered_orders = client.get(
            "/orders",
            params={"order_id": "buy-1"},
        ).json()

        assert audit_payload["total"] == 1
        assert audit_payload["items"][0]["event_id"] == "audit-1"
        assert audit_payload["items"][0]["event_title"] == "order_cancelled"
        assert audit_payload["items"][0]["event_slug"] == "sample-event-a"
        assert audit_without_slug_payload["items"][0]["event_slug"] is None

        assert allocations_payload["total"] == 1
        assert allocations_payload["items"][0]["idempotency_key"] == "alloc-1"
        assert allocations_payload["items"][0]["target_budget_usdc"] == "50"
        assert allocations_payload["items"][0]["event_slug"] == "sample-event-a"

        assert outbox_payload["total"] == 1
        assert outbox_payload["items"][0]["idempotency_key"] == "outbox-1"
        assert outbox_payload["items"][0]["priority"] == 1
        assert outbox_payload["items"][0]["event_slug"] == "sample-event-a"

        assert filtered_orders["total"] == 1
        assert filtered_orders["items"][0]["order_id"] == "buy-1"


def run_admin_api_outbox_pending_prefers_live_runtime_queue() -> None:
    runtime = _build_runtime(ready=True)
    runtime.outbox = LocalOutbox(max_size=8)
    runtime.outbox.put_nowait(
        OutboxEvent(
            trace_id="trace-live",
            event_type="market_discovered",
            idempotency_key="live-outbox-1",
            event_id="live-outbox-event-1",
            market_slug="sample-market-a",
            condition_id="condition-sample",
            token_id="no-token-sample",
            reason="live",
            priority="P2",
            payload={"source": "runtime"},
        )
    )
    runtime.db_session_factory = object()

    app = create_app(runtime=runtime, admin_service=AdminService())

    with TestClient(app) as client:
        payload = client.get("/outbox/pending", params={"trace_id": "trace-live"}).json()

    assert payload["total"] == 1
    assert payload["items"][0]["event_id"] == "live-outbox-event-1"
    assert payload["items"][0]["idempotency_key"] == "live-outbox-1"
    assert payload["items"][0]["event_slug"] is None
