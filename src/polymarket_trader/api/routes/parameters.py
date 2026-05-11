"""实时调参 API。

让 agent / 运维在不重启的前提下覆盖白名单内的风控与策略参数。每次写入都会
通过 ``PARAMETER_OVERRIDE_APPLIED`` 事件落 audit_events，保留完整审计链。
重启即丢——这是有意的：实时调参带探索性，长期固化应回到 ``.env`` / Settings。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_runtime

router = APIRouter(prefix="/parameters", tags=["parameters"])


class SetParameterRequest(BaseModel):
    value: Any = Field(description="新值；按 scope+key 注册的 coerce 函数归一化")
    operator: str = Field(default="agent", min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=512)
    expires_at: str | None = Field(default=None, description="ISO-8601；仅记录，调用侧不强制清除")
    # 前端 confirmAction 生成；PARAMETER_OVERRIDE_APPLIED 事件的 trace_id 用它
    # 让前端审计回看能精准匹配"操作意图 + 持久事件"。
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class ClearParameterRequest(BaseModel):
    operator: str = Field(default="agent", min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=512)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


def _get_param_store(request: Request) -> Any:
    runtime = get_runtime(request)
    store = getattr(runtime, "parameter_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="parameter_store_unavailable")
    return store


@router.get("")
async def list_parameters(
    request: Request,
) -> dict[str, object]:
    """所有可调参数注册表 + 当前 override 状态。"""

    store = _get_param_store(request)
    return {
        "parameters": store.registry_payload(),
        "active_override_count": len(store.snapshot()),
    }


@router.get("/overrides")
async def list_overrides(
    request: Request,
) -> dict[str, object]:
    """仅返回当前 active overrides——比 ``/parameters`` 更轻量。"""

    store = _get_param_store(request)
    return {"overrides": store.snapshot()}


@router.put("/{scope}/{key}")
async def set_parameter(
    scope: str,
    key: str,
    body: SetParameterRequest,
    request: Request,
) -> dict[str, object]:
    """覆盖 ``(scope, key)`` 的运行时值。

    coerce 失败返回 422；scope/key 不在白名单返回 404。
    """

    store = _get_param_store(request)
    try:
        payload = await store.set(
            scope=scope,
            key=key,
            value=body.value,
            operator=body.operator,
            reason=body.reason,
            expires_at=body.expires_at,
            trace_id=body.trace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return payload


@router.delete("/{scope}/{key}")
async def clear_parameter(
    scope: str,
    key: str,
    request: Request,
    body: ClearParameterRequest | None = None,
) -> dict[str, object]:
    """清除 ``(scope, key)`` 的覆盖——回到 Settings / 策略默认值。"""

    store = _get_param_store(request)
    body = body or ClearParameterRequest()
    try:
        return await store.clear(
            scope=scope,
            key=key,
            operator=body.operator,
            reason=body.reason,
            trace_id=body.trace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
