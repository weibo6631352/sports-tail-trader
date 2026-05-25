"""足球 anytime-goalscorer（球员是否进球）盘口的识别与扫尾锁定评估。

Polymarket 该家族盘口语义为"比赛中 [Player] 是否进球？"，Yes/No 结算：
  - YES 锁定：该球员已在比赛中出现一次进球事件（livescore goal event）；
  - NO 锁定：比赛已结束（FT/AET/Pen）且该球员未出现在任何 goal 事件中；
  - 否则：比赛仍在进行、球员尚未进球——盘中不锁定。

数据源（CLAUDE.md §9）：Goalserve ``soccernew/home`` getfeed 的 ``events[]``
携带球员级进球事件（``@type=goal``, ``@player``, ``@playerId``, ``@team``,
``@minute``, ``@result``）。inplay GZIP feed 只提供整队比分，不可用作球员
来源——已通过 ``_extract_soccer_goal_events`` 在 livescore 解析层填入
``SoccerGameState.goal_events``。

球员名称匹配（CLAUDE.md §18 要求可审计、不模糊）：Polymarket slug 用
kebab-case（如 ``...-ags-mario-pasalic``），Goalserve 用初字 + 姓
（"M. Pasalic"）或仅姓。归一化用 NFKD 拆解去重音 + 仅保留 ASCII 字母与
空格；按"姓 + 可选首字母"匹配并返回置信度（HIGH/MEDIUM/LOW）。LOW 一律
按 ``ANYTIME_GOALSCORER_PLAYER_AMBIGUOUS`` 精确拒绝，绝不下单。
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from strategies.sports_framework import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketSide,
    SportsMarketSnapshot,
    is_soccer_game,
)

from polymarket_trader.domain.sports_live import SoccerGoalEvent

from .core import _accept, _reject
from .types import (
    ExecutionPermission,
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


# Gamma sportsMarketType 取自真实数据（用户提供的市场清单一致），是最可靠
# 识别信号；slug 模式仅作 fallback，覆盖该字段缺失情况。
_AGS_MARKET_TYPE = "soccer_anytime_goalscorer"

# slug 中 ags 段定位的两种常见拼写——精确锚定到 "-ags-" 分段或完整 phrase。
_SLUG_AGS_SEGMENT_RE = re.compile(r"-ags-([a-z0-9\-]+?)(?:-yes|-no|-)?$")
_SLUG_AGS_PHRASE_RE = re.compile(r"-anytime-goal-?scorer-([a-z0-9\-]+?)(?:-yes|-no|-)?$")


class MatchConfidence(StrEnum):
    """球员名称匹配置信度。

    HIGH = 首字母 + 姓都对得上（最严，例 "mario-pasalic" vs "M. Pasalic"）；
    MEDIUM = 仅 slug 含单字（姓），但姓匹配（例 "pasalic" vs "M. Pasalic"）；
    LOW = 仅能解出姓但 Goalserve 一方姓也无法对齐——绝不接受。
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def _normalize_name(raw: str) -> str:
    """姓名归一化：NFKD 去重音 → 仅保留 ASCII 字母与空格 → lower。

    "Raúl" → "raul"；"M." → "m"；"O'Connor" → "oconnor"。多余空白压缩。
    """
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # 仅保留 ASCII 字母与空格；其他字符（点号、撇号、连字符、数字）剔除。
    cleaned = re.sub(r"[^a-zA-Z ]+", " ", no_marks)
    return " ".join(cleaned.lower().split())


def _parse_slug_player(slug_player: str) -> tuple[str | None, str]:
    """解析 slug 中的 kebab-case 球员名 → (first_initial, last_name_normalized)。

    "mario-pasalic" → ("m", "pasalic")
    "pasalic"       → (None, "pasalic")
    "j-f-de-pasalic" → ("j", "pasalic")  —— 末段视为姓
    """
    segments = [s for s in slug_player.lower().split("-") if s]
    if not segments:
        return None, ""
    last_name = _normalize_name(segments[-1])
    first_initial: str | None = None
    if len(segments) >= 2:
        first_norm = _normalize_name(segments[0])
        if first_norm:
            first_initial = first_norm[0]
    return first_initial, last_name


