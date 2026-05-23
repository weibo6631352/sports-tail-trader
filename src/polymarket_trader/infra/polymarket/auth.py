from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from polymarket_trader.config import Settings
from polymarket_trader.domain.order import OrderSide, OrderType
from polymarket_trader.infra.polymarket.execution_response_mapper import (
    map_submit_response,
    response_reason,
)
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionRequest,
    OrderExecutionResponse,
)
from polymarket_trader.infra.polymarket.order_signing import build_signed_order


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_body(value: Any | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _require_py_clob_client() -> dict[str, Any]:
    """加载 Polymarket CLOB V2 SDK。

    2026-04-28 生产 CLOB 已切到 V2，旧版 `py-clob-client` 签出的订单会被
    撮合引擎以 `order_version_mismatch` 拒绝。这里集中加载 V2 SDK，避免交易
    主链路继续依赖旧签名结构。
    """

    proxy_keys = (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    )
    saved_proxy_env: dict[str, str] = {}
    for key in proxy_keys:
        value = os.environ.get(key)
        if value and value.lower().startswith(("socks://", "socks4://", "socks5://")):
            saved_proxy_env[key] = value
            os.environ.pop(key, None)
    try:
        from py_clob_client_v2.client import ClobClient as OfficialClobClient
        from py_clob_client_v2.clob_types import (
            ApiCreds,
            MarketOrderArgs,
            OrderArgs,
            OrderType as OfficialOrderType,
            OrderPayload,
            RequestArgs,
        )
        from py_clob_client_v2.headers.headers import create_level_2_headers
    except ImportError as exc:
        raise RuntimeError(
            "py-clob-client-v2 is required for production Polymarket authentication. "
            "Install project dependencies before enabling live trading."
        ) from exc
    finally:
        for key, value in saved_proxy_env.items():
            os.environ[key] = value
    return {
        "client": OfficialClobClient,
        "api_creds": ApiCreds,
        "market_order_args": MarketOrderArgs,
        "order_args": OrderArgs,
        "order_type": OfficialOrderType,
        "order_payload": OrderPayload,
        "request_args": RequestArgs,
        "create_level_2_headers": create_level_2_headers,
    }


@dataclass(frozen=True, slots=True)
class PolymarketCredentials:
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    wallet_private_key: str
    signer_private_key: str | None = None
    chain_id: int = 137
    signature_type: int = 0
    funder_address: str | None = None

    @property
    def signing_private_key(self) -> str:
        return self.signer_private_key or self.wallet_private_key

    @property
    def has_api_credentials(self) -> bool:
        return all((self.api_key, self.api_secret, self.api_passphrase))

    @classmethod
    def from_settings(cls, settings: Settings) -> "PolymarketCredentials | None":
        wallet_private_key = settings._secret_value(settings.wallet_private_key)
        if not wallet_private_key:
            return None
        api_key = settings._secret_value(settings.polymarket_api_key) or None
        api_secret = settings._secret_value(settings.polymarket_api_secret) or None
        api_passphrase = settings._secret_value(settings.polymarket_api_passphrase) or None
        signer_private_key = settings._secret_value(settings.signer_private_key) or None
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            wallet_private_key=wallet_private_key,
            signer_private_key=signer_private_key,
            chain_id=settings.polymarket_chain_id,
            signature_type=settings.polymarket_signature_type,
            funder_address=settings.polymarket_funder_address,
        )


@dataclass(frozen=True, slots=True)
class DerivedApiCredentials:
    api_key: str
    api_secret: str
    api_passphrase: str


