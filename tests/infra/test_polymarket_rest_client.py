from __future__ import annotations

from polymarket_trader.infra.polymarket.base_client import _resolve_proxy_url


def test_resolve_proxy_url_prefers_https_proxy_and_ignores_socks_all_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7898")
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7899")

    proxy = _resolve_proxy_url("https://gamma-api.polymarket.com")

    assert proxy == "http://127.0.0.1:7897"


def test_resolve_proxy_url_returns_none_when_only_unsupported_proxy_exists(monkeypatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7899")

    proxy = _resolve_proxy_url("https://gamma-api.polymarket.com")

    assert proxy is None
