"""当前量化体育盘口 outcome 解析。

量化决策不再假设固定交易 ``NO`` token，而是按盘口类型解析目标方向。
解析失败时返回显式原因，由 universe、trading 和 recovery 决定是否跳过或暂停。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import re

from polymarket_trader.domain.market import Market
from polymarket_trader.sports import SportsMarketFamily, SportsMarketSide, SportsMarketType

_GENERIC_OUTCOMES = {"yes", "no"}
# 盘口线标记词。"totals" 复数与 "handicap-home-1" 这类在标记词和数字之间夹
# home/away 的 slug 都要覆盖，否则标记匹配落空后会退化到通用兜底，把 slug 里的
# 年份（如 2026）误当成盘口线。
_LINE_MARKER_PATTERN = (
    r"(?:over|under|totals?(?:[\s:_/-]+games)?|spread|handicap)"
    r"(?:[\s:_/-]+(?:home|away))?"
)

@dataclass(frozen=True, slots=True)
class SportsTokenTarget:
    """一个可由量化决策管理的 token 方向。"""

    token_id: str
    side: SportsMarketSide
    label: str
    # 单场 Yes/No 胜负盘专用：matching 用 ``label``（问句里点名的球队名）解析出
    # 该球队对应直播源的 HOME/AWAY 后，``invert_side=True`` 的 token（"No" 方向）
    # 需要再翻转一次，因为它结算的是"点名球队不获胜"即对手获胜。
    invert_side: bool = False


@dataclass(frozen=True, slots=True)
class SportsMarketDescriptor:
    """market 文本和 outcomes 解析后的体育盘口描述。"""

    accepted: bool
    reason: str
    market_family: SportsMarketFamily = SportsMarketFamily.UNSUPPORTED
    market_type: SportsMarketType | None = None
    line: Decimal | None = None
    targets: tuple[SportsTokenTarget, ...] = ()
    # 透传 Polymarket Gamma 的 sportsMarketType，供评估器精确识别运动专属 prop 家族。
    sports_market_type: str | None = None

    @property
    def target_token_ids(self) -> tuple[str, ...]:
        return tuple(target.token_id for target in self.targets)


def primary_token_id(market: Market) -> str:
    """返回第一个可管理 token，保留给旧调用侧使用。

    新代码应优先使用 ``sports_token_targets()``，避免重新引入“固定主 token”假设。
    """

    descriptor = describe_sports_market(market)
    if not descriptor.targets:
        raise ValueError(descriptor.reason or "missing_target")
    return descriptor.targets[0].token_id


def sports_token_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    """返回量化可管理的 token 方向。"""

    return describe_sports_market(market).targets


@lru_cache(maxsize=2048)
def describe_sports_market(market: Market) -> SportsMarketDescriptor:
    """从 market 文本和 outcomes 中解析体育盘口类型、盘口线和方向。"""

    text = _market_text(market)
    market_family = _market_family(market, text)
    market_type = _market_type(market, text, market_family)
    if market_type is None:
        return SportsMarketDescriptor(accepted=False, reason="unsupported_market_type")

    _line_required_types = {SportsMarketType.TOTALS, SportsMarketType.SPREADS}
    line = _market_line(text) if market_type in _line_required_types else None
    if market_type in _line_required_types and line is None:
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
        sports_market_type=market.sports_market_type,
    )


def target_for_token(market: Market, token_id: str | None) -> SportsTokenTarget | None:
    """返回 token 对应的体育盘口方向。"""

    if token_id is None:
        return None
    for target in sports_token_targets(market):
        if target.token_id == token_id:
            return target
    return None


def _market_type(
    market: Market,
    text: str,
    market_family: SportsMarketFamily,
) -> SportsMarketType | None:
    outcome_tokens = {_normalize_text(outcome.outcome) for outcome in market.outcomes}
    # outcome_tokens 含纯 "over"/"under" 说明是数值嵌在 slug/question 中的常规 totals；
    # outcome_tokens 含 "over 25.5" 等带数值的标签，"over"/"under" 也出现在 text 中。
    has_over_under_outcomes = bool({"over", "under"} & outcome_tokens) or any(
        t.startswith("over ") or t.startswith("under ") for t in outcome_tokens
    )
    if has_over_under_outcomes or "total" in text or "overunder" in text:
        return SportsMarketType.TOTALS
    if "spread" in text or "handicap" in text or _has_signed_number(text):
        return SportsMarketType.SPREADS
    if _is_binary_yes_no_market(market):
        # 单场 Yes/No 胜负盘（"Will the Lakers win the game?" + Yes/No）当作
        # MONEYLINE 处理：路由到现成的锁定 + 赔率差价 moneyline 评估器。
        # 其余 Yes/No prop（BTTS、首球、半场赛果、NRFI 等）仍归 BINARY_PROP。
        if _is_single_game_yes_no_moneyline(market, text, market_family):
            return SportsMarketType.MONEYLINE
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
        # esports 双方对阵胜负盘走 single_game MONEYLINE：有 livescore 直播源 +
        # best-of 锁定模型，可自动交易。判定条件——含 vs/at 对阵标记、恰 2 个
        # outcome 且 outcome 是战队名（非 Yes/No、非 Over/Under）。其余 esports
        # 盘口（total games、odd/even kills、rampage、penta kill 等 prop）无定价
        # 模型，仍归 ESPORTS family 做可审计 record-only（§9 不静默丢弃）。
        if _is_esports_moneyline_market(market, combined_text):
            return SportsMarketFamily.SINGLE_GAME
        return SportsMarketFamily.ESPORTS
    # tennis "total games" 是单场 totals 盘口，不是系列赛——先排除。
    # series 关键词内联识别：系列赛胜者 / best-of / total games / handicap 子类型
    # 都归 SERIES family，由 quant_decider 主路径统一通过 estimate_signal 取信号。
    if not _is_tennis_text(combined_text):
        if _contains_any(
            combined_text,
            (
                " series winner",
                "win the series",
                "win this series",
                "wins the series",
                "to win the series",
                " best of ",
                " best-of-",
                "series total games",
                "series handicap",
            ),
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
    if _is_season_or_competition_prop(combined_text) and not _has_matchup_marker(text):
        return SportsMarketFamily.OUTRIGHT
    if _is_binary_yes_no_market(market) and _is_season_or_competition_prop(combined_text):
        return SportsMarketFamily.OUTRIGHT
    return SportsMarketFamily.SINGLE_GAME


def _market_family_reason(market_family: SportsMarketFamily) -> str:
    if market_family == SportsMarketFamily.SINGLE_GAME:
        return "market_selected"
    if market_family == SportsMarketFamily.SERIES:
        # series 已删。本字段是
        # universe 排除的展示原因（family 不在 single_game / outright 白名单时使用），
        # 真正的拒绝原因走 evaluator 的 ``SeriesRejectReason``。
        return "series_market_pending_model"
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
    # 单场 Yes/No 胜负盘已被 _market_type 判为 MONEYLINE，但 outcome 仍是 Yes/No，
    # 走专用 target 构造（把问句点名球队塞进 label 供 matching 解析 HOME/AWAY）。
    if market_type == SportsMarketType.MONEYLINE and _is_binary_yes_no_market(market):
        return _yes_no_moneyline_targets(market)
    return _side_targets(market)


def _totals_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    targets: list[SportsTokenTarget] = []
    for outcome in market.outcomes:
        normalized = _normalize_text(outcome.outcome)
        # Polymarket 把 totals outcome 写成 "Over 86.5" 或缩写 "O 86.5"——
        # 单字母 "O"/"U" 在线赛季 win totals 等长盘上很常见，必须识别，
        # 否则 _token_targets 返回空、整条市场以 missing_target_token 静默丢弃。
        first_token = normalized.split(" ", 1)[0] if normalized else ""
        if "over" in normalized or first_token == "o":
            targets.append(
                SportsTokenTarget(
                    token_id=outcome.token_id,
                    side=SportsMarketSide.OVER,
                    label=outcome.outcome,
                )
            )
        elif "under" in normalized or first_token == "u":
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


def _yes_no_moneyline_targets(market: Market) -> tuple[SportsTokenTarget, ...]:
    """构造单场 Yes/No 胜负盘的方向 target。

    单场 Yes/No 胜负盘的 outcome 文案是 "Yes"/"No"，不含球队名，无法直接交给
    matching 做 HOME/AWAY 别名匹配。解法：把问句里点名的球队名写进两个 token
    的 ``label``——"Yes" token 结算的是"点名球队获胜"，"No" token 结算的是
    "点名球队不获胜"即对手获胜。matching 先用 label 解出点名球队对应直播源的
    HOME 还是 AWAY，再对 ``invert_side=True`` 的 "No" token 翻转一次方向。
    side 先填占位 HOME/AWAY，真正方向由 matching 按直播源主客队修正。
    """

    text = _market_text(market)
    named_team = _yes_no_moneyline_named_team(text)
    if named_team is None:
        return ()
    yes_outcome = None
    no_outcome = None
    for outcome in market.outcomes:
        normalized = _normalize_text(outcome.outcome)
        if normalized == "yes":
            yes_outcome = outcome
        elif normalized == "no":
            no_outcome = outcome
    if yes_outcome is None or no_outcome is None:
        return ()
    return (
        SportsTokenTarget(
            token_id=yes_outcome.token_id,
            side=SportsMarketSide.HOME,
            label=named_team,
            invert_side=False,
        ),
        SportsTokenTarget(
            token_id=no_outcome.token_id,
            side=SportsMarketSide.AWAY,
            label=named_team,
            invert_side=True,
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

    # 兜底匹配无标记词的盘口线前，先剥除 slug 里的 YYYY-MM-DD 日期串，
    # 否则日期中的年/月/日会被当成盘口线（历史 bug：line 误取 2026 / 05）。
    # 同时限制整数部分为 1-3 位，真实盘口不会是 4 位数。
    text_without_date = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    match = re.search(r"(?<![a-z0-9])[-+]?\d{1,3}(?:\.\d+)?(?![a-z0-9])", text_without_date)
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


def _is_esports_moneyline_market(market: Market, combined_text: str) -> bool:
    """识别 esports 双方对阵胜负盘（best-of-N 系列赛由哪支战队胜出）。

    只在已确认含 esports 关键词后调用。判据：恰 2 个 outcome，且 outcome 既非
    Yes/No 也非 Over/Under（即两个战队名）；并要求文本含 vs/at 对阵标记，排除
    赛季归属型 esports 盘口。total games / props 等不满足"2 个战队名 outcome"，
    自然落到 ESPORTS family。
    """
    if len(market.outcomes) != 2:
        return False
    outcome_tokens = {_normalize_text(o.outcome) for o in market.outcomes}
    if {"yes", "no"} & outcome_tokens:
        return False
    if {"over", "under"} & outcome_tokens or any(
        t.startswith("over ") or t.startswith("under ") for t in outcome_tokens
    ):
        return False
    return _has_matchup_marker(combined_text)


# 单场胜负语义词：问句问的是"赢下这一场比赛"。
_SINGLE_GAME_WIN_PHRASES = (
    "win the game",
    "win the match",
    "win this game",
    "win this match",
    "win their game",
    "win their match",
    "win game",
    "win match",
)
# outright（冠军/赛季/系列赛归属）语义词：命中即排除，不是单场胜负盘。
_OUTRIGHT_EXCLUSION_PHRASES = (
    "championship",
    "champion",
    "title",
    "cup",
    "final",
    "finals",
    "series",
    "division",
    "conference",
    "the season",
    "trophy",
    "playoff",
    "playoffs",
    "postseason",
    "pennant",
    "promotion",
    "promoted",
    "relegated",
    "relegation",
    "grand slam",
    "world cup",
)
# 单场 Yes/No 胜负盘支持的运动：2-way 无平局胜负语义。足球 3-way 1X2 由
# ``core._evaluate_soccer_moneyline`` 经独立 slug 路径处理，这里显式排除。
_YES_NO_MONEYLINE_SPORT_KEYWORDS = (
    "basketball",
    "nba",
    "wnba",
    "ncaab",
    "euroleague",
    "hockey",
    "nhl",
    "baseball",
    "mlb",
    "kbo",
    "npb",
    "tennis",
    "atp",
    "wta",
    "esports",
    "e sports",
    "honor of kings",
    "league of legends",
    "dota",
    "counter strike",
    "valorant",
    "amfootball",
    "nfl",
    "ncaaf",
    "football",
)
_SOCCER_TEXT_KEYWORDS = (
    "soccer",
    "premier league",
    "la liga",
    "bundesliga",
    "serie a",
    "ligue 1",
    "mls",
    "eredivisie",
    "j league",
    "champions league",
    "europa league",
)


def _yes_no_moneyline_named_team(text: str) -> str | None:
    """从 "Will [the] <球队> win/beat ..." 问句里提取被点名的球队名。

    返回归一化后的球队名文本；matching 会用它和直播源主客队别名比对，解析
    出该球队对应 HOME 还是 AWAY。提取不到（无 "will ... win/beat" 结构）返回
    None——结构不清的 Yes/No 市场不归为单场胜负盘。
    """

    # "win the game" 等短语会被一并捕获，随后剥除，只留球队名。
    match = re.search(r"\bwill\s+(?:the\s+)?(.+?)\s+(win|beat|defeat|defeats|wins|beats)\b", text)
    if match is None:
        return None
    team = match.group(1).strip()
    # 剥除问句里夹带的对手从句尾缀（"beat the heat" → 取 "beat" 前的球队名即可）。
    if not team:
        return None
    return team


def _is_single_game_yes_no_moneyline(
    market: Market,
    text: str,
    market_family: SportsMarketFamily,
) -> bool:
    """识别单场 Yes/No 胜负盘（"Will <球队> win the game?" + Yes/No）。

    必须全部满足：① family 为 SINGLE_GAME（非系列赛/冠军归属）；② 恰 2 个
    Yes/No outcome；③ 问句点名了对阵双方之一并问"赢下这一场"（win the game /
    beat / win vs 等）；④ 运动属于 2-way 无平局胜负语境，且非足球（足球 3-way
    1X2 走 core 独立路径，不能被改判）。
    """

    if market_family != SportsMarketFamily.SINGLE_GAME:
        return False
    if not _is_binary_yes_no_market(market):
        return False
    # 足球单场胜负是 3-way（含平局），由 core 的 soccer moneyline 路径处理；
    # 这里一律不接管足球，避免破坏既有 3-way 评估。
    if _contains_any(text, _SOCCER_TEXT_KEYWORDS):
        return False
    if not _contains_any(text, _YES_NO_MONEYLINE_SPORT_KEYWORDS) and not _has_matchup_marker(text):
        return False
    # outright 语义词命中即排除——冠军/赛季/系列赛归属不是单场胜负盘。
    if _contains_any(text, _OUTRIGHT_EXCLUSION_PHRASES):
        return False
    named_team = _yes_no_moneyline_named_team(text)
    if named_team is None:
        return False
    # 问句必须明确是"赢下这一场比赛"：含单场胜负短语，或含 beat/defeat（击败
    # 对手即赢下当场），或含 vs/at 对阵标记。仅 "win" 不足以判定为单场。
    has_single_game_phrase = _contains_any(text, _SINGLE_GAME_WIN_PHRASES)
    has_beat_verb = _contains_any(text, ("beat", "beats", "defeat", "defeats"))
    has_vs_against = _has_matchup_marker(text) or _contains_any(text, ("against",))
    return has_single_game_phrase or has_beat_verb or has_vs_against


def _is_season_or_competition_prop(text: str) -> bool:
    """识别真实 Gamma 样本里的赛季/赛事归属型 Yes/No 市场。

    这些市场可以解析方向，但不属于单场直播，不进入自动交易 universe。
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
        "us open",
        "french open",
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
        "homer game",
        "promoted to",
        "name stadium",
        "stadium after",
        "start week 1",
        "starting qb",
        "rostered by",
        "play in",
        "grand slam",
        "grand slams",
        "confirmed relationship",
        "out as",
        "leave illinois",
        "relocation",
        "announce relocation",
        "buy the",
        "who will buy",
        "purchase the",
        "acquire the",
        "appointed as manager",
        "announce his retirement",
        "announce her retirement",
        "announce retirement",
    )
    if _contains_any(text, strong_competition_phrases):
        return True
    if _is_next_role_market(text):
        return True

    matchup_sensitive_phrases = (
        "top goal scorer",
        "top goalscorer",
        "golden boot",
        "winner",
        "next team",
        "play for",
        "join the",
        "join team",
        "join someone else",
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


def _is_next_role_market(text: str) -> bool:
    """识别下一任教练/经理等长期职位归属市场。"""

    normalized = f" {text.replace('-', ' ')} "
    if " be the next " not in normalized:
        return False
    return _contains_any(text, ("manager", "coach", "head coach"))


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
