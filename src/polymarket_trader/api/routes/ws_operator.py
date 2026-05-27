"""WebSocket operator stream endpoint —— 前端订阅 operator_ws_publisher 增量推送。

原架构方案 §12.3 + ws_operator/publisher.py. publisher 早已实装完整：
event_bus broadcast listener → 按 topic 路由 → 推 subscriber。本 endpoint
是最后一步：接收前端 WebSocket，调 publisher.subscribe/unsubscribe，
把 `starlette.websockets.WebSocket` 当作 WsSubscriber 协议实例传进去。

# 协议

- 连接 URL: `ws://host/operator/stream?topics=portfolio,candidates,live_states,health`
- topics 默认全订阅（缺省时订阅所有 known_topics）
- 服务端 push 形式：`{topic, event_type, event_id, trace_id, ...}`（publisher
  序列化的 dict）
- 客户端可发文本 message `{action: "subscribe" | "unsubscribe", topics: [...]}`
  动态调整订阅；其他 message 忽略
- 任意 IO 错误自动 unsubscribe + close

# 鉴权

跟 SSE /stream/events 保持一致——API token 通过 query param 或 header 传入。
WebSocket 协议本身不支持 Authorization header 优雅传递；这里采用 query param
`?token=<api_token>` 校验。生产环境应配合 TLS 防止 token 明文泄漏。

# 与 SSE /stream/events 区别

| 维度 | SSE | WebSocket |
|---|---|---|
| 方向 | 单向 server→client | 双向（client 可发订阅变更） |
| 过滤 | 单连接固定 event_types + condition_id | 单连接 topic 动态切换 |
| 适用 | 长期监听原始 audit 事件流 | 仪表盘按面板 topic 订阅高聚合数据 |
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from polymarket_trader.api.ws.publisher import OperatorWsPublisher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/operator", tags=["ws-operator"])


def _resolve_publisher(websocket: WebSocket) -> OperatorWsPublisher | None:
    runtime = None
    state = websocket.app.state
    provider = getattr(state, "get_runtime", None)
    if callable(provider):
        runtime = provider()
    if runtime is None:
        return None
    # R18: RuntimeComponents.operator_ws_publisher 非 Optional 直接访问
    return runtime.operator_ws_publisher


def _resolve_api_token(websocket: WebSocket) -> str | None:
    runtime = None
    state = websocket.app.state
    provider = getattr(state, "get_runtime", None)
    if callable(provider):
        runtime = provider()
    if runtime is None:
        return None
    # R18: runtime.settings 非 Optional；admin_api_token 是 Settings dynamic env 字段保留 getattr 兜底
    return getattr(runtime.settings, "admin_api_token", None)


def _parse_topics(raw: str | None, known: frozenset[str]) -> tuple[str, ...]:
    if not raw:
        return tuple(sorted(known))
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(item for item in parts if item in known)


@router.websocket("/stream")
async def operator_stream(
    websocket: WebSocket,
    topics: str | None = Query(default=None),
    token: str | None = Query(default=None),
) -> None:
    """operator WebSocket 增量推送 endpoint。

    Query params：
    - `topics`：逗号分隔（如 `portfolio,candidates,live_states,health`）；
      缺省订阅 publisher.known_topics() 全部
    - `token`：admin_api_token 校验（必填，与 SSE/REST 一致的 admin 鉴权）
    """

    publisher = _resolve_publisher(websocket)
    if publisher is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="publisher_unavailable")
        return

    expected_token = _resolve_api_token(websocket)
    if expected_token is not None and token != expected_token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="api_token_invalid")
        return

    known = publisher.known_topics()
    initial_topics = _parse_topics(topics, known)
    if not initial_topics:
        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA, reason="no_known_topics")
        return

    await websocket.accept()
    publisher.subscribe(websocket, initial_topics)
    current_topics: set[str] = set(initial_topics)
    logger.info(
        "operator_ws.subscribed",
        extra={"topics": list(initial_topics), "remote": str(websocket.client)},
    )

    try:
        await websocket.send_json({
            "kind": "ready",
            "subscribed_topics": sorted(current_topics),
            "known_topics": sorted(known),
        })
        while True:
            # 接收 client 控制 message；publisher 通过 send_json 单向推送
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            action = msg.get("action")
            req_topics_raw = msg.get("topics")
            req_topics: tuple[str, ...] = ()
            if isinstance(req_topics_raw, list):
                req_topics = tuple(
                    t for t in req_topics_raw if isinstance(t, str) and t in known
                )
            elif isinstance(req_topics_raw, str):
                req_topics = _parse_topics(req_topics_raw, known)
            if not req_topics:
                continue
            if action == "subscribe":
                publisher.subscribe(websocket, req_topics)
                current_topics.update(req_topics)
                await websocket.send_json({
                    "kind": "subscription_changed",
                    "subscribed_topics": sorted(current_topics),
                })
            elif action == "unsubscribe":
                publisher.unsubscribe(websocket, req_topics)
                current_topics.difference_update(req_topics)
                await websocket.send_json({
                    "kind": "subscription_changed",
                    "subscribed_topics": sorted(current_topics),
                })
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("operator_ws.unexpected_error")
    finally:
        publisher.unsubscribe(websocket)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "operator_ws.unsubscribed",
            extra={"remote": str(websocket.client)},
        )
