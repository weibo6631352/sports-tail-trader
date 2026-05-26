"""直播源标识 + 优先级 + inplay 覆盖运动清单。

`LiveSourceKey = (provider, sport)` 是订阅 / 分桶 / 优先级裁决的最小粒度。
一个 LiveSourceKey 覆盖该 sport 下所有 inplay 比赛（同 sport 的多场比赛
共用一个 source bucket，不区分 league）。

# 优先级（原架构方案 §3.3）

| Provider | Priority | 含义 |
|---|---|---|
| GOALSERVE_INPLAY | 100 | 比分 + 实时 odd（最高，核心 edge） |
| GOALSERVE_LIVESCORE | 50 | 纯比分（inplay 不覆盖的运动） |

**注意**：pregame 赔率（goalserve_pregame）**不在 live_source 范畴**——它是赛前
静态赔率，由独立链路 `pipeline/ingest/odds/goalserve_pregame_worker.py` 拉取，
直接写入 `MarketMetadataStore`。live_source 只承载比赛进行中实时数据（比分 +
inplay 赔率）。

`MarketMetadataStore.write_live_state` 内部按优先级裁决：高 prio 总覆盖；同 prio
取新 ts；低 prio 仅在高 prio stale 时覆盖。

# inplay 覆盖运动（CLAUDE.md §9）

`INPLAY_COVERED_SPORTS` 列出 Goalserve inplay feed 覆盖的 8 个 sport，使用**策略
规范码**（与 `_market_sport_codes` / `SPORT_CODE_TO_INPLAY_KEYS.keys()` 一致），
不是 inplay feed 内部的路径 token。这样保证 subscription_policy → registry →
feeder → goalserve client 整链 sport 语义统一，client 内部 SPORT_CODE_TO_INPLAY_KEYS
把规范码映射到 inplay 路径 token。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LiveSourceProvider(StrEnum):
    GOALSERVE_INPLAY = "goalserve_inplay"
    GOALSERVE_LIVESCORE = "goalserve_livescore"


@dataclass(frozen=True, slots=True)
class LiveSourceKey:
    """直播源唯一标识——(provider, sport) 维度。"""

    provider: LiveSourceProvider
    sport: str

    def as_label(self) -> str:
        return f"{self.provider.value}:{self.sport}"


_PROVIDER_PRIORITY: dict[LiveSourceProvider, int] = {
    LiveSourceProvider.GOALSERVE_INPLAY: 100,
    LiveSourceProvider.GOALSERVE_LIVESCORE: 50,
}


def provider_priority(provider: LiveSourceProvider) -> int:
    return _PROVIDER_PRIORITY[provider]


def source_priority(source: LiveSourceKey) -> int:
    return provider_priority(source.provider)


INPLAY_COVERED_SPORTS: frozenset[str] = frozenset(
    {
        "football",
        "basketball",
        "tennis",
        "volleyball",
        "american-football",
        "esports",
        "ice-hockey",
        "baseball",
    }
)
