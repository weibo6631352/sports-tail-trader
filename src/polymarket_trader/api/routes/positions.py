from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/positions", tags=["positions"])


# 实测 Polymarket clob 自带这两个真实流动性接口:
# /midpoint?token_id=X  → {"mid": "0.755"}   (UI 卖出按钮显示的就是 mid 价,实际可成交)
# /price?token_id=X&side=SELL → {"price": "0.76"} (考虑接盘 spread 的 best sellable)
# data-api.polymarket.com/positions 的 curPrice 是 last-trade 价,在冷门盘上滞后/高估;
# 此端点用 midpoint 算"真实可卖价 = mid × size",避免 mid 显示高估误导决策。
async def _fetch_token_liquidity(client: httpx.AsyncClient, token_id: str) -> dict[str, Any]:
    """并发拉 midpoint + best sell price。两个独立 endpoint,任意失败给 None。"""
    async def _get(path: str) -> dict[str, Any] | None:
        try:
            r = await client.get(f"https://clob.polymarket.com{path}", timeout=4.0)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None
    mid_resp, sell_resp = await asyncio.gather(
        _get(f"/midpoint?token_id={token_id}"),
        _get(f"/price?token_id={token_id}&side=SELL"),
    )
    return {
        "midpoint": mid_resp.get("mid") if mid_resp else None,
        "best_sell_price": sell_resp.get("price") if sell_resp else None,
    }


class ForceExitRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    price: Decimal | None = Field(default=None, gt=Decimal("0"), lt=Decimal("1"))
    operator: str = "manual"
    reason: str = "admin_force_exit"
    trace_id: str | None = None


@router.get("")
async def list_positions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_positions(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        token_id=token_id,
        strategy_id=strategy_id,
    )


@router.post("/force-exit")
async def force_exit(
    request: ForceExitRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.force_exit_position(
        condition_id=request.condition_id,
        token_id=request.token_id,
        price=request.price,
        operator=request.operator,
        reason=request.reason,
        trace_id=request.trace_id,
    )


@router.get("/liquidity")
async def positions_liquidity(
    service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    """对当前所有持仓并发拉 Polymarket 真实流动性(midpoint + best sell price),
    返回真实可卖价 = mid × shares。

    解决问题:data-api 的 curPrice/currentValue 是 last-trade 价,冷门赛事滞后/高估
    会让浮盈/止盈决策被误导。这里给 admin/UI/策略一个统一的"真实可卖价"基线。
    """
    positions_resp = await service.list_positions(limit=500, offset=0)
    items = positions_resp.get("items", [])
    if not items:
        return {"items": []}

    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        # 并发拉所有 token 的流动性,任一失败不影响其它
        liquidity_tasks = {
            p.get("token_id"): _fetch_token_liquidity(client, p.get("token_id"))
            for p in items if p.get("token_id")
        }
        results = await asyncio.gather(*liquidity_tasks.values(), return_exceptions=True)
        liq_by_token = dict(zip(liquidity_tasks.keys(), results, strict=True))

    for p in items:
        token_id = p.get("token_id")
        liq = liq_by_token.get(token_id)
        if isinstance(liq, Exception) or liq is None:
            mid = None
            sell_p = None
        else:
            mid = liq.get("midpoint")
            sell_p = liq.get("best_sell_price")
        shares = p.get("shares") or 0
        try:
            mid_d = Decimal(str(mid)) if mid is not None else None
            shares_d = Decimal(str(shares))
            realizable = float(mid_d * shares_d) if mid_d is not None else None
        except Exception:
            realizable = None
        out.append({
            "condition_id": p.get("condition_id"),
            "token_id": token_id,
            "market_slug": p.get("market_slug"),
            "outcome": p.get("outcome"),
            "shares": str(shares),
            "cur_price_data_api": p.get("cur_price"),  # data-api last-trade,可能滞后
            "midpoint": mid,                            # clob 真实 mid
            "best_sell_price": sell_p,                  # 当前能 SELL 接到的价
            "realizable_value_usdc": str(realizable) if realizable is not None else None,
            "current_value_data_api": p.get("current_value"),
        })
    return {"items": out}
