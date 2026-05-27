from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from polymarket_trader.app.operator_service import OperatorService
from polymarket_trader.domain.time_filters import TimeRange

if TYPE_CHECKING:
    # R16-D (Code Q1) 强类型化——避免 main → api → main 循环 import，
    # 用 TYPE_CHECKING + 字符串 hint。运行时 fastapi Depends 仍返回真实
    # RuntimeComponents 实例，类型层让 IDE / mypy 报错 getattr 兜底误用。
    from polymarket_trader.main import RuntimeComponents


def get_runtime(request: Request) -> "RuntimeComponents":
    provider = getattr(request.app.state, "get_runtime", None)
    if callable(provider):
        runtime = provider()
        if runtime is not None:
            return runtime
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    return runtime


def get_operator_service(request: Request) -> OperatorService:
    provider = getattr(request.app.state, "get_operator_service", None)
    if callable(provider):
        service = provider()
        if service is not None:
            return service
    service = getattr(request.app.state, "operator_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="operator_service_unavailable")
    return service


def build_time_range(*, since: int | None, until: int | None) -> TimeRange | None:
    """统一把路由层 ``since`` / ``until`` epoch_ms 转成 domain ``TimeRange``。

    ``since > until`` 由 ``TimeRange`` 构造抛出 ``ValueError``，这里固定映射
    成 HTTP 400 ``since_after_until``，避免每条路由重写校验。
    """

    if since is None and until is None:
        return None
    try:
        return TimeRange(since_ms=since, until_ms=until)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