class PolymarketTradingClient:
    """对官方 `py-clob-client-v2` 的轻量封装。

    这里把 L1 创建/派生 API creds、L2 HMAC headers 和已签名订单构造集中封装，
    避免把官方 SDK 对象泄漏到 app / domain 层。
    """

    def __init__(
        self,
        *,
        host: str,
        credentials: PolymarketCredentials,
    ) -> None:
        self._host = host
        self._credentials = credentials
        self._client: Any | None = None
        self._api_creds: DerivedApiCredentials | None = None
        if credentials.has_api_credentials:
            self._api_creds = DerivedApiCredentials(
                api_key=credentials.api_key or "",
                api_secret=credentials.api_secret or "",
                api_passphrase=credentials.api_passphrase or "",
            )
        self._lock = threading.RLock()

    def get_address(self) -> str:
        client = self._ensure_client(require_l2=False)
        return str(client.get_address())

    def get_api_credentials(self) -> DerivedApiCredentials:
        with self._lock:
            self._ensure_client(require_l2=True)
            if self._api_creds is None:
                raise RuntimeError("failed to load Polymarket API credentials")
            return self._api_creds

    @property
    def signature_type(self) -> int:
        return self._credentials.signature_type

    def build_l2_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: Any | None = None,
    ) -> dict[str, str]:
        exported = _require_py_clob_client()
        client = self._ensure_client(require_l2=True)
        creds = self.get_api_credentials()
        request_args = exported["request_args"](
            method=method.upper(),
            request_path=request_path,
            body=body,
            serialized_body=_json_body(body),
        )
        headers = exported["create_level_2_headers"](
            client.signer,
            exported["api_creds"](
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            ),
            request_args,
        )
        return {str(key): str(value) for key, value in headers.items()}

    def create_signed_order(self, request: OrderExecutionRequest) -> Any:
        exported = _require_py_clob_client()
        client = self._ensure_client(require_l2=False)
        return build_signed_order(client=client, exported=exported, request=request)

    def post_signed_order(
        self,
        signed_order: Any,
        *,
        order_type: str,
        post_only: bool = False,
    ) -> Mapping[str, Any]:
        exported = _require_py_clob_client()
        client = self._ensure_client(require_l2=True)
        official_order_type = getattr(exported["order_type"], order_type.upper())
        try:
            response = client.post_order(
                signed_order,
                order_type=official_order_type,
                post_only=post_only,
            )
        except Exception as exc:
            response = self._normalize_api_exception(exc)
        return self._normalize_response(response)

    def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        exported = _require_py_clob_client()
        client = self._ensure_client(require_l2=True)
        response = client.cancel_order(exported["order_payload"](orderID=order_id))
        normalized = dict(self._normalize_response(response))
        normalized.setdefault("order_id", order_id)
        normalized.setdefault("status", "cancelled")
        return normalized

    def _ensure_client(self, *, require_l2: bool) -> Any:
        exported = _require_py_clob_client()
        with self._lock:
            if self._client is None:
                api_creds = None
                if self._api_creds is not None:
                    api_creds = exported["api_creds"](
                        api_key=self._api_creds.api_key,
                        api_secret=self._api_creds.api_secret,
                        api_passphrase=self._api_creds.api_passphrase,
                    )
                self._client = exported["client"](
                    self._host,
                    self._credentials.chain_id,
                    self._credentials.signing_private_key,
                    api_creds,
                    self._credentials.signature_type,
                    self._credentials.funder_address,
                )
            if require_l2 and self._api_creds is None:
                # API key 可由签名钱包确定性派生；直接 derive 可以避免 V2 SDK 先尝试
                # create 时产生“Could not create api key”的可预期 400 噪音。
                derived = self._client.derive_api_key()
                self._api_creds = DerivedApiCredentials(
                    api_key=str(derived.api_key),
                    api_secret=str(derived.api_secret),
                    api_passphrase=str(derived.api_passphrase),
                )
                self._client.set_api_creds(
                    exported["api_creds"](
                        api_key=self._api_creds.api_key,
                        api_secret=self._api_creds.api_secret,
                        api_passphrase=self._api_creds.api_passphrase,
                    )
                )
            return self._client

    def _normalize_api_exception(self, exc: Exception) -> Mapping[str, Any]:
        """把 SDK 的 HTTP 400 响应还原为框架可判读的 CLOB 响应。

        V2 SDK 对 FAK no-fill、最小订单、余额不足等 400 响应抛异常；交易框架
        需要继续读取 `error` / `orderID` 来区分 no-fill、拒单和可重试失败。
        """

        error_msg = getattr(exc, "error_msg", None)
        if error_msg is None:
            raise exc
        normalized: dict[str, Any] = {"success": False, "status": "rejected"}
        if isinstance(error_msg, Mapping):
            normalized.update({str(key): value for key, value in error_msg.items()})
            if "error" not in normalized and "reason" in normalized:
                normalized["error"] = normalized["reason"]
        else:
            normalized["error"] = str(error_msg)
        order_id = normalized.get("orderID") or normalized.get("order_id") or normalized.get("id")
        if order_id is not None:
            normalized["order_id"] = str(order_id)
        return normalized

    def _normalize_response(self, response: Any) -> Mapping[str, Any]:
        if isinstance(response, Mapping):
            normalized = {str(key): value for key, value in response.items()}
        else:
            normalized = {"raw_response": response}
        order_id = (
            normalized.get("order_id")
            or normalized.get("orderID")
            or normalized.get("orderId")
            or normalized.get("id")
        )
        if order_id is not None:
            normalized["order_id"] = str(order_id)
        trade_id = normalized.get("trade_id") or normalized.get("tradeID") or normalized.get("tradeId")
        if trade_id is None:
            trade_ids = normalized.get("tradeIDs") or normalized.get("trade_ids")
            if isinstance(trade_ids, (list, tuple)) and trade_ids:
                trade_id = trade_ids[0]
        if trade_id is not None:
            normalized["trade_id"] = str(trade_id)
        reason = response_reason(normalized)
        if normalized.get("success") is False or reason:
            normalized["status"] = "rejected"
            if reason:
                normalized["reason"] = reason
        elif "status" not in normalized:
            if normalized.get("canceled") is True or normalized.get("cancelled") is True:
                normalized["status"] = "cancelled"
        return normalized


