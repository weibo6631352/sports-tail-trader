from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from polymarket_trader.domain.events import sanitize_raw_response


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _summary(raw_response: Any | None, *, max_length: int = 512) -> str | None:
    return sanitize_raw_response(raw_response, max_length=max_length)


def _normalize_http_proxy_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    scheme = urlparse(text).scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    return text


def _resolve_proxy_url(base_url: str) -> str | None:
    scheme = urlparse(base_url).scheme.lower()
    candidates: list[str | None] = []
    if scheme == "https":
        candidates.extend((os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy")))
    if scheme in {"http", "https"}:
        candidates.extend((os.environ.get("HTTP_PROXY"), os.environ.get("http_proxy")))
    candidates.extend((os.environ.get("ALL_PROXY"), os.environ.get("all_proxy")))

    for candidate in candidates:
        normalized = _normalize_http_proxy_url(candidate)
        if normalized is not None:
            return normalized
    return None


def _normalize_response_payload(payload: Any) -> Any:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("data", "events", "markets", "items", "results", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return payload
    return payload


class PolymarketClientError(RuntimeError):
    """Common error base for Polymarket external protocol adapters."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        url: str | None = None,
        status_code: int | None = None,
        code: str | None = None,
        retry_after_s: float | None = None,
        raw_response_summary: str | None = None,
        raw_response: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.url = url
        self.status_code = status_code
        self.code = code
        self.retry_after_s = retry_after_s
        self.raw_response_summary = raw_response_summary
        self.raw_response = raw_response

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "operation": self.operation,
            "url": self.url,
            "status_code": self.status_code,
            "code": self.code,
            "retry_after_s": self.retry_after_s,
            "raw_response_summary": self.raw_response_summary,
        }


class PolymarketTimeoutError(PolymarketClientError):
    """Request or WebSocket timeout."""


class PolymarketAuthError(PolymarketClientError):
    """Authentication failure or missing credentials."""


class PolymarketRateLimitError(PolymarketClientError):
    """Rate limit or temporary upstream block."""


class PolymarketTransportError(PolymarketClientError):
    """Network, protocol, or upstream 5xx failure."""


class PolymarketResponseError(PolymarketClientError):
    """Reachable response with an invalid payload."""


class PolymarketWebSocketError(PolymarketClientError):
    """WebSocket connection, subscription, or message parsing failure."""


def _normalize_client_error(
    exc: Exception,
    *,
    operation: str,
    url: str | None = None,
    raw_response: Any | None = None,
) -> PolymarketClientError:
    if isinstance(exc, PolymarketClientError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return PolymarketTimeoutError(
            f"{operation} timed out",
            operation=operation,
            url=url,
            raw_response_summary=_summary(raw_response),
            raw_response=raw_response,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return _error_from_response(response, operation=operation)
    if isinstance(exc, httpx.HTTPError):
        return PolymarketTransportError(
            f"{operation} transport error: {exc}",
            operation=operation,
            url=url,
            raw_response_summary=_summary(raw_response),
            raw_response=raw_response,
        )
    return PolymarketResponseError(
        f"{operation} failed: {exc}",
        operation=operation,
        url=url,
        raw_response_summary=_summary(raw_response),
        raw_response=raw_response,
    )


def _error_from_response(
    response: httpx.Response,
    *,
    operation: str,
) -> PolymarketClientError:
    status_code = response.status_code
    retry_after_s: float | None = None
    with contextlib.suppress(Exception):
        header_value = response.headers.get("retry-after")
        if header_value:
            retry_after_s = float(header_value)

    detail: str | None = None
    code: str | None = None
    payload: Any | None = None
    with contextlib.suppress(Exception):
        payload = response.json()
        if isinstance(payload, Mapping):
            detail = _first_text(payload, "error", "message", "detail", "reason")
            code = _first_text(payload, "code", "error_code", "status")
        else:
            detail = str(payload)
    raw_summary = _summary(payload if payload is not None else response.text)
    message = detail or f"{operation} failed with HTTP {status_code}"
    if status_code in {401, 403}:
        return PolymarketAuthError(
            message,
            operation=operation,
            url=str(response.request.url),
            status_code=status_code,
            code=code,
            retry_after_s=retry_after_s,
            raw_response_summary=raw_summary,
            raw_response=payload,
        )
    if status_code == 429:
        return PolymarketRateLimitError(
            message,
            operation=operation,
            url=str(response.request.url),
            status_code=status_code,
            code=code,
            retry_after_s=retry_after_s,
            raw_response_summary=raw_summary,
            raw_response=payload,
        )
    if status_code >= 500:
        return PolymarketTransportError(
            message,
            operation=operation,
            url=str(response.request.url),
            status_code=status_code,
            code=code,
            retry_after_s=retry_after_s,
            raw_response_summary=raw_summary,
            raw_response=payload,
        )
    return PolymarketResponseError(
        message,
        operation=operation,
        url=str(response.request.url),
        status_code=status_code,
        code=code,
        retry_after_s=retry_after_s,
        raw_response_summary=raw_summary,
        raw_response=payload,
    )


class PolymarketRestClientBase:
    """Lightweight REST base with injectable `httpx.AsyncClient`."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        proxy = _resolve_proxy_url(self._base_url)
        client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": timeout_s,
            "headers": dict(headers or {}),
            "trust_env": False,
            # HTTP/2 multiplexing：单 TCP 连接上多个 stream 并发，HPACK 头部压缩
            # 减少重复字节。中国→美国代理跨洋链路下，每减一次握手 + 头部就少
            # 一次 RTT/带宽抢占——直接帮 WS 决策链路腾路。
            "http2": True,
        }
        if proxy is not None:
            client_kwargs["proxy"] = proxy
        self._client = client or httpx.AsyncClient(**client_kwargs)

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "PolymarketRestClientBase":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        operation: str = "",
        unwrap: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            response = await self._client.request(
                method=method,
                url=path,
                params=params,
                json=json_body,
                headers=dict(headers or {}),
                timeout=timeout_s,
            )
            response.raise_for_status()
        except Exception as exc:
            raise _normalize_client_error(exc, operation=operation or f"{method} {path}", url=url) from exc

        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except Exception as exc:
            raise PolymarketResponseError(
                f"{operation or method} returned non-JSON payload",
                operation=operation or f"{method} {path}",
                url=str(response.request.url),
                status_code=response.status_code,
                raw_response_summary=_summary(response.text),
                raw_response=response.text,
            ) from exc
        return _normalize_response_payload(payload) if unwrap else payload

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        operation: str = "",
        unwrap: bool = True,
    ) -> Any:
        return await self.request_json(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout_s=timeout_s,
            operation=operation or f"GET {path}",
            unwrap=unwrap,
        )

    async def post_json(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        operation: str = "",
        unwrap: bool = True,
    ) -> Any:
        return await self.request_json(
            "POST",
            path,
            params=params,
            json_body=json_body,
            headers=headers,
            timeout_s=timeout_s,
            operation=operation or f"POST {path}",
            unwrap=unwrap,
        )


__all__ = [
    "PolymarketAuthError",
    "PolymarketClientError",
    "PolymarketRateLimitError",
    "PolymarketResponseError",
    "PolymarketRestClientBase",
    "PolymarketTimeoutError",
    "PolymarketTransportError",
    "PolymarketWebSocketError",
]