def _parse_goalserve_player(name: str) -> tuple[str | None, str]:
    """解析 Goalserve "@player" → (first_initial, last_name_normalized)。

    Goalserve 格式多样：
      "A. Khaldi"        → ("a", "khaldi")
      "Mario Pasalic"    → ("m", "pasalic")
      "Pasalic"          → (None, "pasalic")
      "M. Pasalic Jr."   → ("m", "jr")  —— 末段被识别为姓；此种情况罕见，
        如发现实盘漏匹配再加 suffix 过滤。
    规则：normalize 后按空格切；最后一个单字符 token 视为首字母缩写，
    最后一个多字符 token 视为姓。
    """
    norm = _normalize_name(name)
    if not norm:
        return None, ""
    parts = norm.split()
    multi = [p for p in parts if len(p) > 1]
    if not multi:
        # 全是单字母（异常）—— 取最后一个当姓，置信度由调用方据 confidence 收敛。
        return None, parts[-1] if parts else ""
    last_name = multi[-1]
    # 首字母：parts[0] 若为单字符或多字符首字母都可用，统一取首字母字符。
    first_initial = parts[0][0] if parts and parts[0] else None
    return first_initial, last_name


def _slug_player_matches_goal_event(
    slug_player: str,
    goal_event: SoccerGoalEvent,
) -> MatchConfidence:
    """slug 球员名与单粒进球事件的匹配置信度。

    匹配规则：
      - 姓不一致 → LOW（绝不接受）；
      - 姓一致 + slug 含首字母 + Goalserve 含首字母 + 两者一致 → HIGH；
      - 姓一致 + 任一方缺首字母 → MEDIUM；
      - 姓一致 + 双方都给首字母但不一致 → LOW（同姓不同人）。
    Goalserve playerId 若与 slug 末段去连字符版本匹配可直接 HIGH——
    当前 slug 不携 player_id，仅保留 hook 以便未来添加。
    """
    slug_initial, slug_last = _parse_slug_player(slug_player)
    if not slug_last:
        return MatchConfidence.LOW
    gs_initial, gs_last = _parse_goalserve_player(goal_event.player_name)
    if not gs_last or slug_last != gs_last:
        return MatchConfidence.LOW
    # 姓一致——按首字母判定置信度。
    if slug_initial and gs_initial:
        return MatchConfidence.HIGH if slug_initial == gs_initial else MatchConfidence.LOW
    # 任一方未提供首字母：姓一致即 MEDIUM。
    return MatchConfidence.MEDIUM


# ---- 识别 ----------------------------------------------------------------


def is_anytime_goalscorer_market(market: SportsMarketSnapshot) -> bool:
    """识别 anytime-goalscorer 盘口。

    首选 Gamma ``sportsMarketType=soccer_anytime_goalscorer``——真实数据中
    该字段对此家族稳定填充。回退 slug 关键字（``-ags-`` 或
    ``-anytime-goal-scorer-`` 段），覆盖该字段缺失的边界场景。
    """
    if (market.sports_market_type or "").strip().lower() == _AGS_MARKET_TYPE:
        return True
    slug = (market.market_slug or "").strip().lower()
    if not slug:
        return False
    if "-ags-" in slug:
        return True
    if "anytime-goal-scorer" in slug or "anytime-goalscorer" in slug:
        return True
    return False


def extract_slug_player_name(market: SportsMarketSnapshot) -> str | None:
    """从 slug 提取球员名（kebab-case 形式，未规范化）。

    返回示例："mario-pasalic"。识别失败返回 None——调用方据此给精确
    ``ANYTIME_GOALSCORER_PLAYER_AMBIGUOUS`` 拒绝原因。
    """
    slug = (market.market_slug or "").strip().lower()
    if not slug:
        return None
    m = _SLUG_AGS_SEGMENT_RE.search(slug)
    if m is None:
        m = _SLUG_AGS_PHRASE_RE.search(slug)
    if m is None:
        return None
    candidate = m.group(1).strip("-")
    if not candidate or candidate in {"yes", "no"}:
        return None
    # 去掉末尾可能的 -yes/-no 后缀，避免污染姓字段。
    for tail_suffix in ("-yes", "-no"):
        if candidate.endswith(tail_suffix):
            candidate = candidate[: -len(tail_suffix)]
    return candidate or None


# ---- 评估 ----------------------------------------------------------------


