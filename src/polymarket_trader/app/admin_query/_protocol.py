"""共享类型契约：admin_query 子 mixin 期望宿主（AdminService）提供的 helper 集合。

只在 ``TYPE_CHECKING`` 路径下使用；运行时仍依赖 AdminService 的多重继承装配实际方法，
不引入额外的间接调用层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
    from polymarket_trader.app.admin_serialization import AdminSerializer
    from polymarket_trader.app.admin_service_helpers import _RepositoryGroup
    from polymarket_trader.app.entry_plan import EntryPlan
    from polymarket_trader.domain.account import AccountSnapshot
    from polymarket_trader.domain.market import Market
    from polymarket_trader.domain.orderbook import OrderbookSnapshot
    from polymarket_trader.infra.db import RepositoryPage
    from polymarket_trader.main import RuntimeComponents
    from polymarket_trader.runtime.registry import MarketRegistrySnapshot


class AdminQueryHost(Protocol):
    """AdminService 的子 mixin 视角接口。

    所有方法/属性在 AdminService 上已经实现；这里仅为子 mixin 提供类型签名，
    不引入运行时校验。
    """

    runtime: RuntimeComponents | None

    def _serializer(self) -> "AdminSerializer": ...

    def _runtime_view(self) -> "AdminRuntimeView": ...


    def _account_snapshot(self) -> "AccountSnapshot": ...

    def _registry_snapshot(self) -> "MarketRegistrySnapshot": ...

    def _has_db_session_factory(self) -> bool: ...

    def _market_ws_snapshot(self, token_id: str) -> "OrderbookSnapshot | None": ...

    def _clob_client(self) -> Any: ...

    def _entry_metadata_store(self) -> Any | None: ...

    def _entry_metadata_for_market(self, market: "Market") -> dict[str, Any]: ...

    def _slice_sequence(
        self,
        items: "tuple[Any, ...] | list[Any]",
        *,
        limit: int,
        offset: int,
    ) -> "RepositoryPage[Any]": ...

    async def _with_repositories(
        self,
        callback: "Callable[[_RepositoryGroup], Any]",
    ) -> Any: ...

    def _resolve_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> "Market | None": ...

    def _build_entry_plan_for_admin(
        self,
        *,
        market: "Market",
        token_id: str,
        orderbook: "OrderbookSnapshot",
        account: "AccountSnapshot",
    ) -> "EntryPlan": ...

    def _candidate_payload(
        self,
        market: "Market",
        token_id: str,
        plan: Any,
    ) -> dict[str, Any]: ...

    def _candidate_source_markets(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> "tuple[Market, ...]": ...
