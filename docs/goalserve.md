# Goalserve 数据接口

Goalserve 是本项目的体育数据源，提供实时比分（Livescore）、盘中赔率（Inplay Odds）、赛前赔率（Pregame Odds）和赛果数据。后续扩展 Totals、Moneyline、Spread 等扫尾策略的实时判定，依赖本接口的 inplay 数据。

## 资料位置

```
goalserve/
├── full_package_feed.txt     # 完整接口说明：pregame odds、livescore、历史数据
├── inplay-feed-new.txt       # 实时盘中 odds 接口说明
├── Baseball Data Feed.pdf    # 棒球赛事字段说明
├── Basketball Data Feed.pdf  # 篮球赛事字段说明
├── Esports Data Feed.pdf     # 电竞赛事字段说明
├── Hockey Data Feed.pdf      # 冰球赛事字段说明
├── Soccer Data Feed.pdf      # 足球赛事字段说明
├── Tennis Data Feed.pdf      # 网球赛事字段说明
└── UFC Data Feed.pdf         # UFC 赛事字段说明
```

API Key 保存在本地 `.env`（变量名 `GOALSERVE_API_KEY`），不进代码仓库。

## 接口分类

### 1. Inplay 实时盘口（1 秒刷新）

每秒推送最新盘中赔率，压缩 JSON 格式（GZIP）。

| 运动 | URL |
|------|-----|
| 足球 | `http://inplay.goalserve.com/inplay-soccer.gz` |
| 篮球 | `http://inplay.goalserve.com/inplay-basket.gz` |
| 网球 | `http://inplay.goalserve.com/inplay-tennis.gz` |
| 排球 | `http://inplay.goalserve.com/inplay-volleyball.gz` |
| 美式足球 | `http://inplay.goalserve.com/inplay-amfootball.gz` |
| 电竞 | `http://inplay.goalserve.com/inplay-esports.gz` |
| 冰球 | `http://inplay.goalserve.com/inplay-hockey.gz` |
| 棒球 | `http://inplay.goalserve.com/inplay-baseball.gz` |

**比赛结果**（赛后可查，用于核算盘中赔率胜负）：

```
http://inplay.goalserve.com/results/{yyyyMM}/{MATCH_ID}.json
```

**辅助字典**（盘口类型、比赛状态枚举）：

```
http://inplay.goalserve.com/dictionaries/odds-markets/{sport}
http://inplay.goalserve.com/dictionaries/states/{sport}
```

#### time_status 状态码

| 值 | 含义 |
|----|------|
| 0 | 未开始 |
| 1 | 进行中 |
| 3 | 已结束 |
| 4 | 延期 |
| 5 | 取消 |
| 6 | 弃权 |
| 7 | 中断 |
| 8 | 废弃 |
| 9 | 退赛 |
| 99 | 已移除 |

### 2. Pregame 赛前赔率

覆盖主流运动的赛前盘口，支持按联赛、时间范围、庄家、市场类型过滤。响应体为 GZIP 压缩 XML（加 `?json=1` 改为 JSON）。

**增量更新**：首次拉取完整 feed 后，从响应根节点取 `ts` 属性值，后续请求附加 `&ts={值}` 仅获取变更部分，避免全量重载。

支持运动：soccer、basketball、tennis、hockey、handball、volleyball、football（美式）、baseball、cricket、rugby、boxing、esports、futsal、mma、darts。

端点格式：

```
http://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat={sport}_10
```

常用过滤参数：

| 参数 | 说明 |
|------|------|
| `league` | 联赛 ID，逗号分隔；`_gid` 前缀表示按 gid 过滤 |
| `date_start` | 开始日期 |
| `date_end` | 结束日期（仅查单天可省略） |
| `bm` | 庄家 ID，逗号分隔 |
| `market` | 市场类型 ID，逗号分隔 |
| `match` | 比赛 ID，逗号分隔 |
| `ts` | 时间戳，用于增量更新 |

### 3. Livescore 实时比分

足球提供独立高频 livescore 接口，其他运动通过 `home`（今日）/ `d-1`（昨日）查询。

足球示例：

```
http://livescore.goalserve.com/api/v1/soccer/live?apiKey={API_KEY}   # 仅进行中
http://livescore.goalserve.com/api/v1/soccer/home?apiKey={API_KEY}   # 今日全部
```

其他运动（篮球、冰球、棒球、网球等）：

```
http://www.goalserve.com/getfeed/{API_KEY}/{sport}/home   # 今日
http://www.goalserve.com/getfeed/{API_KEY}/{sport}/d-1    # 昨日
```

运动路径关键字：`bsktbl`（篮球）、`hockey`、`baseball`、`tennis_scores`、`football`（美式）、`rugby`、`handball`、`volleyball`、`esports`、`futsal`。

### 4. Inplay 与 Pregame 映射

将 inplay 赛事 ID 映射回 pregame 赛事，用于关联赔率上下文：

```
https://www.goalserve.com/getfeed/{API_KEY}/{sport}/inplay-mapping
```

支持：soccer、esports、tennis_scores、basketball、baseball。

## 对接计划

本项目后续 Goalserve 接入分两阶段：

**阶段一：Inplay 比分源替换 / 增强**  
当前系统已有 ESPN、NBA、NHL、MLB、SofaScore 等比分源。对于已有明确 time_status 和比分结构的运动（篮球、冰球、棒球、足球），优先验证 Goalserve inplay feed 能否作为备选或主力比分源，重点关注：
- 延迟（目标 ≤ 2s）
- time_status 与现有状态机的对齐
- 分节/局数/比分字段完整性

**阶段二：Inplay Odds 接入扫尾定价**  
Inplay odds 覆盖 Moneyline、Totals、Handicap 等盘口的实时赔率，可用于：
- 交叉验证 Polymarket 当前价格与市场均衡价的偏差
- 为 Totals / Spread 扫尾策略提供"市场共识价格"参考
- 扩展盘口识别和定价逻辑

接入实现落在 `src/polymarket_trader/infra/`（外部数据源适配）和 `src/strategies/current/`（策略定价逻辑）。

## 注意事项

- API Key 只存 `.env`，绝不进代码仓库或文档。
- Pregame odds 全量 feed 数据量极大，代码层必须启用 GZIP 解压，并用 `ts` 参数做增量拉取。
- Inplay feed 1 秒一次，拉取循环须在独立 asyncio task 中运行，不占用交易主事件循环。
- Inplay feed 中 `time_status=99`（Removed）的赛事需立即停止订阅并触发相关市场的状态更新。
- 各运动字段结构差异较大，详细字段定义以 `goalserve/` 目录内对应 PDF 为准。
