# Basketball Data Feed

## 基本说明

- 时区：UTC
- JSON 输出：在 URL 末尾加 `?json=1`
- 所有 ID 值静态不变，不随赛季更新
- ID 在单项运动范围内唯一，非全局唯一

---

## 1. 赛事列表 (Tournaments List Feed)

刷新周期：每 3 小时

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/leagues
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/leagues-current   # 仅进行中联赛
```

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 联赛 ID |
| country | string | 国家名 |
| name | string | 联赛名 |
| season | string | 当前赛季 |
| `<fixture>` | — | 当前赛季赛程/结果 |
| `<history>` | — | 历史赛季结果 |

**按赛季查历史数据：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/1287-2022-2023
```

**按赛季查历史积分榜：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktlb/1287-2022-2023_table
```

跨年赛季用 `2021-2022` 格式。

---

## 2. 赛程/结果 (Tournament Fixtures/Results Feed)

刷新周期：每 1 小时，可按联赛 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/1287
```

**按日期范围过滤：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/1046?date_start=21.06.2020&date_end=22.06.2020
```

### 比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 开始时间 |
| status | string | Not Started / Finished / Cancelled / Suspended / Awarded / Walk over / Postponed / Abandoned |
| venue | string | 场馆名（城市） |
| id | int | 比赛 ID |

### 阶段字段

| 字段 | 类型 | 说明 |
|------|------|------|
| number | string | 轮次编号（常规赛/小组赛）或阶段名（季后赛） |
| stage_id | int | 阶段 ID |
| stage | string | 阶段名 |

### 队伍字段（localteam / awayteam）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 队名 |
| Q1 | string | 第 1 节得分 |
| Q2 | string | 第 2 节得分 |
| Q3 | string | 第 3 节得分 |
| Q4 | string | 第 4 节得分 |
| OT | string | 加时赛总得分（所有加时之和） |
| totalscore | int | 总分（所有节 + 加时之和） |
| id | int | 球队 ID |

---

## 3. 实时比分 (Livescore Feed)

刷新周期：每 5 秒，可按联赛 ID 过滤，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/home   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/d1     # 明日
https://www.goalserve.com/getfeed/{API_KEY}/bsktlb/d-1    # 昨日
```

### 比赛状态（status）

| 值 | 说明 |
|----|------|
| Not Started | 未开始 |
| 1st Quarter | 第 1 节进行中 |
| 2nd Quarter | 第 2 节进行中 |
| 3rd Quarter | 第 3 节进行中 |
| 4th Quarter | 第 4 节进行中 |
| Overtime | 加时赛 |
| Half Time | 中场休息 |
| Break Time | 节间休息 |
| Finished | 已结束 |
| After Over Time | 加时后结束 |
| Awarded | 技术判负 |
| Walk Over | 技术判负 |
| Postponed | 延期 |
| Cancelled | 取消 |
| Abandoned | 弃赛 |
| Interrupted | 中断 |
| Delayed | 延迟 |

### 额外字段

| 字段 | 类型 | 说明 |
|------|------|------|
| timer | string | 实时节内计时（如有） |
| gid | int | 阶段 ID（用于与赛程 Feed 关联） |

---

## 4. 实时球员统计 (Live Game Stats Feed)

刷新周期：每 60 秒，仅支持部分联赛，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/home_stats   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/d-1_stats    # 昨日
```

**历史数据（按联赛和赛季）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/1287-2019-2020
```

### 球员统计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球员名 |
| minutes | string | 上场时间（mm:SS） |
| field_goals_made | int | 投篮命中数（2分+3分） |
| field_goals_attempts | int | 投篮出手数（2分+3分） |
| threepoint_goals_made | int | 三分命中数 |
| threepoint_goals_attempts | int | 三分出手数 |
| freethrows_goals_made | int | 罚球命中数 |
| freethrows_goals_attempts | int | 罚球出手数 |
| offence_rebounds | int | 进攻篮板 |
| defense_rebounds | int | 防守篮板 |
| total_rebounds | int | 总篮板（进攻+防守） |
| assists | int | 助攻 |
| steals | int | 抢断 |
| blocks | int | 盖帽 |
| turnovers | int | 失误 |
| personal_fouls | int | 个人犯规 |
| points | int | 总得分（2分+3分+罚球） |

---

## 5. 逐分实时 Feed (Live Game Point by Point Feed)

刷新周期：每 60 秒，仅支持部分联赛，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/home_p2p   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/d-1_p2p    # 昨日
```

### 逐分数据字段

```xml
<pointbypoint>
  <period name="1st Quarter">
    <point number="2" home_score="2" away_score="0" team_scored="home" leader_team="home" points_difference="2" />
  </period>
</pointbypoint>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| point | int | 顺序编号，从 1 开始 |
| home_score | int | 本方得分后主队积分 |
| away_score | int | 本方得分后客队积分 |
| team_scored | string | 得分方：home / away |
| leader_team | string | 领先方：home / away |
| points_difference | string | 分差（home_score - away_score） |

---

## 6. 赛前赔率 (Pregame Odds Comparison Feed)

- 仅显示未开赛比赛（无开赛标志，需自行根据开赛时间关闭）
- **请求限制：每运动每 10 秒 1 次**
- 赔率平均每 30 秒更新一次
- 停盘/暂停赔率标记 `stop=True`
- 支持时间戳增量更新
- 未过滤时数据量 >10MB，**必须启用 GZIP 解压**
- 不提供比分/结果，仅赔率

```
https://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=basket_10
```

**增量更新（ts 参数）：**

首次拉取后，从根节点提取 `ts` 属性，后续请求附加：

```
&ts=1474825423341
```

### 赔率 Feed 字段

与棒球赔率 Feed 结构相同，详见字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `<type value="Match Winner" stop="False" id="1">` | — | 盘口节点 |
| stop | bool | True=暂停；False=正常 |
| `<bookmaker name="bwin" ts="..." id="2">` | — | 庄家节点 |
| `<total name="3.5" ismain="False" stop="False">` | — | Over/Under 盘口值 |
| `<handicap name="-1.75" ismain="False" stop="False">` | — | 让分盘口值（第二子节点符号相反） |
| `<odd name="Home" value="1.05"/>` | — | 单个赔率（decimal 格式） |

**让分示例：**
- `<handicap name="+2.5">` → Home +2.5，Away -2.5
- `<handicap name="-2.5">` → Home -2.5，Away +2.5

### 过滤参数

| 参数 | 说明 |
|------|------|
| `date_start` / `date_end` | 日期范围（dd.MM.yyyy，查单天也须填 end） |
| `league=1287` | 按联赛 ID 过滤（逗号分隔多个） |
| `league=_1287` | 按 gid 过滤（前缀 `_`） |
| `bm=16` | 按庄家 ID 过滤 |
| `market=16` | 按市场 ID 过滤 |

**字典接口：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/pregame-markets
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/pregame-bookmakers
```

**Inplay 映射：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/inplay-mapping
```

```xml
<match pregame_match_id="3423847" pregame_team1_id="5721" pregame_team2_id="5730"
       inplay_match_id="89732575" inplay_team1_id="TeamA" inplay_team2_id="TeamB" />
```

---

## 7. Head-to-Head 对比 (H2H Feed)

刷新周期：每 1 小时，使用两支球队 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/h2h_1425-1130
```

| 节点 | 说明 |
|------|------|
| top50 | 两队最近 50 场比赛 |
| overall | 双方总胜负统计 |
| leagues | 按联赛分类的对比数据 |
