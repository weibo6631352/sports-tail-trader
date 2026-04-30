"""当前策略的体育盘口 outcome 解析。

体育扫尾策略不再假设固定交易 ``NO`` token，而是按盘口类型解析目标方向。
解析失败时返回显式原因，由 universe、trading 和 recovery 决定是否跳过或暂停。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from polymarket_trader.domain.market import Market
from strategies.current.sports_tail import SportsMarketFamily, SportsMarketSide, SportsMarketType

_GENERIC_OUTCOMES = {"yes", "no"}
_LINE_MARKER_PATTERN = r"(?:over|under|total(?:[\s:_/-]+games)?|spread|handicap)"


@dataclass(frozen=True, slots=True)
class SportsTokenTarget:
    """一个可由体育扫尾策略管理的 token 方向。"""

    token_id: str
    side: SportsMarketSide
    label: str


@dataclass(frozen=True, slots=True)
class SportsMarketDescriptor:
    """market 文本和 outcomes 解析后的体育盘口描述。"""

    accepted: bool
    reason: str
    market_family: SportsMarketFamily = SportsMarketFamily.UNSUPPORTED
    market_type: SportsMarketType | None = None
    line: Decimal | None = None
    targets: tuple[SportsTokenTarget, ...] = ()

    @property
    def target_token_ids(self) -> tuple[str, ...]:
        return tuple(target.token_id for target in self.targets)


def primary_token_id(market: Market) -> str:
    """返回第一个可管理 token，保留给旧调用侧使用。

    新代码应优先使用 ``sports_token_targets()``，避免重新引入“固定主 token”假设。
    """

    descriptor = describe_sports_market(market)
    if not descriptor.targets:
        raise ValueError(descriptor.reason or "missing_sports_target")
    return descriptor.targets[0].token_id


def is_primary_token(market: Market, token_id: str | None) -> bool:
    if token_id is None:
        return False
    return token_id in describe_sports_market(market).target_token_ids


def sports_token_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    """返回体育扫尾可管理的 token 方向。"""

    return describe_sports_market(market).targets


def describe_sports_market(market: Market) -> SportsMarketDescriptor:
    """从 market 文本和 outcomes 中解析体育盘口类型、盘口线和方向。"""

    text = _market_text(market)
    market_family = _market_family(market, text)
    market_type = _market_type(market, text)
    if market_type is None:
        return SportsMarketDescriptor(accepted=False, reason="unsupported_sports_market_type")

    line = _market_line(text) if market_type in {SportsMarketType.TOTALS, SportsMarketType.SPREADS} else None
    if market_type in {SportsMarketType.TOTALS, SportsMarketType.SPREADS} and line is None:
        return SportsMarketDescriptor(accepted=False, reason="missing_market_line")

    targets = _token_targets(market, market_type)
    if not targets:
        return SportsMarketDescriptor(accepted=False, reason="missing_target_token")
    return SportsMarketDescriptor(
        accepted=True,
        reason=_market_family_reason(market_family),
        market_family=market_family,
        market_type=market_type,
        line=line,
        targets=targets,
    )


def target_for_token(market: Market, token_id: str | None) -> SportsTokenTarget | None:
    """返回 token 对应的体育盘口方向。"""

    if token_id is None:
        return None
    for target in sports_token_targets(market):
        if target.token_id == token_id:
            return target
    return None


def _market_type(market: Market, text: str) -> SportsMarketType | None:
    outcome_tokens = {_normalize_text(outcome.outcome) for outcome in market.outcomes}
    if {"over", "under"} & outcome_tokens or "total" in text or "overunder" in text:
        return SportsMarketType.TOTALS
    if "spread" in text or "handicap" in text or _has_signed_number(text):
        return SportsMarketType.SPREADS
    if _is_binary_yes_no_market(market):
        return SportsMarketType.BINARY_PROP
    if len(market.outcomes) >= 2:
        return SportsMarketType.MONEYLINE
    return None


def _market_family(market: Market, text: str) -> SportsMarketFamily:
    """按结算对象划分市场，避免把系列赛或冠军归属误当单场盘口。"""

    tag_text = _normalize_text(" ".join(_non_empty(market.category, *market.tags)))
    combined_text = f"{text} {tag_text}".strip()
    if _contains_any(
        combined_text,
        (
            "esports",
            "e sports",
            "honor of kings",
            "league of legends",
            "dota",
            "counter strike",
            "bo3",
            "bo5",
        ),
    ):
        return SportsMarketFamily.ESPORTS
    series_phrases = (
        "who will win series",
        "win series",
        "series winner",
        "games o u",
        "games ou",
        "game handicap",
    )
    if (_contains_any(text, series_phrases) and not _is_tennis_text(combined_text)) or (
        " total games " in f" {text} " and not _is_tennis_text(combined_text)
    ):
        return SportsMarketFamily.SERIES
    if _contains_any(
        text,
        (
            "championship winner",
            "cup winner",
            "winner",
            "stanley cup winner",
            "tournament winner",
            "winner nation",
            "to win the",
            "will win the",
        ),
    ) and not _has_matchup_marker(text):
        return SportsMarketFamily.OUTRIGHT
    if _is_binary_yes_no_market(market) and _is_season_or_competition_prop(combined_text):
        return SportsMarketFamily.OUTRIGHT
    return SportsMarketFamily.SINGLE_GAME


def _market_family_reason(market_family: SportsMarketFamily) -> str:
    if market_family == SportsMarketFamily.SINGLE_GAME:
        return "sports_market_selected"
    if market_family == SportsMarketFamily.SERIES:
        return "series_market_not_auto_tradable"
    if market_family == SportsMarketFamily.OUTRIGHT:
        return "outright_market_not_auto_tradable"
    if market_family == SportsMarketFamily.ESPORTS:
        return "esports_market_not_auto_tradable"
    return "unsupported_market_family"


def _token_targets(
    market: Market,
    market_type: SportsMarketType,
) -> tuple[SportsTokenTarget, ...]:
    if market_type == SportsMarketType.TOTALS:
        return _totals_targets(market)
    if market_type == SportsMarketType.BINARY_PROP:
        return _binary_targets(market)
    return _side_targets(market)


def _totals_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    targets: list[SportsTokenTarget] = []
    for outcome in market.outcomes:
        normalized = _normalize_text(outcome.outcome)
        if "over" in normalized:
            targets.append(
                SportsTokenTarget(
                    token_id=outcome.token_id,
                    side=SportsMarketSide.OVER,
                    label=outcome.outcome,
                )
            )
        elif "under" in normalized:
            targets.append(
                SportsTokenTarget(
                    token_id=outcome.token_id,
                    side=SportsMarketSide.UNDER,
                    label=outcome.outcome,
                )
            )
    return tuple(targets)


def _side_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    non_generic = [
        outcome for outcome in market.outcomes if _normalize_text(outcome.outcome) not in _GENERIC_OUTCOMES
    ]
    if len(non_generic) < 2:
        return ()
    return (
        SportsTokenTarget(
            token_id=non_generic[0].token_id,
            side=SportsMarketSide.HOME,
            label=non_generic[0].outcome,
        ),
        SportsTokenTarget(
            token_id=non_generic[1].token_id,
            side=SportsMarketSide.AWAY,
            label=non_generic[1].outcome,
        ),
    )


def _binary_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    """解析 Polymarket 真实 Yes/No 体育 prop 的方向。"""

    targets: list[SportsTokenTarget] = []
    for outcome in market.outcomes:
        normalized = _normalize_text(outcome.outcome)
        if normalized == "yes":
            targets.append(
                SportsTokenTarget(
                    token_id=outcome.token_id,
                    side=SportsMarketSide.YES,
                    label=outcome.outcome,
                )
            )
        elif normalized == "no":
            targets.append(
                SportsTokenTarget(
                    token_id=outcome.token_id,
                    side=SportsMarketSide.NO,
                    label=outcome.outcome,
                )
            )
    return tuple(targets)


def _market_line(text: str) -> Decimal | None:
    point_decimal = re.search(
        r"(?:spread|handicap)[\s:_/-]*(?:minus|negative)[\s:_/-]*(\d+)pt(\d+)(?![a-z0-9])",
        text,
    )
    if point_decimal is not None:
        try:
            return Decimal(f"-{point_decimal.group(1)}.{point_decimal.group(2)}")
        except InvalidOperation:
            return None

    point_decimal = re.search(
        rf"{_LINE_MARKER_PATTERN}[\s:_/-]*([-+]?\d+)pt(\d+)(?![a-z0-9])",
        text,
    )
    if point_decimal is not None:
        try:
            return Decimal(f"{point_decimal.group(1)}.{point_decimal.group(2)}")
        except InvalidOperation:
            return None

    hyphen_decimal = re.search(
        r"(?:spread|handicap)[\s:_/-]*(?:minus|negative)[\s:_/-]*(\d+)-(\d+)",
        text,
    )
    if hyphen_decimal is not None:
        try:
            return Decimal(f"-{hyphen_decimal.group(1)}.{hyphen_decimal.group(2)}")
        except InvalidOperation:
            return None

    hyphen_decimal = re.search(
        rf"{_LINE_MARKER_PATTERN}[\s:_/-]*([-+]?\d+)-(\d+)",
        text,
    )
    if hyphen_decimal is not None:
        try:
            return Decimal(f"{hyphen_decimal.group(1)}.{hyphen_decimal.group(2)}")
        except InvalidOperation:
            return None

    marker_decimal = re.search(
        rf"{_LINE_MARKER_PATTERN}[\s:_/-]*([-+]?\d+(?:\.\d+)?)(?![a-z0-9])",
        text,
    )
    if marker_decimal is not None:
        try:
            return Decimal(marker_decimal.group(1))
        except InvalidOperation:
            return None

    match = re.search(r"(?<![a-z0-9])[-+]?\d+(?:\.\d+)?(?![a-z0-9])", text)
    if match is None:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _has_signed_number(text: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])[-+]\d+(?:\.\d+)?(?![a-z0-9])", text))


def _has_matchup_marker(text: str) -> bool:
    return bool(re.search(r"(?:^|\s)(?:vs|v|at)(?:\s|$)", text))


def _is_tennis_text(text: str) -> bool:
    return _contains_any(text, ("tennis", "atp", "wta"))


def _is_binary_yes_no_market(market: Market) -> bool:
    outcome_tokens = {_normalize_text(outcome.outcome) for outcome in market.outcomes}
    return {"yes", "no"} <= outcome_tokens


def _is_season_or_competition_prop(text: str) -> bool:
    """识别真实 Gamma 样本里的赛季/赛事归属型 Yes/No 市场。

    这些市场可以解析方向，但不属于单场直播扫尾，不进入自动交易 universe。
    """

    strong_competition_phrases = (
        "draft",
        "drafted",
        "overall pick",
        "award",
        "awards",
        "mvp",
        "player of the year",
        "defender of the year",
        "rookie of the year",
        "manager of the year",
        "coach of the year",
        "comeback player",
        "defensive player",
        "cba",
        "collective bargaining",
        "scorigami",
        "season",
        "champion",
        "championship",
        "advance to",
        "conference semifinals",
        "conference semi finals",
        "conference finals",
        "nba playoffs",
        "nhl playoffs",
        "mlb playoffs",
        "postseason",
        "world cup",
        "wimbledon",
        "champions league",
        "europa league",
        "conference league",
        "premier league",
        "la liga",
        "bundesliga",
        "serie a",
        "ligue 1",
        "ucl",
        "uel",
        "uecl",
        "most goals",
        "most assists",
        "most cards",
        "most yellow cards",
        "most red cards",
        "most goal contributions",
        "promoted to",
        "name stadium",
        "stadium after",
        "start week 1",
        "starting qb",
        "rostered by",
        "grand slams",
        "confirmed relationship",
        "out as",
        "leave illinois",
        "buy the",
        "who will buy",
        "purchase the",
        "acquire the",
    )
    if _contains_any(text, strong_competition_phrases):
        return True

    matchup_sensitive_phrases = (
        "top goal scorer",
        "top goalscorer",
        "golden boot",
        "winner",
        "next team",
        "play for",
        "sign with",
        "to leave",
        "traded to",
        "be traded",
    )
    return _contains_any(
        text,
        matchup_sensitive_phrases,
    ) and not _has_matchup_marker(text)


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {text} "
    hyphen_normalized = f" {text.replace('-', ' ')} "
    return any(f" {phrase} " in padded or f" {phrase} " in hyphen_normalized for phrase in phrases)


def _non_empty(*values: str | None) -> tuple[str, ...]:
    return tuple(value for value in values if value)


def _market_text(market: Market) -> str:
    return _normalize_text(
        " ".join(
            part
            for part in (
                market.market_question,
                market.market_name,
                market.market_slug,
                market.event_title,
            )
            if part
        )
    )


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    normalized = text.lower().replace("&", " and ")
    parts: list[str] = []
    current: list[str] = []
    for char in normalized:
        if char.isalnum() or char in {"+", "-", "."}:
            current.append(char)
            continue
        if current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    return " ".join(parts)