class PolymarketOrderExecutionClient:
    """`OrderExecutor` 的生产 Polymarket 适配器。"""

    def __init__(self, trading_client: PolymarketTradingClient) -> None:
        self._trading_client = trading_client
        self._signed_orders: dict[str, Any] = {}
        self._lock = threading.RLock()

    def sign_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        if request.action == "submit":
            signed_order = self._trading_client.create_signed_order(request)
            with self._lock:
                self._signed_orders[request.fingerprint()] = signed_order
            return OrderExecutionResponse(
                status="signed",
                raw_response={
                    "signed": True,
                    "action": request.action,
                    "idempotency_key": request.idempotency_key,
                },
                reason="signed",
            )

        if request.action == "replace":
            replacement_request = OrderExecutionRequest(
                action="submit",
                strategy_id=request.strategy_id,
                trace_id=request.trace_id,
                idempotency_key=request.idempotency_key,
                condition_id=request.condition_id,
                token_id=request.token_id,
                market_slug=request.market_slug,
                side=OrderSide.SELL,
                order_type=OrderType.GTC,
                price=request.new_price,
                size_shares=request.size_shares,
                post_only=request.post_only,
                reason=request.reason,
                retry_count=request.retry_count,
                timestamps=request.timestamps,
            )
            signed_order = self._trading_client.create_signed_order(replacement_request)
            with self._lock:
                self._signed_orders[request.fingerprint()] = signed_order
            return OrderExecutionResponse(
                status="signed",
                raw_response={
                    "signed": True,
                    "action": request.action,
                    "idempotency_key": request.idempotency_key,
                },
                reason="signed",
            )

        self._trading_client.get_api_credentials()
        return OrderExecutionResponse(
            status="signed",
            raw_response={"signed": True, "action": request.action},
            reason="signed",
        )

    def submit_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        with self._lock:
            signed_order = self._signed_orders.pop(request.fingerprint(), None)
        if signed_order is None:
            signed_order = self._trading_client.create_signed_order(request)
        response = self._trading_client.post_signed_order(
            signed_order,
            order_type=request.order_type.value if request.order_type is not None else "GTC",
            post_only=request.post_only,
        )
        return map_submit_response(response, request)

    def cancel_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        if not request.order_id:
            raise ValueError("cancel_order requires order_id")
        response = self._trading_client.cancel_order(request.order_id)
        return OrderExecutionResponse(
            status=_text(response.get("status")) or "cancelled",
            order_id=_text(response.get("order_id")) or request.order_id,
            raw_response=response,
            reason=_text(response.get("reason")) or "cancelled",
            retryable=False,
        )

    def replace_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        if not request.order_id:
            raise ValueError("replace_order requires order_id")
        cancel_response = self._trading_client.cancel_order(request.order_id)
        # Polymarket cancel API 返回 OK 后服务端 balance/share allowance 同步需 1-2s。
        # 立即 post replacement 会因为旧 SELL 仍占用 share allowance → 新 SELL 报
        # "not enough balance / allowance"。1.5s 是实测安全余量。
        # 这是 sync thread-pool 调用，time.sleep 不阻塞 asyncio 主循环。
        time.sleep(1.5)
        replacement_request = OrderExecutionRequest(
            action="submit",
            strategy_id=request.strategy_id,
            trace_id=request.trace_id,
            idempotency_key=request.idempotency_key,
            condition_id=request.condition_id,
            token_id=request.token_id,
            market_slug=request.market_slug,
            side=OrderSide.SELL,
            order_type=OrderType.GTC,
            price=request.new_price,
            size_shares=request.size_shares,
            post_only=request.post_only,
            reason=request.reason,
            retry_count=request.retry_count,
            timestamps=request.timestamps,
        )
        with self._lock:
            signed_order = self._signed_orders.pop(request.fingerprint(), None)
        if signed_order is None:
            signed_order = self._trading_client.create_signed_order(replacement_request)
        response = dict(
            self._trading_client.post_signed_order(
                signed_order,
                order_type=request.order_type.value if request.order_type is not None else "GTC",
                post_only=request.post_only,
            )
        )
        response["cancelled_order_id"] = request.order_id
        if "reason" not in response and "reason" in cancel_response:
            response["reason"] = cancel_response["reason"]
        return OrderExecutionResponse(
            status=_text(response.get("status")) or "live",
            order_id=_text(response.get("order_id")),
            trade_id=_text(response.get("trade_id")),
            raw_response=response,
            reason=_text(response.get("reason")) or "replaced",
            retryable=False,
        )


def build_order_execution_client(settings: Settings) -> PolymarketOrderExecutionClient | None:
    trading_client = build_trading_client(settings)
    if trading_client is None:
        return None
    return PolymarketOrderExecutionClient(trading_client)


def build_trading_client(settings: Settings) -> PolymarketTradingClient | None:
    credentials = PolymarketCredentials.from_settings(settings)
    if credentials is None:
        return None
    _require_py_clob_client()
    return PolymarketTradingClient(
        host=settings.polymarket_clob_host,
        credentials=credentials,
    )


__all__ = [
    "DerivedApiCredentials",
    "PolymarketCredentials",
    "PolymarketOrderExecutionClient",
    "PolymarketTradingClient",
    "build_trading_client",
    "build_order_execution_client",
]
