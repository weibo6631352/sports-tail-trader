"""Goalserve inplay WS token 冷却、可观测性与持久化回归测试。

历史故障：gettoken 直连被拒（401），8 个 sport 各自每 60s 重试，持续打爆
gettoken 配额（429）。且 token 占用无任何记录，重启后无从得知槽位耗尽。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polymarket_trader.infra.sports.goalserve_client import GoalserveClient


@pytest.mark.asyncio
async def test_gettoken_failure_triggers_shared_cooldown(tmp_path: Path) -> None:
    """一次 gettoken 失败后进入共享冷却，期内不再重复打 gettoken。"""

    client = GoalserveClient(
        api_key="test",
        sports=("basketball", "soccer", "hockey"),
        token_cache_path=tmp_path / "token.json",
    )
    calls = 0

    async def _failing_fetch() -> tuple[str, float]:
        nonlocal calls
        calls += 1
        raise RuntimeError("429 Too Many Requests")

    client._fetch_token = _failing_fetch  # type: ignore[method-assign]

    # 第一次：真正打 gettoken，失败，设冷却。
    with pytest.raises(RuntimeError):
        await client._ensure_token()
    assert calls == 1

    # 冷却期内的后续 sport：直接抛错，不再打 gettoken。
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await client._ensure_token()
    assert calls == 1, "冷却期内不应重复调用 gettoken"

    assert client._token_fetch_count == 1
    assert client._token_fetch_failures == 1
    assert client._token_cooldown_until > 0


@pytest.mark.asyncio
async def test_ws_per_sport_status_records_token_lifecycle(tmp_path: Path) -> None:
    """ws_per_sport_status 首条必须是 token 生命周期记录，暴露占用与冷却。"""

    client = GoalserveClient(
        api_key="test",
        sports=("basketball", "soccer"),
        token_cache_path=tmp_path / "token.json",
    )

    async def _failing_fetch() -> tuple[str, float]:
        raise RuntimeError("401 Unauthorized")

    client._fetch_token = _failing_fetch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await client._ensure_token()

    status = client.ws_per_sport_status()
    token_row = status[0]
    assert token_row["sport"] == "_inplay_token"
    assert token_row["type"] == "token"
    assert token_row["token_present"] is False
    assert token_row["gettoken_count"] == 1
    assert token_row["gettoken_failures"] == 1
    assert token_row["cooldown_remaining_s"] > 0
    assert "401" in (token_row["last_error"] or "")
    assert token_row["configured_sports"] == 2


@pytest.mark.asyncio
async def test_token_record_persists_across_restart(tmp_path: Path) -> None:
    """token 占用记录必须持久化：重启（新实例）后仍能恢复累计计数与冷却。"""

    cache = tmp_path / "token.json"
    client = GoalserveClient(api_key="test", sports=("soccer",), token_cache_path=cache)

    async def _failing_fetch() -> tuple[str, float]:
        raise RuntimeError("429 Too Many Requests")

    client._fetch_token = _failing_fetch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await client._ensure_token()

    # 模拟重启：同一缓存路径新建实例，记录必须恢复。
    restarted = GoalserveClient(api_key="test", sports=("soccer",), token_cache_path=cache)
    assert restarted._token_fetch_count == 1
    assert restarted._token_fetch_failures == 1
    assert restarted._token_cooldown_until > 0  # 冷却跨重启仍生效
    assert "429" in (restarted._last_token_error or "")


def test_client_accepts_proxy(tmp_path: Path) -> None:
    """proxy 参数必须被接受并保存（inplay 直连会 401，须走代理）。"""

    client = GoalserveClient(
        api_key="test",
        sports=("soccer",),
        proxy="http://127.0.0.1:7890",
        token_cache_path=tmp_path / "token.json",
    )
    assert client._proxy == "http://127.0.0.1:7890"
