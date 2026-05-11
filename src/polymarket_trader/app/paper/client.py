"""沙箱用 ``OrderExecutionClient`` 实现：真实签名 + 内存撮合 + 账本累计。

与 ``InMemoryPolymarketOrderClient``（``infra/polymarket/order_executor.py``）同形
Protocol，由 ``PolymarketOrderExecutor`` 注入；§3 要求的 OrderExecutor 唯一下单
出口未变，沙箱仅替换该 client 实现。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import OrderResultStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionRequest,
    OrderExecutionResponse,
)

from .fill_engine import SimulationOutcome, simulate_fill
from .state import PaperVirtualLedger


class PaperSubmitOnlyOrderClient:
    """真实构造/签名订单；最后 submit 由 ``app/paper`` 撮合引擎模拟。

    ``orderbook_lookup`` / ``market_lookup`` 由调用方注入，便于撮合时按 token_id
    取回真实订单簿和 fee 配置；``ledger`` 累计虚拟成交账本，供 PnL 计算读取。
    """

    def __init__(
        self,
        *,
        real_sign_client: Any | None = None,
        market_lookup: Callable[[str], Market | None] | None = None,
        orderbook_lookup: Callable[[str], OrderbookSnapshot | None] | None = None,
        ledger: PaperVirtualLedger | None = None,
    ) -> None:
        self.requests: list[tuple[str, OrderExecutionRequest]] = []
        self.simulations: list[tuple[str, SimulationOutcome]] = []
        self._real_sign_client = real_sign_client
        self._market_lookup = market_lookup or (lambda _token_id: None)
        self._orderbook_lookup = orderbook_lookup or (lambda _token_id: None)
        self._ledger = ledger or PaperVirtualLedger()

    @property
    def ledger(self) -> PaperVirtualLedger:
        return self._ledger

    @property
    def signing_source(self) -> str:
        if self._real_sign_client is None:
            return "paper_fallback"
        return "real_trading_client"

    async def sign_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        """尽量沿用生产签名流程；缺少交易客户端时才退回本地虚拟签名。"""

        self.requests.append(("sign", request))
        sign_order = None if self._real_sign_client is None else getattr(
            self._real_sign_client,
            "sign_order",
            None,
        )
        if callable(sign_order):
            return await _maybe_await(sign_order(request))
        return OrderExecutionResponse(
            status="signed",
            raw_response={
                "signed": True,
                "paper_fallback": True,
                "idempotency_key": request.idempotency_key,
            },
            reason="paper_signed_without_live_trading_client",
        )

    async def submit_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        """走 ``app/paper`` 撮合引擎；不调用 Polymarket post_signed_order。"""

        self.requests.append(("submit", request))
        market = self._market_lookup(request.token_id)
        orderbook = self._orderbook_lookup(request.token_id)
        outcome = simulate_fill(request, market=market, orderbook=orderbook, ledger=self._ledger)
        self.simulations.append((request.trace_id, outcome))
        return outcome.response

    async def cancel_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        self.requests.append(("cancel", request))
        return OrderExecutionResponse(
            status=OrderResultStatus.CANCELLED,
            order_id=request.order_id,
            raw_response={"virtual": True, "action": "cancel"},
            reason="paper_cancelled",
        )

    async def replace_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        self.requests.append(("replace", request))
        return OrderExecutionResponse(
            status=OrderResultStatus.LIVE,
            order_id=f"paper-replace-{request.trace_id}",
            remaining_shares=request.size_shares,
            raw_response={"virtual": True, "action": "replace"},
            reason="paper_replaced",
        )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
