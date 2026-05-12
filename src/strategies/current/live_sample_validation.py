"""体育直播真实样本校验工具。

本模块服务当前体育扫尾策略的离线验收：把采集到的 ESPN scoreboard payload
和 Polymarket market 样本放在同一个 fixture 中，校验 ESPN 字段归一化结果、
比赛状态字段以及 market/game 匹配是否符合人工预期。

它不参与交易主链路，不访问外部网络，也不改变运行态状态——属于离线 dev tool，
CLAUDE.md §6 的运行时分层约束不适用，可直接 import infra/sports 复用 ESPN 解析。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.extension_api import load_mapping_file
from polymarket_trader.infra.sports import parse_espn_scoreboard_payload
from polymarket_trader.serialization import jsonable
from strategies.current.live_state import best_live_match, live_event_metadata


@dataclass(frozen=True, slots=True)
class SportsLiveSampleFailure:
    """真实样本校验失败项。"""

    code: str
    path: str
    message: str
    expected: Any = None
    actual: Any = None

    def as_payload(self) -> dict[str, Any]:
        """返回管理台和命令行可直接展示的失败记录。"""

        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "expected": jsonable(self.expected),
            "actual": jsonable(self.actual),
        }


@dataclass(frozen=True, slots=True)
class SportsLiveSampleReport:
    """ESPN 真实样本和 Polymarket market 匹配校验报告。"""

    source: str
    league: str
    events_seen: int
    markets_checked: int
    expected_events_checked: int
    expected_matches_checked: int
    matches: tuple[dict[str, Any], ...]
    failures: tuple[SportsLiveSampleFailure, ...]

    @property
    def passed(self) -> bool:
        """是否全部校验通过。"""

        return not self.failures

    def as_payload(self) -> dict[str, Any]:
        """返回可序列化报告。"""

        return {
            "passed": self.passed,
            "source": self.source,
            "league": self.league,
            "events_seen": self.events_seen,
            "markets_checked": self.markets_checked,
            "expected_events_checked": self.expected_events_checked,
            "expected_matches_checked": self.expected_matches_checked,
            "matches": jsonable(self.matches),
            "failures": [failure.as_payload() for failure in self.failures],
        }


def validate_sports_live_sample(sample: Mapping[str, Any]) -> SportsLiveSampleReport:
    """校验一份 ESPN/Polymarket 成对样本。

    fixture 顶层字段：
    - ``source``：当前只支持 ``espn``。
    - ``league``：ESPN league code，例如 ``nba``、``nhl``。
    - ``scoreboard``：ESPN scoreboard 原始 JSON。
    - ``markets``：Polymarket market 样本，字段与 entry replay fixture 对齐。
    - ``expected_games``：人工标注的状态字段期望。
    - ``expected_matches``：人工标注的 market -> ESPN event 匹配期望。
    """

    failures: list[SportsLiveSampleFailure] = []
    source = str(sample.get("source") or "espn").strip().lower()
    league = str(sample.get("league") or "").strip().lower()
    if source != "espn":
        failures.append(
            SportsLiveSampleFailure(
                code="unsupported_source",
                path="source",
                message="当前真实样本校验只支持 ESPN scoreboard。",
                expected="espn",
                actual=source,
            )
        )
    if not league:
        failures.append(
            SportsLiveSampleFailure(
                code="missing_league",
                path="league",
                message="样本必须声明 ESPN league code。",
            )
        )

    scoreboard = sample.get("scoreboard")
    if not isinstance(scoreboard, Mapping):
        failures.append(
            SportsLiveSampleFailure(
                code="missing_scoreboard",
                path="scoreboard",
                message="样本必须提供 ESPN scoreboard 原始 payload。",
            )
        )
        scoreboard = {}

    observed_at = _datetime_value(sample.get("observed_at")) or datetime.now(timezone.utc)
    events = parse_espn_scoreboard_payload(scoreboard, league=league or "nba", observed_at=observed_at)
    markets = tuple(_load_market(item) for item in _mapping_list(sample.get("markets")))
    matches = tuple(_match_payload(match) for market in markets if (match := best_live_match(market, events)))

    failures.extend(_validate_expected_events(sample, events))
    failures.extend(_validate_expected_matches(sample, markets=markets, events=events))
    return SportsLiveSampleReport(
        source=source,
        league=league,
        events_seen=len(events),
        markets_checked=len(markets),
        expected_events_checked=len(_mapping_list(sample.get("expected_events"))),
        expected_matches_checked=len(_mapping_list(sample.get("expected_matches"))),
        matches=matches,
        failures=tuple(failures),
    )


def validate_sports_live_sample_file(path: str) -> SportsLiveSampleReport:
    """从 JSON/TOML fixture 文件读取并校验真实样本。"""

    return validate_sports_live_sample(load_mapping_file(path))


def _validate_expected_events(
    sample: Mapping[str, Any],
    events: Sequence[Any],
) -> tuple[SportsLiveSampleFailure, ...]:
    failures: list[SportsLiveSampleFailure] = []
    by_event_id = {event.source_event_id: event for event in events}
    for index, expected in enumerate(_mapping_list(sample.get("expected_events"))):
        path = f"expected_events[{index}]"
        source_event_id = _text(expected.get("source_event_id"))
        if not source_event_id:
            failures.append(_failure("missing_source_event_id", path, "expected event 缺少 source_event_id。"))
            continue
        event = by_event_id.get(source_event_id)
        if event is None:
            failures.append(
                _failure(
                    "event_not_found",
                    path,
                    "ESPN payload 中没有找到期望事件。",
                    expected=source_event_id,
                    actual=tuple(by_event_id),
                )
            )
            continue
        actual = live_event_metadata(event)
        for field in (
            "status",
            "period",
            "seconds_remaining",
            "home_score",
            "away_score",
            "home_name",
            "away_name",
            "raw_status",
        ):
            if field not in expected:
                continue
            if actual.get(field) != expected.get(field):
                failures.append(
                    _failure(
                        "event_field_mismatch",
                        f"{path}.{field}",
                        "ESPN 状态字段归一化结果与样本期望不一致。",
                        expected=expected.get(field),
                        actual=actual.get(field),
                    )
                )
    return tuple(failures)


def _validate_expected_matches(
    sample: Mapping[str, Any],
    *,
    markets: Sequence[Market],
    events: Sequence[Any],
) -> tuple[SportsLiveSampleFailure, ...]:
    failures: list[SportsLiveSampleFailure] = []
    by_condition = {market.condition_id: market for market in markets}
    for index, expected in enumerate(_mapping_list(sample.get("expected_matches"))):
        path = f"expected_matches[{index}]"
        condition_id = _text(expected.get("condition_id"))
        if not condition_id:
            failures.append(_failure("missing_condition_id", path, "expected match 缺少 condition_id。"))
            continue
        market = by_condition.get(condition_id)
        if market is None:
            failures.append(
                _failure(
                    "market_not_found",
                    path,
                    "Polymarket 样本中没有找到期望 market。",
                    expected=condition_id,
                    actual=tuple(by_condition),
                )
            )
            continue
        match = best_live_match(market, tuple(events))
        if match is None:
            failures.append(_failure("match_not_found", path, "market 未匹配到任何 ESPN 事件。"))
            continue
        expected_event_id = _text(expected.get("source_event_id"))
        if expected_event_id and match.event.source_event_id != expected_event_id:
            failures.append(
                _failure(
                    "match_event_mismatch",
                    f"{path}.source_event_id",
                    "market 匹配到了非预期 ESPN event。",
                    expected=expected_event_id,
                    actual=match.event.source_event_id,
                )
            )
        min_score = _optional_int(expected.get("min_score"))
        if min_score is not None and match.score < min_score:
            failures.append(
                _failure(
                    "match_score_below_min",
                    f"{path}.min_score",
                    "market/game 匹配分低于人工要求。",
                    expected=min_score,
                    actual=match.score,
                )
            )
        for field, actual_value in (
            ("matched_home_alias", match.matched_home_alias),
            ("matched_away_alias", match.matched_away_alias),
        ):
            expected_value = _text(expected.get(field))
            if expected_value and expected_value != actual_value:
                failures.append(
                    _failure(
                        "match_alias_mismatch",
                        f"{path}.{field}",
                        "market/game 命中的队名别名与样本期望不一致。",
                        expected=expected_value,
                        actual=actual_value,
                    )
                )
    return tuple(failures)


def _load_market(item: Mapping[str, Any]) -> Market:
    """从校验 fixture 载入 Polymarket market 快照。"""

    status_text = str(item.get("trading_status") or TradingStatus.ELIGIBLE.value)
    try:
        trading_status = TradingStatus(status_text)
    except ValueError:
        trading_status = TradingStatus.CANDIDATE
    return Market(
        condition_id=_text(item.get("condition_id")) or "",
        market_slug=_text(item.get("market_slug")) or "",
        outcomes=tuple(
            MarketOutcome(
                token_id=_text(outcome.get("token_id")) or "",
                outcome=_text(outcome.get("outcome")) or "",
            )
            for outcome in _mapping_list(item.get("outcomes"))
        ),
        market_name=_text(item.get("market_name")),
        market_question=_text(item.get("market_question")),
        event_id=_text(item.get("event_id")),
        event_title=_text(item.get("event_title")),
        event_slug=_text(item.get("event_slug")),
        end_date=_datetime_value(item.get("end_date")),
        game_start_time=_datetime_value(item.get("game_start_time")),
        category=_text(item.get("category")),
        tags=tuple(str(tag) for tag in item.get("tags", ()) if tag),
        matched_keywords=tuple(str(tag) for tag in item.get("matched_keywords", ()) if tag),
        trading_status=trading_status,
    )


def _match_payload(match: Any) -> dict[str, Any]:
    return {
        "condition_id": match.market.condition_id,
        "market_slug": match.market.market_slug,
        "event_slug": match.market.event_slug,
        "source": match.event.source,
        "source_event_id": match.event.source_event_id,
        "league": match.event.league,
        "status": match.event.status.value,
        "period": match.event.period,
        "seconds_remaining": match.event.seconds_remaining,
        "score": match.score,
        "matched_home_alias": match.matched_home_alias,
        "matched_away_alias": match.matched_away_alias,
        "primary_source": match.primary_source,
        "contributing_sources": list(match.contributing_sources),
        "confidence": match.confidence,
    }


def _failure(
    code: str,
    path: str,
    message: str,
    *,
    expected: Any = None,
    actual: Any = None,
) -> SportsLiveSampleFailure:
    return SportsLiveSampleFailure(
        code=code,
        path=path,
        message=message,
        expected=expected,
        actual=actual,
    )


def _mapping_list(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：输出真实样本校验 JSON 报告。"""

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m strategies.current.live_sample_validation <fixture.json>", file=sys.stderr)
        return 2
    report = validate_sports_live_sample_file(args[0])
    print(json.dumps(report.as_payload(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口由人工运行
    raise SystemExit(main())
