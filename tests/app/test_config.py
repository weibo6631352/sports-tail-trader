from __future__ import annotations

from polymarket_trader.config import Settings


def test_settings_defaults_match_env_example(monkeypatch) -> None:
    for key in (
        "MAX_OPEN_ORDERS",
        "MARKET_SYNC_INTERVAL_SECONDS",
        "ORDER_RETRY_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.max_open_orders == 0
    assert settings.market_sync_interval_seconds == 60
    assert settings.order_retry_limit == 2
