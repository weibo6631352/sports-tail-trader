"""LiveSourceFeeder —— 单个 provider 的数据拉取 + bucket 分发任务。

不直接发 HTTP 请求；委托给 goalserve client——client 内部已有 per-sport demand-
driven polling + speed limit + 429 backoff（设计良好，不动）。Feeder 周期性调
`client.list_events()` 拿全 sport snapshot，按 sport 分组推到 `LiveStateStore`
对应 bucket。

# 一个 provider 一个 feeder

- `GOALSERVE_INPLAY` → 1 个 feeder，注入 inplay client
- `GOALSERVE_LIVESCORE` → 1 个 feeder，注入 livescore client

不是每个 (provider, sport) 一个 feeder——避免 N 个 feeder 都调 `client.list_events()`
浪费（同一 client 返回全 sport 数据）。client 内部的 per-sport task 已经做了
demand-driven 分桶，feeder 只是周期性拉缓存 + 按 sport 推到 store。

# 启停

Feeder 在 main.py 启动时拉起（每 provider 一个，常驻）；不按订阅启停。实际
HTTP 流量由 client 内部 per-sport task 控制（registry.active_sports_for(provider)
注入 client 的 active_sports_provider），demand-driven 自然实现"无订阅不轮询"。

# 失败兜底

feeder fetch 异常 → 把所有 expected sport 的 bucket 标记为 FAILED（last_error 记
异常摘要）。listener 看到 FAILED bucket 自然不会触发 match（match_service 内
events 为空时早退）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
)

from .source import LiveSourceKey, LiveSourceProvider
from .store import LiveStateStore

logger = logging.getLogger(__name__)

SnapshotFetcher = Callable[[], Awaitable[SportsLiveSnapshot]]
ExpectedSportsProvider = Callable[[], frozenset[str]]
SportNormalizer = Callable[[str], str | None]


# Goalserve client parsers 内部部分用 inplay 路径 token（soccer / basket / hockey /
# amfootball）而非 workflow 规范码（football / basketball / ice-hockey / american-football）。
# feeder 在分桶前归一化，让 LiveSourceKey.sport 与 subscription_policy 输出的规范码
# 严格一致。新增 sport 时同步补这张映射，否则会被默认归一化为原文（即 raw token）。
_SPORT_TOKEN_TO_CODE: dict[str, str] = {
    "soccer": "football",
    "basket": "basketball",
    "amfootball": "american-football",
    "hockey": "ice-hockey",
}


def _default_sport_normalizer(raw: str) -> str | None:
    """归一化 LiveEvent.sport → 规范码。空 / 无法识别返回 None（事件直接丢弃）。"""

    if not raw:
        return None
    key = raw.strip().lower()
    if not key:
        return None
    return _SPORT_TOKEN_TO_CODE.get(key, key)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class LiveSourceFeederConfig:
    poll_interval_s: float = 1.0
    task_name: str = ""


class LiveSourceFeeder:
    """单 provider 的 snapshot 拉取 + bucket 分发。"""

    def __init__(
        self,
        *,
        provider: LiveSourceProvider,
        snapshot_fetcher: SnapshotFetcher,
        expected_sports_provider: ExpectedSportsProvider,
        store: LiveStateStore,
        sport_normalizer: SportNormalizer = _default_sport_normalizer,
        config: LiveSourceFeederConfig | None = None,
    ) -> None:
        self._provider = provider
        self._fetcher = snapshot_fetcher
        self._expected_sports = expected_sports_provider
        self._store = store
        self._normalize_sport = sport_normalizer
        self._cfg = config or LiveSourceFeederConfig()
        if not self._cfg.task_name:
            self._cfg.task_name = f"live-source-feeder:{provider.value}"
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def name(self) -> str:
        return self._cfg.task_name

    @property
    def provider(self) -> LiveSourceProvider:
        return self._provider

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=self._cfg.task_name)

    async def stop(self) -> None:
        self._running = False
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("feeder task %s exited with error during stop", self._cfg.task_name)
        self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                snapshot = await self._fetcher()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "live source feeder %s fetch failed", self._provider.value
                )
                self._mark_all_failed(str(exc))
                await asyncio.sleep(self._cfg.poll_interval_s)
                continue
            self._distribute(snapshot)
            await asyncio.sleep(self._cfg.poll_interval_s)

    def _distribute(self, snapshot: SportsLiveSnapshot) -> None:
        """按 sport 分组推到 store；expected 集合内但本次 snapshot 无事件的 sport，
        bucket 写空 events + SUCCESS_EMPTY，让 listener 知道这是"已查询无数据"
        而不是"未查询"。
        """

        expected = self._expected_sports()
        by_sport: dict[str, list[LiveEvent]] = {sport: [] for sport in expected}
        for event in snapshot.events:
            normalized = self._normalize_sport(event.sport or "")
            if not normalized:
                continue
            by_sport.setdefault(normalized, []).append(event)
        observed_at = snapshot.observed_at or _utc_now()
        for sport, events in by_sport.items():
            source = LiveSourceKey(provider=self._provider, sport=sport)
            health = (
                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                if events
                else SportsLiveSourceHealth.SUCCESS_EMPTY
            )
            self._store.update(
                source,
                tuple(events),
                observed_at=observed_at,
                health=health,
            )

    def _mark_all_failed(self, last_error: str) -> None:
        observed_at = _utc_now()
        for sport in self._expected_sports():
            source = LiveSourceKey(provider=self._provider, sport=sport)
            self._store.update(
                source,
                (),
                observed_at=observed_at,
                health=SportsLiveSourceHealth.FAILED,
                last_error=last_error[:200],  # 限制长度避免污染 audit
            )
