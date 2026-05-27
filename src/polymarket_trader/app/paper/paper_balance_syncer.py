"""Paper-mode balance + positions 单 writer。

§5 反馈分工：paper 模式下唯一允许写 `AccountStateStore.balance / positions`
的入口。和 `pipeline/feedback/user_account_poller.py`（live 模式 writer）严格
互斥——run mode 决定哪个生效，绝不允许两个同时跑（违反 §17.3 单 writer 约束）。

每秒把 paper_ledger 投影回 account_state_store：

- balance: ``paper_ledger.available_usdc`` → ``balance_usdc``（否则 reconcile
  写回真链上 $0.x 余额阻塞 Kelly）
- positions: ``paper_ledger.positions`` → ``replace_positions``（否则 workflow 读
  stale account_state 持仓反复 reprice 已平仓 token，触发 simulate_fill
  卖空 ledger 持仓 → available 凭空涨的 bug）
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_trader.app.paper import PaperVirtualLedger
    from polymarket_trader.pipeline.ingest.orderbook_ws import MarketWsWorker
    from polymarket_trader.runtime.account_state import AccountStateStore
    from polymarket_trader.runtime.registry import MarketRegistry


logger = logging.getLogger(__name__)


async def paper_balance_syncer(
    *,
    ledger: "PaperVirtualLedger",
    account_state_store: "AccountStateStore",
    portfolio_budget_usdc: Decimal,
    registry: "MarketRegistry",
    market_ws_worker: "MarketWsWorker",
) -> None:
    """每秒把 paper_ledger 投影回 account_state_store。"""

    from polymarket_trader.domain.position import Position
    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

    allowance = portfolio_budget_usdc * Decimal("10")
    while True:
        try:
            account_state_store.update_balances(
                balance_usdc=ledger.available_usdc,
                allowance_usdc=allowance,
            )
            paper_positions: list[Position] = []
            for token_id, shares in ledger.positions.items():
                if shares <= Decimal("0"):
                    continue
                market = registry.get_by_token_id(token_id)
                if market is None:
                    continue
                cost = ledger.cost_basis_usdc.get(token_id, Decimal("0"))
                ob = market_ws_worker.snapshot(token_id)
                if ob is not None and ob.best_bid is not None and ob.sell_actionable:
                    ledger.observe_unrealized(token_id, ob.best_bid)
                # paper 路径 opened_at 从 ledger.first_fill_at 取（已存在的 setdefault
                # 语义 + SELL 对称 pop，状态生命周期完美对齐）。每秒重建 Position
                # 不丢 opened_at（ledger 一直存活到平仓）。
                paper_positions.append(
                    Position(
                        condition_id=market.condition_id,
                        token_id=token_id,
                        market_slug=market.market_slug,
                        shares=shares,
                        cost_usdc=cost,
                        opened_at=ledger.first_fill_at.get(token_id),
                    )
                )
            account_state_store.replace_positions(tuple(paper_positions))
            SystemPerfMonitor.get().worker_tick(
                "paper_balance_syncer", expected_interval_s=1.0
            )
        except Exception:
            logger.warning("paper_balance_syncer.tick_failed", exc_info=True)
        await asyncio.sleep(1)
