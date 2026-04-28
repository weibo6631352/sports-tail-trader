"""外部体育数据源适配器的公共工具。

本模块只承载协议错误归一化、时间和简单字段转换工具；各数据源的 payload
结构仍由各自 client 负责解释。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from polymarket_trader.domain.events import sanitize_raw_response


class SportsDataClientError(RuntimeError):
    """外部体育数据源错误基类。"""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        url: str | None = None,
        status_code: int | None = None,
        code: str | None = None,
        retry_after_s: float | None = None,
        raw_response_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.url = url
        self.status_code = status_code
        self.code = code
        self.retry_after_s = retry_after_s
        self.raw_response_summary = raw_response_summary

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化的错误摘要。"""

        return {
            "message": str(self),
            "operation": self.operation,
            "url": self.url,
            "status_code": self.status_code,
            "code": self.code,
            "retry_after_s": self.retry_after_s,
            "raw_response_summary": self.raw_response_summary,
        }


class SportsDataTimeoutError(SportsDataClientError):
    """外部体育数据源请求超时。"""


class SportsDataRateLimitError(SportsDataClientError):
    """外部体育数据源限流。"""


class SportsDataTransportError(SportsDataClientError):
    """外部体育数据源网络或服务端错误。"""


class SportsDataResponseError(SportsDataClientError):
    """外部体育数据源响应格式错误。"""


def normalize_sports_data_error(exc: Exception, *, operation: str) -> SportsDataClientError:
    """把 httpx 和解析异常归一化成体育数据源错误。"""

    if isinstance(exc, SportsDataClientError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return SportsDataTimeoutError(f"{operation} timed out", operation=operation)
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        retry_after_s: float | None = None
        try:
            retry_after = response.headers.get("retry-after")
            retry_after_s = None if retry_after is None else float(retry_after)
        except (TypeError, ValueError):
            retry_after_s = None
        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        error_cls = (
            SportsDataRateLimitError
            if response.status_code == 429
            else SportsDataTransportError if response.status_code >= 500 else SportsDataResponseError
        )
        return error_cls(
            f"{operation} failed with HTTP {response.status_code}",
            operation=operation,
            url=str(response.request.url),
            status_code=response.status_code,
            retry_after_s=retry_after_s,
            raw_response_summary=sanitize_raw_response(payload, max_length=512),
        )
    if isinstance(exc, httpx.HTTPError):
        return SportsDataTransportError(f"{operation} transport error: {exc}", operation=operation)
    return SportsDataResponseError(f"{operation} failed: {exc}", operation=operation)


def json_mapping_from_response(response: httpx.Response, *, operation: str) -> Mapping[str, Any]:
    """读取 JSON mapping 响应，并把格式错误归一化。"""

    try:
        payload = response.json()
    except Exception as exc:
        raise SportsDataResponseError(
            f"{operation} returned non-JSON payload",
            operation=operation,
            url=str(response.request.url),
            status_code=response.status_code,
            raw_response_summary=sanitize_raw_response(response.text, max_length=512),
        ) from exc
    if not isinstance(payload, Mapping):
        raise SportsDataResponseError(
            f"{operation} returned unexpected payload",
            operation=operation,
            url=str(response.request.url),
            status_code=response.status_code,
            raw_response_summary=sanitize_raw_response(payload, max_length=512),
        )
    return payload


def utc_now(now_provider: Callable[[], datetime] | None = None) -> datetime:
    """返回 UTC aware 时间，便于测试注入。"""

    value = datetime.now(timezone.utc) if now_provider is None else now_provider()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def timezone_for(value: str) -> tzinfo:
    """解析 IANA 时区，失败时回退 UTC。"""

    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return timezone.utc


def int_value(value: Any) -> int | None:
    """宽松读取整数值。"""

    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    """按顺序读取第一个非空文本字段。"""

    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
