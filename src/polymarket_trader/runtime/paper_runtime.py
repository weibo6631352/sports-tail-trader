"""Paper trading 模式下的 execution_client + 虚拟账本 + 后台 task 装配。

把 main.py 中 ``settings.paper_trading_mode`` 分支整段抽出，避免 build_runtime
内 50 行 closure 分散注意力。

返回的 4 元组：
- ``execution_client``：``PaperSubmitOnlyOrderClient``，下游 ``OrderExecutor`` 接管
- ``paper_ledger``：虚拟账本，operator /runtime/paper-ledger 暴露其字段
- ``goalserve_lazy_client``：按需 Goalserve schedule/h2h 查询客户端（API key 存在才创建）
- ``background_tasks``：4 个 paper 模式后台 task，由 build_runtime 透传给
  ``RuntimeComponents.background_tasks``，shutdown 时统一 cancel + gather
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from polymarket_trader.app.paper import PaperSubmitOnlyOrderClient, PaperVirtualLedger
from polymarket_trader.config import Settings
from polymarket_trader.infra.sports.goalserve_lazy_client import GoalserveLazyClient
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.paper_background_tasks import start_paper_background_tasks
from polymarket_trader.runtime.registry import MarketRegistry

if TYPE_CHECKING:
    from polymarket_trader.pipeline.ingest.orderbook_ws import MarketWsWorker


logger = logging.getLogger(__name__)


def build_paper_runtime(
    *,
    settings: Settings,
    account_state_store: AccountStateStore,
    registry: MarketRegistry,
    market_ws_worker: "MarketWsWorker",
) -> tuple[
    PaperSubmitOnlyOrderClient,
    PaperVirtualLedger,
    GoalserveLazyClient | None,
    dict[str, asyncio.Task],
]:
    """构造 paper 模式的执行链 + 4 个后台 task。

    必须在 ``market_ws_worker`` 已经构造后调用——``PaperSubmitOnlyOrderClient``
    直接复用 ``market_ws_worker.snapshot`` 作为 orderbook lookup。
    """

    paper_ledger = PaperVirtualLedger()
    paper_ledger.fund(settings.portfolio_budget_usdc)
    # paper 模式禁用真签名：py-clob-client.sign_order 会调链上 balance check，
    # 链上实际余额很少（多被 active orders 锁住）→ 立即报 "not enough balance"
    # 阻塞所有下单。paper 模式不上链，签名走本地虚拟即可，不损失决策验证价值。
    execution_client = PaperSubmitOnlyOrderClient(
        real_sign_client=None,
        market_lookup=registry.get_by_token_id,
        orderbook_lookup=market_ws_worker.snapshot,
        ledger=paper_ledger,
    )
    # account_state_store 必须用 paper_ledger 的虚拟余额，否则 RiskManager 看
    # 链上真实余额（$0.x 锁在 active orders 后）→ "available_usdc_below..." 警告 +
    # Kelly 算 stake=0 全部拒绝。初始一次性设值，paper_balance_syncer 持续覆盖
    # reconcile worker 周期写回的真链上余额。
    account_state_store.update_balances(
        balance_usdc=settings.portfolio_budget_usdc,
        allowance_usdc=settings.portfolio_budget_usdc * Decimal("10"),
    )

    background_tasks = start_paper_background_tasks(
        ledger=paper_ledger,
        account_state_store=account_state_store,
        portfolio_budget_usdc=settings.portfolio_budget_usdc,
        registry=registry,
        market_ws_worker=market_ws_worker,
    )

    # GoalserveLazy 提供 schedule / h2h 等按需拉取（不轮询，仅 lookup 时调用）。
    goalserve_lazy_client: GoalserveLazyClient | None = None
    api_key_secret = settings.goalserve_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret else None
    if api_key:
        goalserve_lazy_client = GoalserveLazyClient(
            api_key=api_key,
            proxy=settings.goalserve_proxy,
            cache_ttl_s=3600.0,
        )
        logger.info("GoalserveLazy (schedule/h2h) started")
    logger.warning(
        "paper_trading_mode=true → PaperSubmitOnlyOrderClient + 本地签名 + 虚拟余额 %s USDC。"
        "WS 盘口=market_ws_worker.snapshot, syncer 每秒同步 paper_ledger → account_state_store",
        settings.portfolio_budget_usdc,
    )

    return execution_client, paper_ledger, goalserve_lazy_client, background_tasks
