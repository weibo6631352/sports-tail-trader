# Sports Opportunity Expansion Design

## 目标

在现有体育扫尾策略上增加两类机会：比赛已结束但 Polymarket 尚未封盘的确定性机会，以及已有持仓在优势继续扩大时的受控加仓机会。

## 设计原则

- 新机会仍属于当前策略能力，主要落在 `src/strategies/current/`。
- 买入、加仓、退出覆盖继续走统一交易主链路，不新增直连下单旁路。
- 结束未封盘必须以外部直播源明确 `ENDED`、单场盘口、无来源冲突、可从最终比分明确判断目标 token 为前提。
- `ENDED` 是确定性机会状态，不属于恢复链路的异常暂停状态；取消、延期、退赛、争议和未知状态仍必须暂停新增交易。
- 加仓不是亏损补仓，只允许在已有退出覆盖、没有开放 BUY、优势状态强于普通入场阈值、总暴露仍在上限内时触发。
- 风控默认继续禁止“有退出 SELL 时再次 BUY”；只有策略显式标记为受控加仓的 BUY intent，才允许穿过该门禁，并保留审计记录。

## 机会类型

### `ended_not_closed`

适用场景：

- `game.status == ended`
- market 仍处于可交易状态
- market family 为 `single_game`
- 直播源无冲突
- 当前盘口价格和流动性满足策略上限

判定规则：

- Totals：最终总分大于 line 买 Over，最终总分小于 line 买 Under，等于 line 不交易。
- Moneyline：最终比分目标方胜出才交易，平局不交易。
- Spreads：`目标方最终分差 + line > 0` 才交易，等于 0 不交易。
- Tennis：优先使用结构化 tennis state 判断 moneyline、set winner、match games / sets totals；无法明确结算方向时不交易。

### `scale_in_advantage`

适用场景：

- 当前 token 已有持仓。
- 当前持仓已有退出 SELL 覆盖，覆盖份额至少等于已有持仓份额。
- 同 token 没有开放 BUY。
- 当前比分状态仍支持原方向，并且达到更严格的加仓阈值。
- 买入价格仍低于对应盘口价格上限。
- 加仓金额受单笔、单市场、单比赛、单联赛、当日上限约束。
- 加仓成交后由现有成交回调继续补挂新增份额的退出 SELL。

加仓阈值：

- Moneyline：剩余时间不超过普通阈值的一半，且领先至少比普通阈值多 2 分。
- Spreads：剩余时间不超过普通阈值的一半，且安全边际至少比普通阈值多 1。
- Totals Under：剩余时间不超过普通阈值的一半，且安全边际至少比普通阈值多 1。
- Totals Over：已越过 line 后继续拉开至少 1 分。
- Tennis Moneyline：当前盘目标方至少 5 局且领先至少 3 局，或比赛已经结束且目标方胜出。
- Tennis Totals：整场总局数使用 `tennis_state.total_games`，总盘数使用结构化盘数，不复用普通球队比分总分。

## 运行链路

1. live state worker 继续把直播状态写入 entry metadata。
2. entry planner 构造当前 market/token 的候选快照。
3. recovery 只暂停真正异常的直播状态；`ended` market 保持可评估，交给 `ended_not_closed` 判断是否交易。
4. 当前策略先评估 `ended_not_closed`，再评估普通尾盘机会，最后评估 `scale_in_advantage`。
5. scale-in 通过策略 metadata 显式标记 `allow_open_exit_overlap`，由 intent builder 写入 BUY intent。
6. TradingDecisionWorker 只允许 `POSITION_OPEN` / `FOLLOW_UP_ORDER_OPEN` 生命周期中的受控加仓继续执行。
7. RiskManager 默认仍拒绝开放退出单上的 BUY；仅当 intent 明确允许受控加仓时通过该门禁。
8. BUY 成交后，现有 `decide_follow_up` 继续为新增份额挂 GTC SELL。

## 验收口径

- 已结束未封盘的 Moneyline / Totals / Spreads 能在最终比分明确时生成 BUY。
- push、平局、比分源冲突、无法解析 target 的市场不交易。
- 普通已有持仓加开放退出单仍然禁止重复入场。
- 符合 `scale_in_advantage` 的持仓可生成加仓 BUY，并通过 open exit 风控。
- 加仓 BUY 成交后仍会触发新增份额的退出 SELL。
- 非 `scale_in_advantage` 的 BUY 不得绕过 open exit gate。
