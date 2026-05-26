"""Admin API token 鉴权 middleware。

docs/新架构方案.md §12.4。从 api/app.py 抽出来集中维护，路由层只关心业务。

# 鉴权语义

- `ADMIN_API_TOKEN` 未配置 → 路由裸跑（仅本机开发场景；启动 warning）
- 配置了 → 非豁免路径必须带 `X-Admin-Token: <匹配值>`，否则 401
- OPTIONS 请求由 CORSMiddleware 处理，不在本 middleware 校验
- 豁免路径：健康检查 / OpenAPI docs / SSE / WebSocket operator stream
  （SSE 与 WS 浏览器不支持自定义 header，token 走 query param）
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

logger = logging.getLogger(__name__)


# 鉴权豁免前缀——前缀字符串匹配，扩展时直接加 tuple 元素
DEFAULT_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/ready",
    "/openapi.json",
    "/docs",
    "/redoc",
    # SSE / 只读 stream：前端 EventSource 不支持自定义 header，token 走 query param
    "/stream/",
    # WebSocket operator stream：浏览器 WebSocket 同样不支持自定义 header
    "/operator/stream",
)


def install_admin_token_middleware(
    app: FastAPI,
    *,
    expected_token: str | None,
    exempt_prefixes: tuple[str, ...] = DEFAULT_AUTH_EXEMPT_PREFIXES,
) -> None:
    """注册 API token middleware 到 FastAPI app。

    `expected_token=None` 时跳过校验（本机开发场景），但会在启动期输出 warning
    提示生产部署必须配置。
    """

    if expected_token is None:
        logger.warning(
            "admin_api_token not set — admin API endpoints accept anonymous requests. "
            "Set ADMIN_API_TOKEN in .env / environment for any non-local deployment."
        )

    @app.middleware("http")
    async def _admin_token_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if expected_token is None:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(prefix) for prefix in exempt_prefixes):
            return await call_next(request)
        # OPTIONS 是 CORS 预检——由 CORSMiddleware 处理，跳过 token 校验
        if request.method == "OPTIONS":
            return await call_next(request)
        provided = request.headers.get("x-admin-token")
        if provided != expected_token:
            return JSONResponse(status_code=401, content={"detail": "admin_token_required"})
        return await call_next(request)


__all__ = ["DEFAULT_AUTH_EXEMPT_PREFIXES", "install_admin_token_middleware"]
