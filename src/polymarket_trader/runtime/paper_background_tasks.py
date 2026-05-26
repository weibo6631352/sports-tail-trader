"""Paper 模式专用的后台 task。

只在 ``settings.paper_trading_mode=true`` 时由 main.py 启动。balance + positions
投影（§5 单 writer）走 [`app/paper/paper_balance_syncer.py`](../app/paper/paper_balance_syncer.py)；
本文件负责其余 3 个采样/探测 task（equity_curve / system_perf / eventloop_lag）以及
统一的 task 启动 orchestration。

为什么不放进 ``workers/``：workers 是 P0/P2 主链路上的常驻 service（带
supervisor heartbeat、event_bus 订阅）；这些只是定时采样与投影任务，
失败时无后果（顶多丢一帧统计），保持轻量函数式即可。
"""

from __future__ import annotations

import asyncio
import logging
import platform
import resource
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from polymarket_trader.app.paper.paper_balance_syncer import paper_balance_syncer

if TYPE_CHECKING:
    from polymarket_trader.app.paper import PaperVirtualLedger
    from polymarket_trader.runtime.account_state import AccountStateStore
    from polymarket_trader.runtime.registry import MarketRegistry
    from polymarket_trader.pipeline.ingest.orderbook_ws import MarketWsWorker


logger = logging.getLogger(__name__)


# equity_curve 上限：30s/点 × 2880 ≈ 24h
_EQUITY_CURVE_MAX_POINTS = 2880


async def _equity_curve_recorder(
    *,
    ledger: "PaperVirtualLedger",
    market_ws_worker: "MarketWsWorker",
) -> None:
    """每 30s 追加一行 equity_snapshot 到 paper_ledger.equity_curve。

    in-memory 滚动窗口（重启清零），最多保留 ``_EQUITY_CURVE_MAX_POINTS`` 条
    （30s × 2880 = 24h）。供 operator /runtime/equity-curve 端点画资金曲线。
    """

    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

    while True:
        try:
            total_cost = Decimal("0")
            unrealized_value = Decimal("0")
            for tok, shares in ledger.positions.items():
                cost = ledger.cost_basis_usdc.get(tok, Decimal("0"))
                total_cost += cost
                ob = market_ws_worker.snapshot(tok)
                if ob and ob.best_bid is not None and ob.sell_actionable:
                    unrealized_value += shares * ob.best_bid
            equity = ledger.available_usdc + unrealized_value
            ledger.equity_curve.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "available_usdc": str(ledger.available_usdc),
                    "total_cost_usdc": str(total_cost),
                    "unrealized_value_usdc": str(unrealized_value),
                    "equity_usdc": str(equity),
                    "positions_count": len(ledger.positions),
                    "fees_accrued_usdc": str(ledger.fees_accrued_usdc),
                }
            )
            if len(ledger.equity_curve) > _EQUITY_CURVE_MAX_POINTS:
                del ledger.equity_curve[:-_EQUITY_CURVE_MAX_POINTS]
            SystemPerfMonitor.get().worker_tick(
                "equity_curve_recorder", expected_interval_s=30.0
            )
        except Exception:
            logger.warning("equity_curve_recorder.tick_failed", exc_info=True)
        await asyncio.sleep(30)


async def _system_perf_sampler(
    *,
    ledger: "PaperVirtualLedger",
    portfolio_budget_usdc: Decimal,
) -> None:
    """每 30s 采样 RSS + PnL，写入 SystemPerfMonitor。

    用 Python 内置 ``resource`` 模块，不引入 psutil 依赖。
    PnL = 最新 equity_curve 点的 equity_usdc - 初始 budget。
    """

    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

    mon = SystemPerfMonitor.get()
    while True:
        try:
            ru = resource.getrusage(resource.RUSAGE_SELF)
            rss_bytes = (
                ru.ru_maxrss if platform.system() == "Darwin" else ru.ru_maxrss * 1024
            )
            mon.sample_memory(rss_bytes / 1024 / 1024)
        except Exception:
            logger.warning("system_perf_sampler.rss_failed", exc_info=True)
        try:
            if ledger.equity_curve:
                last = ledger.equity_curve[-1]
                pnl = float(last["equity_usdc"]) - float(portfolio_budget_usdc)
                mon.sample_pnl(pnl)
        except Exception:
            logger.warning("system_perf_sampler.pnl_failed", exc_info=True)
        await asyncio.sleep(30)


async def _eventloop_lag_prober() -> None:
    """每 2s probe 一次 event loop 调度延迟。

    await sleep(0.1)；实际耗时 - 100ms = loop 被某个 callback 卡住的时长。
    单次 lag > 50ms 记 slow_callback，便于排查 P0 路径上的阻塞 await。
    """

    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

    mon = SystemPerfMonitor.get()
    target_sleep_s = 0.1
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(target_sleep_s)
        actual_ms = (time.perf_counter() - t0) * 1000
        lag_ms = actual_ms - target_sleep_s * 1000
        mon.record_eventloop_lag(max(0.0, lag_ms))
        if lag_ms > 50:
            mon.record_slow_callback(lag_ms, task_name="eventloop_lag_prober_observed")
        await asyncio.sleep(2.0)


def start_paper_background_tasks(
    *,
    ledger: "PaperVirtualLedger",
    account_state_store: "AccountStateStore",
    portfolio_budget_usdc: Decimal,
    registry: "MarketRegistry",
    market_ws_worker: "MarketWsWorker",
) -> dict[str, asyncio.Task]:
    """启动 4 个 paper-mode 后台 task。

    同时把 asyncio loop 的 slow_callback_duration 调到 0.05s——让 asyncio 内置
    warning 也能捕捉到 50ms+ 的阻塞 callback（与 eventloop_lag_prober 互为兜底）。

    返回 dict[name -> Task]，调用方持有引用避免被 GC 提前回收。
    """

    try:
        asyncio.get_event_loop().slow_callback_duration = 0.05
    except Exception:
        logger.warning("paper_background_tasks.slow_callback_duration_set_failed", exc_info=True)

    tasks: dict[str, asyncio.Task] = {
        "paper_balance_syncer": asyncio.create_task(
            paper_balance_syncer(
                ledger=ledger,
                account_state_store=account_state_store,
                portfolio_budget_usdc=portfolio_budget_usdc,
                registry=registry,
                market_ws_worker=market_ws_worker,
            ),
            name="paper_balance_syncer",
        ),
        "equity_curve_recorder": asyncio.create_task(
            _equity_curve_recorder(
                ledger=ledger,
                market_ws_worker=market_ws_worker,
            ),
            name="paper_equity_curve",
        ),
        "system_perf_sampler": asyncio.create_task(
            _system_perf_sampler(
                ledger=ledger,
                portfolio_budget_usdc=portfolio_budget_usdc,
            ),
            name="system_perf_sampler",
        ),
        "eventloop_lag_prober": asyncio.create_task(
            _eventloop_lag_prober(),
            name="eventloop_lag_prober",
        ),
    }
    return tasks
