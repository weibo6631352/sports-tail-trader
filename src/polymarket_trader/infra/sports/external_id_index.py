"""跨源 LiveEvent ID 合并索引（无状态，按轮重建）。

每次 ``SportsLiveAggregateClient.list_events`` 内重建一次：把多源
``LiveEvent`` 按 ``external_ids`` 的可传递交集合并成"同一场比赛"的组，
组内由 aggregate 的融合策略选出主源，其余作为 ``source_conflicts``
或证据保留下来。

设计取舍（详见 CLAUDE.md §8）：
- 不持久化、不跨轮维护，避免迁移代码与漂移；
- 各 source client 解析时尽量把已知 cross-ref 写进 ``Participant.external_ids``
  和 ``LiveEvent.external_ids``（例如 SofaScore payload 里携带的 ESPN id）；
- 无任何 external_id 交集时，调用方走文本+开赛时间窗 fallback（aggregate 内）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polymarket_trader.domain.sports_live import LiveEvent


@dataclass(frozen=True, slots=True)
class MergeGroup:
    """一组被判定为同场比赛的 LiveEvent 索引。

    ``decided_by`` 标注合并依据，用于 audit 与校准报告：
    "external_id:<scheme>" 表示按某外部 ID 命中；
    "text" 表示该 group 暂无 external_id 交集，aggregate 内会用 text+start_bucket
    继续合并（这种 group 只含一个成员，作为待文本融合的候选）。
    """

    indices: tuple[int, ...]
    decided_by: str
    schemes_used: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MergeReport:
    """ExternalIdIndex.merge 的报告，供 audit / 校准 harness 使用。"""

    groups: tuple[MergeGroup, ...] = field(default_factory=tuple)
    id_merge_count: int = 0
    singleton_count: int = 0


class ExternalIdIndex:
    """对一批 LiveEvent 做 external_id-first 的合并分组。

    内部用 union-find（不可变 index），不修改原 events。返回每个 LiveEvent
    所在 group 的 indices，调用方据此做后续融合。
    """

    @staticmethod
    def merge(events: tuple[LiveEvent, ...]) -> MergeReport:
        if not events:
            return MergeReport()

        # union-find on event index
        parent: list[int] = list(range(len(events)))

        def find(node: int) -> int:
            root = node
            while parent[root] != root:
                root = parent[root]
            while parent[node] != root:
                parent[node], node = root, parent[node]
            return root

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        # key = (scheme, ext_id_value) → first event index
        scheme_to_first: dict[tuple[str, str], int] = {}
        group_schemes: dict[int, set[str]] = {}
        for index, event in enumerate(events):
            event_keys = _collect_keys(event)
            for key in event_keys:
                existing = scheme_to_first.get(key)
                if existing is None:
                    scheme_to_first[key] = index
                else:
                    union(existing, index)

        # 收集每个 root 对应的成员与命中的 scheme
        groups_map: dict[int, list[int]] = {}
        for index, event in enumerate(events):
            root = find(index)
            groups_map.setdefault(root, []).append(index)
            for scheme, _value in _collect_keys(event):
                group_schemes.setdefault(root, set()).add(scheme)

        # 把同 root 的所有 schemes 合并（union 后 group_schemes 要按 root 重投）
        rooted_schemes: dict[int, set[str]] = {}
        for root, indices in groups_map.items():
            schemes: set[str] = set()
            for idx in indices:
                for scheme, _value in _collect_keys(events[idx]):
                    schemes.add(scheme)
            rooted_schemes[root] = schemes

        groups: list[MergeGroup] = []
        id_merge_count = 0
        singleton_count = 0
        for root, indices in groups_map.items():
            sorted_indices = tuple(sorted(indices))
            schemes = tuple(sorted(rooted_schemes.get(root, set())))
            if len(sorted_indices) > 1:
                id_merge_count += 1
                decided_by = "external_id:" + ",".join(schemes) if schemes else "external_id"
            else:
                singleton_count += 1
                decided_by = "text" if not schemes else "external_id_singleton"
            groups.append(
                MergeGroup(indices=sorted_indices, decided_by=decided_by, schemes_used=schemes)
            )

        return MergeReport(
            groups=tuple(groups),
            id_merge_count=id_merge_count,
            singleton_count=singleton_count,
        )


def _collect_keys(event: LiveEvent) -> tuple[tuple[str, str], ...]:
    """从 LiveEvent.external_ids + 各 Participant.external_ids 抽取 (scheme, value) 对。

    Participant 的 ID 也参与 group 合并（例如电竞同一支战队在多源各自有 id，
    通过 participant external_ids 可达成跨源关联）。空字符串值跳过。
    """

    keys: set[tuple[str, str]] = set()
    for scheme, value in event.external_ids.items():
        text = str(value or "").strip()
        if text:
            keys.add((str(scheme).strip().lower(), text))
    for participant in event.participants:
        for scheme, value in participant.external_ids.items():
            text = str(value or "").strip()
            if text:
                # participant id 单独命名空间，避免与 event 级 id 冲突
                keys.add((f"p:{str(scheme).strip().lower()}:{participant.role}", text))
    return tuple(keys)
