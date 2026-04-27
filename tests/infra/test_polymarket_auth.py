from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.order import OrderResultStatus, OrderSide, OrderType
from polymarket_trader.infra.polymarket import auth
from polymarket_trader.infra.polymarket.auth import PolymarketOrderExecutionClient
from polymarket_trader.infra.polymarket.execution_response_mapper import map_submit_response
from polymarket_trader.infra.polymarket.order_execution_types import OrderExecutionRequest
from polymarket_trader.infra.polymarket.order_signing import build_signed_order


class _FakeOrderType:
    FAK = "FAK"
    GTC = "GTC"


class _FakeMarketOrderArgs:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeOrderArgs:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeOfficialClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_market_order(self, order_args):
        self.calls.append(("create_market_order", order_args))
        return {"signed": "market"}

    def create_order(self, order_args):
        self.calls.append(("create_order", order_args))
        return {"signed": "limit"}


def _patch_official(monkeypatch, official_client: _FakeOfficialClient) -> None:
    monkeypatch.setattr(
        auth,
        "_require_py_clob_client",
        lambda: {
            "order_type": _FakeOrderType,
            "market_order_args": _FakeMarketOrderArgs,
            "order_args": _FakeOrderArgs,
        },
    )
    monkeypatch.setattr(
        auth.PolymarketTradingClient,
        "_ensure_client",
        lambda self, *, require_l2: official_client,
    )


def _request(
    *,
    side: OrderSide,
    order_type: OrderType,
    price: Decimal = Decimal("0.60"),
    amount_usdc: Decimal | None = Decimal("6"),
    size_shares: Decimal | None = None,
) -> OrderExecutionRequest:
    return OrderExecutionRequest(
        action="submit",
        trace_id="trace",
        idempotency_key=f"{side.value}:{order_type.value}",
        condition_id="condition",
        token_id="token",
        side=side,
        order_type=order_type,
        price=price,
        amount_usdc=amount_usdc,
        size_shares=size_shares,
    )


def test_buy_gtc_signing_uses_limit_order_size_not_market_amount(monkeypatch) -> None:
    official_client = _FakeOfficialClient()
    _patch_official(monkeypatch, official_client)
    trading_client = auth.PolymarketTradingClient(
        host="https://clob.polymarket.com",
        credentials=auth.PolymarketCredentials(
            api_key=None,
            api_secret=None,
            api_passphrase=None,
            wallet_private_key="0x1",
        ),
    )

    signed = trading_client.create_signed_order(
        _request(side=OrderSide.BUY, order_type=OrderType.GTC, amount_usdc=Decimal("6"))
    )

    assert signed == {"signed": "limit"}
    [(method, args)] = official_client.calls
    assert method == "create_order"
    assert args.kwargs["side"] == "BUY"
    assert args.kwargs["price"] == 0.6
    assert args.kwargs["size"] == 10.0


def test_fak_orders_sign_with_market_order_args_for_buy_and_sell(monkeypatch) -> None:
    official_client = _FakeOfficialClient()
    _patch_official(monkeypatch, official_client)
    trading_client = auth.PolymarketTradingClient(
        host="https://clob.polymarket.com",
        credentials=auth.PolymarketCredentials(
            api_key=None,
            api_secret=None,
            api_passphrase=None,
            wallet_private_key="0x1",
        ),
    )

    trading_client.create_signed_order(
        _request(side=OrderSide.BUY, order_type=OrderType.FAK, amount_usdc=Decimal("6"))
    )
    trading_client.create_signed_order(
        _request(
            side=OrderSide.SELL,
            order_type=OrderType.FAK,
            amount_usdc=None,
            size_shares=Decimal("10"),
        )
    )

    buy_call, sell_call = official_client.calls
    assert buy_call[0] == "create_market_order"
    assert buy_call[1].kwargs["amount"] == 6.0
    assert buy_call[1].kwargs["order_type"] == "FAK"
    assert sell_call[0] == "create_market_order"
    assert sell_call[1].kwargs["amount"] == 10.0
    assert sell_call[1].kwargs["order_type"] == "FAK"


class _FakeTradingClient:
    def __init__(self, response):
        self.response = response

    def create_signed_order(self, request):
        return {"signed": request.idempotency_key}

    def post_signed_order(self, signed_order, *, order_type: str, post_only: bool = False):
        return dict(self.response)


def test_execution_client_maps_matched_fak_buy_amounts_to_partial_fill() -> None:
    client = PolymarketOrderExecutionClient(
        _FakeTradingClient(
            {
                "success": True,
                "orderID": "order-1",
                "status": "matched",
                "makingAmount": "2400000",
                "takingAmount": "4000000",
                "tradeIDs": ["trade-1"],
            }
        )
    )

    response = client.submit_order(
        _request(side=OrderSide.BUY, order_type=OrderType.FAK, amount_usdc=Decimal("6"))
    )

    assert response.status == OrderResultStatus.PARTIAL_FILL
    assert response.order_id == "order-1"
    assert response.trade_id == "trade-1"
    assert response.spent_usdc == Decimal("2.4")
    assert response.matched_shares == Decimal("4")
    assert response.notional_usdc == Decimal("2.4")


def test_execution_client_maps_fak_no_match_error_to_no_fill() -> None:
    client = PolymarketOrderExecutionClient(
        _FakeTradingClient(
            {
                "success": False,
                "orderID": "",
                "status": "rejected",
                "errorMsg": "no orders found to match with FAK order",
            }
        )
    )

    response = client.submit_order(
        _request(side=OrderSide.BUY, order_type=OrderType.FAK, amount_usdc=Decimal("6"))
    )

    assert response.status == OrderResultStatus.NO_FILL
    assert response.reason == "no orders found to match with FAK order"


def test_execution_client_maps_non_fill_error_to_rejected() -> None:
    client = PolymarketOrderExecutionClient(
        _FakeTradingClient(
            {
                "success": False,
                "orderID": "",
                "status": "delayed",
                "errorMsg": "not enough balance / allowance",
            }
        )
    )

    response = client.submit_order(
        _request(side=OrderSide.BUY, order_type=OrderType.FAK, amount_usdc=Decimal("6"))
    )

    assert response.status == OrderResultStatus.REJECTED
    assert response.reason == "not enough balance / allowance"


def test_auth_delegates_signing_and_response_mapping_to_focused_modules() -> None:
    assert callable(build_signed_order)
    assert callable(map_submit_response)
    assert not hasattr(auth, "_response_amounts")
    assert not hasattr(auth, "_execution_status")