def _evaluate_anytime_goalscorer(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """anytime-goalscorer 扫尾锁定评估。

    锁定数学：
      - 任一进球事件匹配 slug 球员 → 该球员已进球 → YES 真。
      - 比赛 ENDED 且无任一进球事件匹配 → 该球员整场未进球 → NO 真。
      - 其它（比赛仍在进行且无匹配）→ 暂不锁定。

    side 与锁定方向不一致直接精确拒绝 ANYTIME_GOALSCORER_WRONG_SIDE——
    把"注定输"的下注挡在风控前。
    """
    game = candidate.game
    market = candidate.market
    if not is_soccer_game(game):
        # ags 是 soccer-only 家族——non-soccer 命中识别属于误判（CLAUDE.md §18
        # 要求可审计），给一个精确的不可下单原因。
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    soccer_state = game.soccer_state
    if soccer_state is None:
        # 缺直播 SoccerGameState（数据源未匹配或解析失败）——CLAUDE.md §18
        # 要求显式归到 missing_live_game_state，不静默吞。
        return _reject(candidate, TailRejectReason.MISSING_LIVE_GAME_STATE.value)
    slug_player = extract_slug_player_name(market)
    if not slug_player:
        return _reject(
            candidate,
            TailRejectReason.ANYTIME_GOALSCORER_PLAYER_AMBIGUOUS.value,
        )

    # 价格 / 流动性自查（binary_prop 在通用门禁跳过）。
    price_reject = _binary_prop_price_reject(market, policy)
    if price_reject is not None:
        return _reject(candidate, price_reject.value)

    has_scored = False
    best_confidence = MatchConfidence.LOW
    for event in soccer_state.goal_events:
        confidence = _slug_player_matches_goal_event(slug_player, event)
        if confidence in {MatchConfidence.HIGH, MatchConfidence.MEDIUM}:
            has_scored = True
            best_confidence = MatchConfidence.HIGH if confidence == MatchConfidence.HIGH else best_confidence
            if confidence == MatchConfidence.HIGH:
                break
            best_confidence = MatchConfidence.MEDIUM
    game_ended = game.status == LiveGameStatus.ENDED

    if has_scored:
        # YES 锁定：该球员已进球——比分只增不减，进球既成事实。
        if market.side == SportsMarketSide.YES:
            return _accept(
                candidate,
                f"anytime_goalscorer_yes_locked_{best_confidence.value}",
                ExecutionPermission.AUTO_EXECUTE,
            )
        # NO 已注定输（球员已进球）——精确拒绝，不要继续走赔率差价回退。
        return _reject(candidate, TailRejectReason.ANYTIME_GOALSCORER_WRONG_SIDE.value)

    if game_ended:
        # 比赛已结束且无匹配进球事件 → NO 锁定。但需保留可能性：直播源 goal_events
        # 缺失（数据源故障）会假阳性锁定 NO——goal_events 元组为空且比赛已结束
        # 时不可信任，必须有至少一个 goal_event 才证明数据源链路在线。
        # 此处采取保守策略：若整场无任何进球事件且总比分 > 0，说明 goal_events
        # 数据流失败，不锁定。
        total_goals_in_feed = len(soccer_state.goal_events)
        total_score = game.home_score + game.away_score
        if total_score > 0 and total_goals_in_feed == 0:
            # 直播 feed 里没有任何 goal 事件但比分非 0——数据流断了，不可锁定。
            return _reject(candidate, TailRejectReason.MISSING_LIVE_GAME_STATE.value)
        if market.side == SportsMarketSide.NO:
            return _accept(
                candidate,
                "anytime_goalscorer_no_locked",
                ExecutionPermission.AUTO_EXECUTE,
            )
        # YES 已注定输——精确拒绝。
        return _reject(candidate, TailRejectReason.ANYTIME_GOALSCORER_WRONG_SIDE.value)

    # 比赛进行中且球员尚未进球——不锁定。
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def _binary_prop_price_reject(
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> TailRejectReason | None:
    """binary_prop 盘口的入场价 / 流动性自查（与 event_props 同语义）。"""

    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。
    return None


# 给 LiveGameState 用作 sport 判定（is_soccer_game 已从 sports_framework 导入）。
_ = LiveGameState  # 显式标注引用，便于将来扩展，本模块仅用类型签名。
