"""Market → sport 单一权威推断。

# 为什么独立成模块

历史上"slug → sport"硬编码出现在 3 处（runtime.py 健康面板列表、live_state.py
`_market_sport_codes` 表、subscription_policy 注入函数），且各自实现略有不同：
runtime 用前缀匹配，live_state 用 token 子串匹配。性能分析师 R5 诊断发现 mlbb
（Mobile Legends Bang Bang）只出现在 runtime 列表却没进 live_state token 表，
导致 7 个 mlbb 市场全部 `sport=None` → SportResolver 返回空 → subscription_policy
不订任何 live source → entry_metadata 永远为 None → ws_loops gate 永远不通过。

把所有 sport 推断收口到本模块单一权威表，新增运动只改这里。

# 推断优先级（高 → 低）

1. `market.tags` 中找运动关键词（gamma 显式标签最可信）
2. `market.sports_market_type`（gamma sportsMarketType 字段，prop 家族可靠）
3. `market.category`（事件大类）
4. `market.market_slug` 关键词命中（slug 前缀 / 中段都覆盖，因为 slug 经规范化后
   `cs2-foo-bar` 拆成 `cs2 foo bar`，`cs2` 作为独立 token 仍能 hit）

任一层命中即返回。

# 返回值

`resolve_sport_from_market` 返回**workflow 规范运动码**（与
`INPLAY_COVERED_SPORTS` / `SPORT_CODE_TO_INPLAY_KEYS` 对齐：baseball / basketball /
tennis / football / ice-hockey / american-football / esports / volleyball / cricket /
rugby / handball / mma / boxing / golf / horse-racing / formula1 / motogp / table-tennis），
不返回 league 缩写（如 `mlb` / `nba` / `cs2`）。这样下游 subscription_policy /
goalserve client 用同一套语义索引。

如果 market 没有任何运动信号则返回 None——SportResolver 注入函数会让 policy 返回
空 LiveSourceKey 列表，不订 live source（避免错配）。
"""

from __future__ import annotations

import re

from polymarket_trader.domain.market import Market

# ---------------------------------------------------------------------------
# 单一权威表：(规范运动码, 联赛 / slug 关键词集合)
# ---------------------------------------------------------------------------
# 顺序敏感：先匹配命中即返回，所以更具体 / 更小概率撞车的运动放前面。
# 例如 table-tennis 在 tennis 之前（避免 "table tennis" 命中 "tennis"）。
# esports 放最后做兜底——避免短码（如 "lol"）误命中前面联赛 slug 里的子串。
_SPORT_KEYWORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("table-tennis", frozenset({"table tennis", "table-tennis", "wtt", "world team championships"})),
    ("baseball", frozenset({"mlb", "kbo", "baseball", "npb", "korean baseball"})),
    ("tennis", frozenset({"atp", "wta", "itf", "tennis"})),
    ("basketball", frozenset({"nba", "wnba", "ncaamb", "ncaawb", "ncaab", "basketball"})),
    ("ice-hockey", frozenset({"nhl", "ahl", "khl", "hockey", "ice hockey", "icehockey"})),
    ("american-football", frozenset({"nfl", "ncaaf", "american football", "amfootball"})),
    (
        "football",
        frozenset(
            {
                "soccer", "football", "mls", "nwsl", "epl", "ligue 1", "ligue1",
                "laliga", "la liga", "bundesliga", "serie a", "j1", "j2", "j3",
                "j1100", "j2100", "champions league", "europa league",
            }
        ),
    ),
    ("volleyball", frozenset({"volleyball"})),
    ("cricket", frozenset({"cricket", "ipl", "bbl", "t20", "test match", "odi"})),
    ("rugby", frozenset({"rugby", "six nations", "rugby union", "rugby league"})),
    ("handball", frozenset({"handball"})),
    ("mma", frozenset({"mma", "ufc", "bellator", "mixed martial arts"})),
    ("boxing", frozenset({"boxing"})),
    ("golf", frozenset({"golf", "pga tour", "masters", "open championship", "ryder cup", "lpga"})),
    (
        "horse-racing",
        frozenset({"horse racing", "horse race", "cheltenham", "kentucky derby", "grand national"}),
    ),
    ("formula1", frozenset({"formula 1", "formula1", "f1", "grand prix", "monaco gp"})),
    ("motogp", frozenset({"motogp", "moto gp"})),
    # esports 放最后兜底：mlbb / cs2 / dota / lol / valorant 等短码避免误命中
    # 前面联赛 slug 里的 substring（如足球预备队 "X 2" 撞 "cs2"）。
    (
        "esports",
        frozenset(
            {
                "esports", "e sports",
                # MOBA / FPS / RTS / 其它电竞项目
                "mlbb", "mobile legends",
                "cs2", "csgo", "cs go", "counter strike", "counter-strike",
                "dota2", "dota 2", "dota",
                "lol", "league of legends", "league-of-legends",
                "valorant", "val",
                "rocket league", "overwatch", "ow",
                "sc2", "starcraft",
            }
        ),
    ),
)


