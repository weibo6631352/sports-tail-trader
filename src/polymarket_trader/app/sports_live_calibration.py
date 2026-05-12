"""体育直播源校准 harness 业务逻辑。

独立运行模式：不启动交易主链路，不下单。给定 aggregate_client 与 market 列表，
跑一次（或多轮）``list_events()`` 并把每个 market 跑 ``best_live_match``，
输出 ``SportsLiveCalibrationReport``，便于运维侧观测：
- 每 source × 每 league/sport 的拉取耗时、events 数、解析失败数、ID 合并率、
  文本 fallback 命中率、冲突触发率；
- 与 market 匹配率、未匹配 market 列表 + 原因；
- ``silent_gap`` 警告：``matched_markets / candidate_markets < threshold`` 或
  某 source 解析 0 events 但其他源对应 league 有数据时。

CLI 入口在 ``polymarket_trader.tools.sports_live_calibration``。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent, SportsLiveSnapshot
from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient
from polymarket_trader.infra.sports.external_id_index import ExternalIdIndex
from polymarket_trader.serialization import jsonable

LiveMatchFunction = Callable[[Market, tuple[LiveEvent, ...]], Any]


@dataclass(frozen=True, slots=True)
class CalibrationSourceMetrics:
    """单个 source 在一轮校准中的统计指标。"""

    source: str
    events_seen: int
    fetch_ms: float
    success: bool
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class CalibrationLeagueMetrics:
    """单个 league × source 矩阵格的统计：让运维看到"某 league 哪个源覆盖最佳"。"""

    league: str
    source: str
    events_count: int

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class CalibrationUnmatchedMarket:
    """未匹配上的 market + 可能原因（debug 用）。"""

    condition_id: str
    market_slug: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class CalibrationSilentGap:
    """silent_gap 警告项：识别失败导致看似无直播源。"""

    code: str
    league: str | None
    source: str | None
    matched_markets: int
    candidate_markets: int
    threshold: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class SportsLiveCalibrationReport:
    """一次校准跑的全量报告。"""

    observed_at: datetime
    rounds: int
    candidate_markets: int
    matched_markets: int
    aggregate_fetch_ms: float
    id_merge_rate: float
    text_fallback_rate: float
    conflict_rate: float
    source_metrics: tuple[CalibrationSourceMetrics, ...]
    league_metrics: tuple[CalibrationLeagueMetrics, ...]
    unmatched_markets: tuple[CalibrationUnmatchedMarket, ...]
    silent_gaps: tuple[CalibrationSilentGap, ...]
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def match_rate(self) -> float:
        return (self.matched_markets / self.candidate_markets) if self.candidate_markets else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "rounds": self.rounds,
            "candidate_markets": self.candidate_markets,
            "matched_markets": self.matched_markets,
            "match_rate": self.match_rate,
            "aggregate_fetch_ms": self.aggregate_fetch_ms,
            "id_merge_rate": self.id_merge_rate,
            "text_fallback_rate": self.text_fallback_rate,
            "conflict_rate": self.conflict_rate,
            "source_metrics": [s.as_dict() for s in self.source_metrics],
            "league_metrics": [s.as_dict() for s in self.league_metrics],
            "unmatched_markets": [u.as_dict() for u in self.unmatched_markets],
            "silent_gaps": [g.as_dict() for g in self.silent_gaps],
            "failures": list(self.failures),
        }


async def run_calibration(
    *,
    aggregate_client: SportsLiveAggregateClient,
    markets: Sequence[Market] = (),
    match_live_event: LiveMatchFunction | None = None,
    rounds: int = 1,
    silent_gap_threshold: float = 0.5,
    league_filter: Sequence[str] | None = None,
    source_filter: Sequence[str] | None = None,
) -> SportsLiveCalibrationReport:
    """跑 ``rounds`` 轮校准并返回归并后的报告。

    实际生产中通常 rounds=1（即时快照）；调试时可以跑多轮验证稳定性。

    ``silent_gap_threshold`` 是 ``matched / candidate`` 命中率下限。低于此值
    视为 silent_gap 警告（"识别失败导致看似无直播源"）。

    ``markets`` 与 ``match_live_event`` 都为可选。两者缺一即视为 aggregate-only 模式：
    跳过 market-matching loop 与 low_match_rate 检测，但仍计算 source × league 矩阵、
    ID 合并率、source_silent_with_peers_active 等 aggregate-level 指标。"""

    if rounds <= 0:
        rounds = 1
    observed_at = datetime.now(timezone.utc)
    all_snapshots: list[SportsLiveSnapshot] = []
    aggregate_fetch_ms = 0.0
    failures: list[str] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        try:
            snapshot = await aggregate_client.list_events()
        except Exception as exc:
            failures.append(f"aggregate_fetch_failed:{exc}")
            continue
        elapsed = (time.perf_counter() - t0) * 1000.0
        aggregate_fetch_ms += elapsed
        all_snapshots.append(snapshot)
    if not all_snapshots:
        return SportsLiveCalibrationReport(
            observed_at=observed_at,
            rounds=rounds,
            candidate_markets=len(markets),
            matched_markets=0,
            aggregate_fetch_ms=aggregate_fetch_ms,
            id_merge_rate=0.0,
            text_fallback_rate=0.0,
            conflict_rate=0.0,
            source_metrics=(),
            league_metrics=(),
            unmatched_markets=tuple(
                CalibrationUnmatchedMarket(
                    condition_id=m.condition_id,
                    market_slug=m.market_slug,
                    reason="aggregate_returned_no_events",
                )
                for m in markets
            ),
            silent_gaps=(
                CalibrationSilentGap(
                    code="aggregate_no_events",
                    league=None,
                    source=None,
                    matched_markets=0,
                    candidate_markets=len(markets),
                    threshold=silent_gap_threshold,
                    detail="aggregate_client returned no events on all rounds",
                ),
            ),
            failures=tuple(failures),
        )

    # 多轮取最后一轮做 match（实时性优先）；fetch_ms 累计。
    latest_snapshot = all_snapshots[-1]
    fused_events = latest_snapshot.events
    source_filter_set = {s.strip().lower() for s in (source_filter or ()) if s.strip()}
    league_filter_set = {ln.strip().upper() for ln in (league_filter or ()) if ln.strip()}

    # 应用 filter（仅影响 match/report 报告，不阻断 fetch）
    candidate_events = tuple(
        e
        for e in fused_events
        if (not league_filter_set or (e.league or "").upper() in league_filter_set)
        and (not source_filter_set or (e.source or "").lower() in source_filter_set)
    )

    # 计算源-级指标
    source_metrics: list[CalibrationSourceMetrics] = []
    for status in latest_snapshot.source_statuses:
        if source_filter_set and status.source.lower() not in source_filter_set:
            continue
        source_metrics.append(
            CalibrationSourceMetrics(
                source=status.source,
                events_seen=status.events_seen,
                fetch_ms=aggregate_fetch_ms / max(1, len(all_snapshots)),
                success=status.success,
                last_error=status.last_error,
            )
        )

    # 计算 league × source 矩阵
    league_counts: dict[tuple[str, str], int] = {}
    for event in candidate_events:
        key = ((event.league or "").upper(), (event.source or "").lower())
        league_counts[key] = league_counts.get(key, 0) + 1
    league_metrics = tuple(
        CalibrationLeagueMetrics(league=league, source=source, events_count=count)
        for (league, source), count in sorted(league_counts.items())
    )

    # ID 合并率：candidate_events 已经是 aggregate 融合后输出，包含 contributing_sources
    # 长度。把 contributing_sources>1 视为"被融合"的 group；其中含 external_id 交集的
    # 算 id-merge，纯 text-fallback 的算 text-fallback。重跑一遍 ExternalIdIndex.merge
    # 得到准确的 id_merge_count / singleton_count。
    if candidate_events:
        merged_groups = [e for e in candidate_events if len(e.contributing_sources) > 1]
        # 拆 id-merge vs text：参考 ExternalIdIndex 在原 events 上的报告——但融合后已
        # 不可见原 events。用 external_ids key 数量是否 >= 1 + len(contributing_sources)>1
        # 推断是否走过 id_merge；不严格但足够给 admin 看趋势。
        id_merged_groups = [
            e for e in merged_groups if len(e.external_ids) > 0
        ]
        total_groups = len(candidate_events)
        id_merge_rate = len(id_merged_groups) / max(1, total_groups)
        text_fallback_rate = (
            (len(merged_groups) - len(id_merged_groups)) / max(1, total_groups)
        )
    else:
        id_merge_rate = 0.0
        text_fallback_rate = 0.0
    # ExternalIdIndex import kept available for future fixture-based callers; mark as used.
    _ = ExternalIdIndex

    # conflict 触发率：含 ConflictRecord 的 event 比例
    if candidate_events:
        conflict_rate = sum(1 for e in candidate_events if e.source_conflicts) / len(candidate_events)
    else:
        conflict_rate = 0.0

    # market 匹配：仅在 markets 非空且提供 match 函数时执行；否则 aggregate-only 模式。
    matched = 0
    unmatched: list[CalibrationUnmatchedMarket] = []
    matching_enabled = bool(markets) and match_live_event is not None
    if matching_enabled:
        assert match_live_event is not None  # narrow for type-checkers
        for market in markets:
            match = match_live_event(market, candidate_events)
            if match is None:
                unmatched.append(
                    CalibrationUnmatchedMarket(
                        condition_id=market.condition_id,
                        market_slug=market.market_slug,
                        reason="no_live_event_matched",
                    )
                )
                continue
            matched += 1

    # silent_gap detection: low_match_rate 仅在启用 market-matching 时检测，
    # 避免 aggregate-only 模式下误报"全部市场未匹配"。
    silent_gaps: list[CalibrationSilentGap] = []
    candidate_count = len(markets)
    if matching_enabled and candidate_count > 0:
        match_rate = matched / candidate_count
        if match_rate < silent_gap_threshold:
            silent_gaps.append(
                CalibrationSilentGap(
                    code="low_match_rate",
                    league=None,
                    source=None,
                    matched_markets=matched,
                    candidate_markets=candidate_count,
                    threshold=silent_gap_threshold,
                    detail=f"matched/candidate = {match_rate:.3f} < threshold {silent_gap_threshold:.3f}",
                )
            )
    # 某 source 0 events 但其他源仍有 events → silent_gap。
    # source_statuses 的 events_seen 来自每个 provider 自己快照，是融合前的视角；
    # 用它判断"是否真的拉到 0"。融合后某 source 不再是 primary 不代表 events_seen=0。
    statuses = latest_snapshot.source_statuses
    if statuses:
        any_active = any(s.events_seen > 0 for s in statuses)
        if any_active:
            active_sources = sorted(s.source for s in statuses if s.events_seen > 0)
            for status in statuses:
                if status.events_seen == 0 and status.success:
                    silent_gaps.append(
                        CalibrationSilentGap(
                            code="source_silent_with_peers_active",
                            league=None,
                            source=status.source,
                            matched_markets=0,
                            candidate_markets=candidate_count,
                            threshold=silent_gap_threshold,
                            detail=(
                                f"source {status.source} returned 0 events while peers "
                                f"{active_sources} have live data"
                            ),
                        )
                    )

    return SportsLiveCalibrationReport(
        observed_at=observed_at,
        rounds=rounds,
        candidate_markets=len(markets),
        matched_markets=matched,
        aggregate_fetch_ms=aggregate_fetch_ms,
        id_merge_rate=id_merge_rate,
        text_fallback_rate=text_fallback_rate,
        conflict_rate=conflict_rate,
        source_metrics=tuple(source_metrics),
        league_metrics=league_metrics,
        unmatched_markets=tuple(unmatched),
        silent_gaps=tuple(silent_gaps),
        failures=tuple(failures),
    )


def run_calibration_sync(
    *,
    aggregate_client: SportsLiveAggregateClient,
    markets: Sequence[Market] = (),
    match_live_event: LiveMatchFunction | None = None,
    rounds: int = 1,
    silent_gap_threshold: float = 0.5,
    league_filter: Sequence[str] | None = None,
    source_filter: Sequence[str] | None = None,
) -> SportsLiveCalibrationReport:
    """同步包装；CLI 跑一次性脚本时用。"""

    return asyncio.run(
        run_calibration(
            aggregate_client=aggregate_client,
            markets=markets,
            match_live_event=match_live_event,
            rounds=rounds,
            silent_gap_threshold=silent_gap_threshold,
            league_filter=league_filter,
            source_filter=source_filter,
        )
    )
