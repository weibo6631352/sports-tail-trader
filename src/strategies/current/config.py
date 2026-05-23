"""当前默认策略的配置定义。

这个文件只负责描述“策略自己关心的业务参数”，不负责框架级配置。
二次开发时如果只是替换筛选词、价格阈值、流动性门槛，通常从这里开始改。

远端 discovery 粗筛会调用 Polymarket Gamma Events keyset API：
https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination

能安全前移到粗筛的条件，应当是 Gamma API 原生支持、且即使接口语义波动也不会
破坏本地最终判断的条件，例如标题搜索词和稳定 tag slug。价格、盘口深度、spread、
持仓和挂单状态依赖热态数据，继续留在本地 universe / trading 判断里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from polymarket_trader.extension_api import load_extension_config
from strategies.current.tail import (
    ExecutionPermission,
    SportsMarketType,
    TailPolicy,
)


def _default_league_source_affinity() -> Mapping[str, tuple[str, ...]]:
    """各 league 偏好的直播源顺序，靠前权重更高（aggregate league-aware priority）。

    全部联赛切换到 Goalserve inplay feed（IP 白名单 + 1 秒刷新）。
    """

    _gs = ("goalserve_inplay",)
    return {
        "NBA": _gs,
        "WNBA": _gs,
        "NHL": _gs,
        "MLB": _gs,
        "NFL": _gs,
        "NCAAF": _gs,
        "NCAAMB": _gs,
        "NCAAWB": _gs,
        "NCAAB": _gs,
        "ATP": _gs,
        "WTA": _gs,
        "EPL": _gs,
        "PREMIER-LEAGUE": _gs,
        "CS2": _gs,
        "DOTA2": _gs,
        "LOL": _gs,
        "VALORANT": _gs,
        "UFC": _gs,
        "MMA": _gs,
    }



@dataclass(frozen=True, slots=True)
class CurrentStrategyConfig:
    """当前策略的静态配置。

    字段说明：
        entry_no_price_max:
            框架入场 BUY 的兜底最高价格。体育扫尾会优先使用各盘口自己的
            价格上限；这里保留给现有分配和下单接线使用。
        exit_no_price:
            启用自动退出时使用的目标挂卖价格。当前默认扫尾策略买入后等待
            权威结算，不主动挂 follow-up SELL。
        auto_exit_enabled:
            是否在 BUY 成交或持仓恢复时自动生成 SELL。默认开启：退出 overlay
            必须每个决策周期运行，才能基于实时盘口动态重估退出价/退出时机；
            settlement-only 模式（仅等权威结算、不主动挂 SELL）需显式设为 False。
        tail_dynamic_exit_stop_loss_fraction:
            动态止损阈值（CLAUDE.md §17）。当我方方向的 fair value 跌到
            买入价 × 此比例以下时，说明比赛/赔率已明确逆转，主动按当前
            best bid 卖出止损，宁可只损失部分本金，不等结算归零。默认 0.5。
        tail_dynamic_exit_take_profit_multiple:
            动态止盈倍数。best bid ≥ 买入价 × 此倍数时按 best bid 卖出锁利，
            抓波段高点。无 Goalserve 赔率的市场靠此判据触发止盈。默认 1.5。
        tail_dynamic_exit_lock_in_price:
            动态止盈锁定价。best bid ≥ 此价即视为结果接近锁定，提前锁利
            避开结算黑天鹅（CLAUDE.md §17）。默认 0.93。
        tail_dynamic_exit_trailing_retreat_fraction:
            动态止盈回撤反转阈值。处于止盈区时 best bid 从峰值回撤达到峰值
            的此比例即判定顺势趋势反转，在接近峰值处兑现。默认 0.04（回撤 4%）。
        tail_dynamic_exit_min_hold_return_per_hour:
            动态止盈资金占用效率门槛。持有到结算的每小时收益率低于此值时
            主动卖出腾资金重新部署。默认 0.03（每小时 < 3% 视为低效）。
        tail_dynamic_exit_max_slippage_fraction:
            动态止盈滑点容忍上限。退出按 bid 深度逐档撮合算出真实成交均价；
            若 (best_bid - realized_avg) / best_bid 超过此比例（或 bid 簿深度
            不足以吃完全部份额），说明 bid 簿太薄、止盈卖单会吃大滑点——
            此时非紧急止盈分支改为 HOLD 等深度回补，不把仓位砸进薄簿。
            止损紧急分支不受此约束（割肉优先于滑点）。默认 0.03。
        tail_dynamic_exit_depth_band_fraction:
            双侧深度统计带宽。bid depth = best_bid 下方此比例区间内所有 bid
            档位的 USDC 名义额之和（买方力量）；ask depth = best_ask 上方同
            比例区间内所有 ask 档位名义额之和（卖方力量）。默认 0.05
            （统计最优价上下 5% 价格区间内的双侧名义额）。
        tail_dynamic_exit_imbalance_reversal_drop:
            双侧深度失衡反转阈值。失衡比 = bid depth /（bid depth + ask
            depth），∈[0,1]，>0.5 买方占优、<0.5 卖方占优。持仓浮盈时若失衡
            比从历史峰值向卖方倾斜下降达此值（买方撤离 + 卖方堆单 = 风向
            逆转），主动按逐档撮合价兑现浮盈，抢在价格被砸下来前锁定收益。
            默认 0.20（失衡比从峰值跌 0.20 即判定风向逆转）。
        min_liquidity_usdc:
            允许入场前要求达到的最小盘口深度，单位是 USDC。
        max_spread:
            允许的最大买一卖一价差；为 ``None`` 表示不限制。
        discovery_tag_slugs:
            远端 discovery 的 tag slug。当前实现(``build_configured_discovery_queries``)
            为每个 tag slug 发 3 个定向查询:① ``live=true``;② ``start_time``
            在 ``[now-12h, now+1h]``(进行中+临近开赛);③ ``start_time`` 在
            ``[now+1h, now+24h]``(未来 24h)。基于 Polymarket 自己的数据
            完整覆盖、零名字匹配。默认 ``("sports",)`` 已够用。
        tail_category_tokens:
            本地 universe 精筛时用于识别已接入直播源的体育联赛 token。
            识别文本以 category/tags 为优先信号，并用 market/event slug、
            问题和标题兜底处理 Gamma 缺失标签的真实赛事。这里不使用泛化的
            ``sports``，避免未建模联赛仅凭大类标签进入自动交易候选。
        tail_enabled_market_types:
            体育扫尾允许纳入 universe 的盘口类型。
        tail_*:
            体育扫尾策略自己的价格、流动性、时间窗口和执行权限参数。
        tail_max_*:
            体育扫尾策略级相关性硬上限，用于约束同一比赛、同一联赛和当日新增
            暴露（替代旧绝对 USDC，改为 bankroll fraction + 绝对 floor）。
            框架级 Kelly 单仓 cap (`KELLY_MAX_POSITION_FRACTION`) 仍由
            RiskManager 做最终门禁。
        tail_min_expected_profit_*:
            入场前按买入价格和买入金额估算等待权威结算的毛利润和资金占用效率。
            如果结算持有收益太低，策略只在可挂出满足最小毛利润的 profit-take
            SELL 时允许买入，否则拒绝这类长时间占用资金的小利润订单。
        tail_profit_take_min_profit_usdc:
            低结算效率订单允许走 profit-take 路径时，目标卖价相对买入价至少需要
            产生的预期毛利润。
        tail_profit_take_hold_minutes:
            估算一档 profit-take 挂单成交前的资金占用时间。小绝对利润订单只有
            按该占用时间折算后的每小时资金效率达标时才允许进入，避免长期挂单
            只赚极小金额。
        tail_entry_maker_max_resting_seconds:
            历史遗留开放 BUY 的最长容忍秒数。自动入场 BUY 只走触发式
            FAK 市价单；该参数只用于恢复链路撤掉旧 GTC BUY，避免长期占用资金。
            撤掉后下一轮 entry signal 会重跑 Kelly（重读最新 bankroll / fair_value），
            实现"maker 单 staleness → 重新 sizing"闭环（C10）。
        tail_settlement_hold_minutes:
            估算等待权威结算的保守资金占用时间。实盘里市场结束到可结算可能跨越
            数小时，因此这里不只看比赛剩余时间。
        tail_baseball_max_game_state_age_seconds:
            MLB 官方结构化局面允许的最大状态年龄。MLB schedule/linescore
            拉取和匹配会批量处理大量市场，不能用通用 10 秒阈值误杀第 9 局
            这类真实尾盘；该放宽只作用于棒球结构化局面。
        tail_mlb_eighth_moneyline_min_lead:
            MLB 第 8 局 moneyline 早期领先机会的最低领先分差。该规则还要求
            至少一出局且二/三垒无得分威胁，避免把普通中局波动提前纳入。
        tail_mlb_ninth_moneyline_min_lead:
            MLB 第 9 局（含延长赛）moneyline 早期领先机会的最低领先分差。
            允许 0 出局即开始评估；阈值高于第 8 局规则（默认 3），因为价格
            往往已上升到 0.93-0.97 区间，需要足够领先保证正期望。
        tail_recovery_profit_take_*:
            恢复侧对历史遗留或买入后缺失止盈挂单的近端仓位补救退出参数。
            默认只在持仓均价较高、无开放 SELL、且挂到下一档 tick 的预期毛利润
            达标时补 profit-take SELL，避免继续长期占用资金。
    """

    # Kelly sizing 参数（策略层决策参数，不进框架 Settings）。
    # κ 默认 0.25 = quarter Kelly：模型不确定性下的工业标准（max drawdown 约半 full Kelly）。
    kelly_fraction: Decimal = Decimal("0.25")
    # 单仓上限（占 bankroll 比例）。设为 1.0 = 不额外设单仓硬上限，严格按
    # quarter-Kelly（kelly_fraction）公式定注——κ=0.25 本身即风险缓冲，
    # 不再叠加一个会裁掉合理凯利仓位的 10% 硬顶。
    kelly_max_position_fraction: Decimal = Decimal("1.0")
    # 最低 edge 阈值；实测 edge < 200 bps 时 Kelly 公式对 p 估计误差极敏感，不下单。
    kelly_min_edge: Decimal = Decimal("0.02")
    # 框架硬下限 USDC；实际 effective_min_stake = max(此值, market.min_order_size × price)。
    kelly_min_stake_usdc: Decimal = Decimal("1")
    # Kelly 推荐 stake < market min 时是否凑齐到 market min（轻度 over-bet）。
    kelly_allow_round_up_to_market_min: bool = True
    # 凑齐金额上限 = position_cap × ratio。1.0=凑齐金额最多到 cap；> 1 时让 RiskManager
    # 的 effective position cap 同步放宽——bankroll 极小阶段唯一能下单的方式。
    kelly_round_up_max_overbet_ratio: Decimal = Decimal("10")
    # drawdown lockout：bankroll 跌破 peak × halt_fraction 时拒新仓。0 关闭。
    kelly_drawdown_halt_fraction: Decimal = Decimal("0")

    entry_no_price_max: Decimal = Decimal("0.99")
    exit_no_price: Decimal = Decimal("0.995")
    auto_exit_enabled: bool = True
    min_liquidity_usdc: Decimal = Decimal("1")
    max_spread: Decimal | None = Decimal("0.10")
    discovery_tag_slugs: tuple[str, ...] = ("sports",)
    # CLAUDE.md §9：所有目标盘口必须纳入诊断，不得 silent 忽略。universe 接受的
    # 运动现已全部具备直播源——KBO 走 baseball/home livescore，table-tennis/WTT
    # 走 tennis_scores/tt_live livescore，其余由 inplay GZIP + livescore 覆盖。
    # 仍有市场拿不到 live state 时由 live-source-gaps 标 unsupported_league，
    # 区分"暂时缺直播 vs 联赛根本不被覆盖"。
    tail_category_tokens: tuple[str, ...] = (
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "kbo",
        "basketball",
        "baseball",
        "korean baseball",
        "hockey",
        "football",
        "soccer",
        "table tennis",
        "table-tennis",
        "wtt",
        "tennis",
        "atp",
        "wta",
        "cricket",
        "ipl",
        "bbl",
        "rugby",
        "esports",
        "cs2",
        "csgo",
        "counter-strike",
        "dota2",
        "dota",
        "lol",
        "league-of-legends",
        "valorant",
    )
    tail_enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
        SportsMarketType.BINARY_PROP,
    )
    tail_totals_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_moneyline_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_spreads_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_min_entry_price: Decimal = Decimal("0.10")
    tail_totals_max_entry_price: Decimal = Decimal("0.99")
    tail_moneyline_max_entry_price: Decimal = Decimal("0.98")
    # 锁定结果的入场价上限。锁定方向必然结算到 1.0，0.98 保证 ≥2% 确定毛利——
    # Polymarket 手续费 ∝ price×(1-price)，p=0.98 处手续费极小，2% 毛利稳覆盖。
    tail_locked_outcome_max_entry_price: Decimal = Decimal("0.98")
    tail_spreads_max_entry_price: Decimal = Decimal("0.96")
    tail_min_liquidity_usdc: Decimal = Decimal("1")
    tail_max_game_state_age_seconds: int = 10
    tail_baseball_max_game_state_age_seconds: int = 45
    tail_tennis_max_game_state_age_seconds: int = 35
    # J1/J2/Australia/各次级足球联赛 livescore feed 更新周期 60-90s(实测重启后
    # 仍有 sports_live_state_stale pause 触发);设 120s 给足 buffer,避免 reconcile
    # 误把活跃 J2 market 标 not_tradable 导致 system 评估接受却下不了单。
    tail_soccer_max_game_state_age_seconds: int = 120
    # esports livescore feed 服务端每 60s 刷新、锁定信号（已赢地图数）单调，
    # 90s 新鲜度窗口匹配其真实更新率。
    tail_esports_max_game_state_age_seconds: int = 90
    # 赛事起始时间超过该秒数且仍无任何直播状态 → 视为 stale market 主动 pause。
    # 默认 24 小时：MLB/NBA/NHL 单场比赛通常 4-6 小时内完成；超过 24 小时无任何
    # 直播信号意味着该 market 已脱离入场窗口（赛事已结束 / 联赛不被任何数据源覆盖），
    # 继续扫描只是 noise。reconcile 看到 pause 后会把 market 从订阅集合排除。
    tail_stale_no_live_state_seconds: int = 86_400
    tail_max_under_seconds_remaining: int = 30
    tail_max_moneyline_seconds_remaining: int = 180
    tail_max_spreads_seconds_remaining: int = 120
    tail_min_under_safety_margin: Decimal = Decimal("2")
    # MLB Under 总分入场最早可考虑的局数；早于此局一律拒绝。
    tail_mlb_under_min_inning: int = 6
    # MLB Under 每提前一局（早于 9 局）额外要求的 safety margin。
    tail_mlb_under_inning_margin_step: Decimal = Decimal("2")
    tail_min_moneyline_lead: int = 6
    tail_soccer_min_moneyline_lead: int = 1
    tail_hockey_min_moneyline_lead: int = 1
    tail_mlb_eighth_moneyline_min_lead: int = 2
    tail_mlb_ninth_moneyline_min_lead: int = 3
    tail_min_spread_safety_margin: Decimal = Decimal("2")
    # 赔率差价入场（odds-gap，CLAUDE.md §17 第二条入场路径）：当 Goalserve 盘中
    # 去抽水真实概率高出 Polymarket ask 至少此差值时入场。0.06 需覆盖约 3% taker
    # 手续费 + 安全余量；低于此差价不下单。操盘手可经 ParameterStore override 调整。
    # 0 = 无门槛: 只要 Goalserve 有 devig 真概率就视为 odds_gap 候选(Kelly 自决)。
    # Kelly 内部用 net edge 算 fraction; net edge ≤ 0 时 Kelly reject, 否则按比例下注。
    # 不在 Kelly 之上叠加额外阈值 cap。保留字段以便审计/A-B test;实测倾向永久 0。
    tail_odds_gap_min_edge: Decimal = Decimal("0")
    # 赔率差价候选执行权限；默认 AUTO_EXECUTE，与扫尾锁定一致走完整入场链路。
    tail_odds_gap_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    # 单场景 (single-game tail) implied fair value 公式：``cap = fair × (1 - edge_required)``
    # 解出 fair。500 bps = 5% 表示策略相信"fair 比 cap 至少高 5%"。
    # 用于 Kelly sizing 的 prob_p。outright path 直接用 the-odds-api 真概率。
    #
    # 与 ``tail_outright_min_edge_bps`` 数学功能相同（required edge），但作用范围不同：
    # 这个用于 tail single-game 反推 implied prob_p；outright_min_edge_bps 用于
    # outright family 在 fair_value 已知后算 entry_price_cap。outright 接通 Kelly 后
    # （B9 / 未来工作）再考虑统一字段。
    # 操盘手运行时调宽走 ParameterStore override，不动默认。
    tail_implied_min_edge_bps: int = 500
    # tail implied_p 的不确定性 → κ 缩放（confidence）。0.5 = 半 κ baseline。
    # 实际 conf = base × min(1, ask_depth / depth_baseline_usdc) × max(0.25, 1 - spread/spread_widening)
    # 流动性薄 / 价差宽时进一步收缩，符合"implied_p 在低质量盘口里更不可靠"。
    # outright path 当前不走 Kelly（_size_outright_entry 用固定 budget 包络 +
    # outright/evaluator 的反向定价），不消费此字段；未来 outright 接通 Kelly 时
    # 应直接用 the-odds-api 真概率 + conf=1.0（B9 / 未来工作）。
    # 操盘手运行时调宽走 ParameterStore override，不动默认。
    tail_implied_prob_confidence: Decimal = Decimal("0.5")
    # 流动性 baseline：ask_depth >= 此值时不再缩 conf；不到时按比例缩。25 USDC ≈ 5 shares × 0.50。
    tail_implied_conf_depth_baseline_usdc: Decimal = Decimal("25")
    # spread 容忍：spread > 此值时 conf 衰减到 25%；spread=0 时不缩。
    tail_implied_conf_spread_widening: Decimal = Decimal("0.05")
    # Goalserve 盘口交叉验证：当 Goalserve 对目标方向的隐含概率比 Polymarket ask 低超过
    # 此阈值时，拒绝入场（防止在 Goalserve 认为对手方大幅领先时仍买入我方 YES）。
    # 设为较宽（0.25）保守起步；操盘手可调高（例如 0.30）收紧 or 调低至 0.0 关闭。
    goalserve_cross_validation_margin: Decimal = Decimal("0.25")
    # 关闭 Goalserve 交叉验证（调试用）；True = 启用，False = 只加 metadata 不拒绝。
    goalserve_cross_validation_enabled: bool = True
    # Goalserve 强确认：当 Goalserve 对目标方向隐含概率比 Polymarket ask 高出此阈值时，
    # 视为 Goalserve 强力背书，允许对 price_cap 加成以争取更多成交量。
    goalserve_strong_edge_threshold: Decimal = Decimal("0.05")
    # price_cap 加成比例：Goalserve 强确认时将 price_cap 上调此比例（如 0.02 = 多 2¢/dollar）。
    # 设为 0 则不加成——仅写入 metadata 供审计。
    goalserve_strong_edge_price_bonus: Decimal = Decimal("0.02")
    # 是否启用 Goalserve 强确认 price_cap 加成（False 时仍写 metadata 但不改价）。
    goalserve_strong_edge_enabled: bool = True
    # Goalserve Spread 方向一致性验证：当 Spread 盘口对目标方向隐含概率与 Moneyline 方向
    # 矛盾（spread 偏向对手方且差值超阈值）时，额外要求更大领先优势再入场。
    goalserve_spread_conflict_threshold: Decimal = Decimal("0.15")
    # Spread 冲突时要求比 min_moneyline_lead 多几分才允许入场（直接修改评估策略）。
    goalserve_spread_conflict_extra_lead: int = 3
    # 多信号确认（Moneyline + Spread 均认可我方）时可豁免几分领先要求（放宽评估）。
    goalserve_multi_confirm_lead_relief: int = 2
    # 多信号确认时允许在距比赛结束更早的时间入场（扩展买入时窗，秒）。
    goalserve_multi_confirm_time_bonus_seconds: int = 30
    # 是否启用 Goalserve 多信号策略调参（False 时跳过策略修改，仅写 metadata）。
    goalserve_policy_adjustment_enabled: bool = True
    # Goalserve 半场/第二节 Money Line 否决：若目标方向在半场盘口隐含概率低于此阈值，
    # 说明书商认为剩余半场目标方依然落后，拒绝入场。
    goalserve_halftime_veto_min_implied: Decimal = Decimal("0.30")
    # 是否启用半场 Money Line 否决（False 时仅写 metadata 不拒绝）。
    goalserve_halftime_veto_enabled: bool = True
    tail_max_consecutive_losses: int = 3
    tail_scale_in_budget_fraction: Decimal = Decimal("0.5")
    tail_scale_in_max_buy_fills: int = 2
    tail_min_expected_profit_usdc: Decimal = Decimal("0.03")
    tail_min_expected_profit_per_hour_usdc: Decimal = Decimal("0.10")
    tail_profit_take_min_profit_usdc: Decimal = Decimal("0.02")
    tail_profit_take_hold_minutes: int = 2  # 流动性好时预计止盈成交时间（分钟）
    # bid 侧深度低于此值视为薄市场，止盈单大概率等结算，效率按结算持仓时间算
    tail_profit_take_liquid_bid_depth_usdc: Decimal = Decimal("10")
    # 止盈目标价：三选一，优先级 offset > multiplier > 默认上一档 tick。
    # tail_profit_take_offset：目标卖价 = entry_price + offset，超过 1.0 自动收敛到 0.99。
    #   固定 offset 让盘中提前止盈可达（如 +0.07：买 0.88 → 卖 0.95），不必死等结算。
    # tail_profit_take_multiplier：目标卖价 = entry_price × multiplier（旧模式）。
    # 两者都为 None：只上一档 tick（原有资金效率模式）。
    tail_profit_take_offset: Decimal | None = None
    tail_profit_take_multiplier: Decimal | None = None
    # 动态止损阈值：fair value 跌破 entry_price × 此比例时主动按 best bid 卖出止损。
    # 0.5 = 价值跌到买入价一半即止损（CLAUDE.md §17），不等结算输掉全部本金。
    tail_dynamic_exit_stop_loss_fraction: Decimal = Decimal("0.5")
    # 动态止盈倍数：best bid ≥ entry_price × 此倍数时按 best bid 卖出锁利。
    # 1.5 = 浮盈达 50% 即抓波段高点；这是无 Goalserve 赔率市场的止盈兜底
    # （fair value 退回市场中价时 bid≥fair value 永不成立，靠此倍数判据触发）。
    tail_dynamic_exit_take_profit_multiple: Decimal = Decimal("1.5")
    # 动态止盈锁定价：best bid ≥ 此价即视为结果接近锁定，提前锁利避开结算
    # 黑天鹅（CLAUDE.md §17：价格超过约 0.92 应挂卖单）。
    tail_dynamic_exit_lock_in_price: Decimal = Decimal("0.93")
    # 动态止盈回撤反转阈值：处于止盈区时，best bid 从峰值回撤达到峰值的此比例
    # 即判定顺势趋势反转，在接近峰值处兑现浮盈。0.04 = 回撤 4%。
    tail_dynamic_exit_trailing_retreat_fraction: Decimal = Decimal("0.04")
    # 动态止盈资金占用效率门槛：持有到结算的每小时收益率低于此值时主动卖出
    # 腾出资金重新部署。0.03 = 每小时 < 3% 即认为占用资金太低效（CLAUDE.md §17）。
    tail_dynamic_exit_min_hold_return_per_hour: Decimal = Decimal("0.03")
    # 动态止盈滑点容忍上限：退出价基于 bid 深度逐档撮合算真实成交均价，若
    # (best_bid - realized_avg)/best_bid 超过此值或 bid 簿吃不完全部份额，
    # 非紧急止盈分支 HOLD 等深度回补，不把仓位砸进薄簿吃滑点（CLAUDE.md §17）。
    tail_dynamic_exit_max_slippage_fraction: Decimal = Decimal("0.03")
    # 双侧深度统计带宽：bid/ask depth = 最优价上下此比例区间内各档位 USDC
    # 名义额之和，分别作买方/卖方力量代理。0.05 = 最优价上下 5% 价格区间。
    tail_dynamic_exit_depth_band_fraction: Decimal = Decimal("0.05")
    # 双侧深度失衡反转阈值：失衡比 = bid /（bid+ask）depth；持仓浮盈时失衡比
    # 从峰值向卖方倾斜下降达此值即判风向逆转，按逐档撮合价兑现。0.20。
    tail_dynamic_exit_imbalance_reversal_drop: Decimal = Decimal("0.20")
    tail_entry_maker_max_resting_seconds: int = 60
    tail_settlement_hold_minutes: int = 180
    # 比赛结束后等待 Polymarket 权威结算的缓冲时间（分钟）。
    # 动态结算时间估算：live 时 = ceil(seconds_remaining/60) + buffer；
    # 已结束时 = buffer；无比赛状态时回退 tail_settlement_hold_minutes。
    tail_settlement_buffer_minutes: int = 60
    tail_recovery_profit_take_enabled: bool = True
    tail_recovery_profit_take_min_avg_price: Decimal = Decimal("0.90")

    # Series 结算时间估算：每场比赛间隔（包含主客场轮换/交通/休息日）。
    # NBA/NHL 季后赛典型约 2-3 天；使用偏保守的 2.5 天作为默认。
    tail_series_avg_days_per_game: float = 2.5
    # Series 资金效率门槛（每天最低预期利润，美元）。
    # 防止持有几周但利润极薄的 series 头寸长期占用资金。
    tail_series_min_expected_profit_per_day_usdc: Decimal = Decimal("0.02")

    # Outright family 配置。默认 budget=0 + RECORD_ONLY；必须两个 flip 才真实下单。
    tail_outright_enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.MONEYLINE,
        SportsMarketType.BINARY_PROP,
    )
    tail_outright_execution_permission: ExecutionPermission = ExecutionPermission.RECORD_ONLY
    tail_outright_min_edge_bps: int = 500  # 5%
    tail_outright_max_entry_price: Decimal = Decimal("0.85")
    tail_outright_budget_usdc: Decimal = Decimal("0")
    tail_outright_max_per_market_usdc: Decimal = Decimal("25")
    tail_outright_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_outright_max_hold_horizon_days: int = 180
    tail_outright_max_season_odds_age_seconds: int = 14400
    tail_outright_season_odds_ttl_seconds: int = 1800
    tail_outright_reassessment_interval_seconds: int = 3600
    tail_outright_exit_edge_target: Decimal = Decimal("0.03")
    tail_outright_min_profit_per_share: Decimal = Decimal("0.02")
    tail_outright_min_remaining_days: int = 7
    tail_outright_entry_maker_max_resting_seconds: int = 86400
    tail_outright_min_orderbook_depth_usdc: Decimal = Decimal("100")
    # Outright 资金效率门槛（每天最低预期利润，美元）。
    # 防止持有几个月但利润极薄的 outright 头寸长期占用资金。
    tail_outright_min_expected_profit_per_day_usdc: Decimal = Decimal("0.02")

    # Series WINNER family 配置。默认 budget=0 + RECORD_ONLY；与 outright 同样
    # 双 flip（permission=AUTO_EXECUTE + budget>0）才会真实下单。
    tail_series_winner_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    tail_series_winner_min_edge_bps: int = 200
    tail_series_winner_max_entry_price: Decimal = Decimal("0.95")
    tail_series_winner_budget_usdc: Decimal = Decimal("25")
    tail_series_winner_max_per_market_usdc: Decimal = Decimal("25")
    tail_series_winner_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_series_winner_min_orderbook_depth_usdc: Decimal = Decimal("50")
    tail_series_winner_max_state_age_seconds: int = 3600  # 1h：比赛日内 state 应频繁刷新
    tail_series_winner_max_game_odds_age_seconds: int = 7200  # 2h
    tail_series_winner_max_hold_horizon_days: int = 30
    tail_series_winner_min_remaining_days: int = 0  # 系列赛剩余比赛随时可成交
    tail_series_winner_exit_edge_target: Decimal = Decimal("0.05")
    tail_series_winner_min_profit_per_share: Decimal = Decimal("0.02")

    # Series TOTAL_GAMES family 配置。分布尾端方差大，default min_edge 比 WINNER 略高。
    tail_series_total_games_execution_permission: ExecutionPermission = ExecutionPermission.RECORD_ONLY
    tail_series_total_games_min_edge_bps: int = 1000  # 10%
    tail_series_total_games_max_entry_price: Decimal = Decimal("0.92")
    tail_series_total_games_budget_usdc: Decimal = Decimal("0")
    tail_series_total_games_max_per_market_usdc: Decimal = Decimal("25")
    tail_series_total_games_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_series_total_games_min_orderbook_depth_usdc: Decimal = Decimal("50")
    tail_series_total_games_max_hold_horizon_days: int = 30
    tail_series_total_games_min_remaining_days: int = 0

    # Series GAME_HANDICAP family 配置。single_game scope 与 series scope 共用
    # 同一组配置——风险定价路径不同但资金 / 时间窗约束等同。
    tail_series_handicap_execution_permission: ExecutionPermission = ExecutionPermission.RECORD_ONLY
    tail_series_handicap_min_edge_bps: int = 1000  # 10%
    tail_series_handicap_max_entry_price: Decimal = Decimal("0.92")
    tail_series_handicap_budget_usdc: Decimal = Decimal("0")
    tail_series_handicap_max_per_market_usdc: Decimal = Decimal("25")
    tail_series_handicap_max_event_correlation_usdc: Decimal = Decimal("40")
    tail_series_handicap_min_orderbook_depth_usdc: Decimal = Decimal("50")
    tail_series_handicap_max_hold_horizon_days: int = 30
    tail_series_handicap_min_remaining_days: int = 0

    # league-aware 源亲和：aggregate_client 用此覆盖默认全局源优先级表。
    # 仅当前体育扫尾策略关心；framework Settings 不持有，CLAUDE.md §10。
    league_source_affinity: Mapping[str, tuple[str, ...]] = field(
        default_factory=_default_league_source_affinity
    )


def tail_policy_from_config(config: CurrentStrategyConfig) -> TailPolicy:
    """把当前策略配置转换成体育扫尾纯业务策略参数。"""

    return TailPolicy(
        enabled_market_types=config.tail_enabled_market_types,
        totals_execution_permission=config.tail_totals_execution_permission,
        moneyline_execution_permission=config.tail_moneyline_execution_permission,
        spreads_execution_permission=config.tail_spreads_execution_permission,
        min_entry_price=config.tail_min_entry_price,
        totals_max_entry_price=config.tail_totals_max_entry_price,
        moneyline_max_entry_price=config.tail_moneyline_max_entry_price,
        locked_outcome_max_entry_price=config.tail_locked_outcome_max_entry_price,
        spreads_max_entry_price=config.tail_spreads_max_entry_price,
        min_liquidity_usdc=config.tail_min_liquidity_usdc,
        max_game_state_age_seconds=config.tail_max_game_state_age_seconds,
        baseball_max_game_state_age_seconds=config.tail_baseball_max_game_state_age_seconds,
        tennis_max_game_state_age_seconds=config.tail_tennis_max_game_state_age_seconds,
        esports_max_game_state_age_seconds=config.tail_esports_max_game_state_age_seconds,
        max_under_seconds_remaining=config.tail_max_under_seconds_remaining,
        max_moneyline_seconds_remaining=config.tail_max_moneyline_seconds_remaining,
        max_spreads_seconds_remaining=config.tail_max_spreads_seconds_remaining,
        min_under_safety_margin=config.tail_min_under_safety_margin,
        mlb_under_min_inning=config.tail_mlb_under_min_inning,
        mlb_under_inning_margin_step=config.tail_mlb_under_inning_margin_step,
        min_moneyline_lead=config.tail_min_moneyline_lead,
        soccer_min_moneyline_lead=config.tail_soccer_min_moneyline_lead,
        hockey_min_moneyline_lead=config.tail_hockey_min_moneyline_lead,
        mlb_eighth_moneyline_min_lead=config.tail_mlb_eighth_moneyline_min_lead,
        mlb_ninth_moneyline_min_lead=config.tail_mlb_ninth_moneyline_min_lead,
        min_spread_safety_margin=config.tail_min_spread_safety_margin,
        odds_gap_min_edge=config.tail_odds_gap_min_edge,
        odds_gap_execution_permission=config.tail_odds_gap_execution_permission,
    )


def default_strategy_config() -> CurrentStrategyConfig:
    """返回内置默认配置。

    返回：
        一份可直接用于生产装配的 ``CurrentStrategyConfig``。
    """

    return CurrentStrategyConfig()


def load_current_strategy_config(config_path: str | None) -> CurrentStrategyConfig:
    """从外部配置文件加载当前策略配置。

    参数：
        config_path:
            外部配置文件路径。支持 ``json`` / ``toml``。如果为 ``None``，
            或者调用方没有提供配置文件，则退回默认配置。

    返回：
        解析后的 ``CurrentStrategyConfig``。
    """

    return load_extension_config(CurrentStrategyConfig, config_path) or default_strategy_config()