def _normalize_text(value: str | None) -> str:
    """归一化文本用于 token 子串匹配——大小写折叠 + 非字母数字字符转空格。

    经过 normalize 后所有 token 都是空格分隔的独立词，可用 ` token ` 包夹做精确
    词边界匹配，避免 substring 误命中（如 slug `cs2-...` 不应被 token `2` 命中）。
    """
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _has_token(text: str, token: str) -> bool:
    """判断 token 是否作为独立词（多词亦可）出现在 normalized text 中。"""
    if not text or not token:
        return False
    return f" {token} " in f" {text} "


def _resolve_from_text(text: str) -> str | None:
    """从已 normalize 的 text 中找第一个命中的运动码。"""
    if not text:
        return None
    for sport, tokens in _SPORT_KEYWORDS:
        for token in tokens:
            if _has_token(text, token):
                return sport
    return None


def resolve_sport_from_slug(slug: str | None) -> str | None:
    """从单个 slug 字符串推断运动——给只有 slug 没有完整 Market 对象的调用方用。

    典型场景：discovery 早期阶段拿到 raw_payload['slug'] 但还没构造 Market；
    或 operator UI 只传 slug 做诊断。
    """
    return _resolve_from_text(_normalize_text(slug))


def resolve_sport_from_market(market: Market) -> str | None:
    """从 Market 多字段推断主运动码。

    优先 tags → sports_market_type → category → slug。任一层命中即返回。
    无运动信号返回 None——调用方（subscription_policy 等）据此决定不订 live source。
    """
    # 1) tags：gamma 显式分类，最可信
    tags_text = _normalize_text(" ".join(market.tags) if market.tags else "")
    sport = _resolve_from_text(tags_text)
    if sport is not None:
        return sport

    # 2) sports_market_type：gamma sportsMarketType，prop 家族识别可靠
    smt_text = _normalize_text(market.sports_market_type)
    sport = _resolve_from_text(smt_text)
    if sport is not None:
        return sport

    # 3) category：事件大类
    category_text = _normalize_text(market.category)
    sport = _resolve_from_text(category_text)
    if sport is not None:
        return sport

    # 4) slug / 其它文本字段：覆盖纯 slug 命名（如 `mlbb-foo-bar-...`）
    fallback_text = _normalize_text(
        " ".join(
            part
            for part in (
                market.market_slug,
                market.event_slug,
                market.market_question,
                market.market_name,
                market.event_title,
            )
            if part
        )
    )
    return _resolve_from_text(fallback_text)


def market_sport_codes(market: Market) -> frozenset[str]:
    """返回 market 命中的所有运动码集合——供 live event 候选过滤器使用。

    多数情况下结果是单元素或空集；保留 set 形态是因为
    ``candidate_live_events_for_market`` 用 `in` 检查 event sport 是否属于市场可能
    的运动池（个别 cross-sport 联赛理论上可能命中多码）。
    """
    hits: set[str] = set()
    combined = _normalize_text(
        " ".join(
            part
            for part in (
                " ".join(market.tags) if market.tags else None,
                market.sports_market_type,
                market.category,
                market.market_slug,
                market.event_slug,
                market.market_question,
                market.market_name,
                market.event_title,
            )
            if part
        )
    )
    if not combined:
        return frozenset()
    for sport, tokens in _SPORT_KEYWORDS:
        for token in tokens:
            if _has_token(combined, token):
                hits.add(sport)
                break
    return frozenset(hits)
