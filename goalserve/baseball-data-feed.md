# Baseball Data Feed

## 基本说明

- 时区：UTC
- JSON 输出：在 URL 末尾加 `?json=1`
- 所有 ID 值静态不变，不随赛季更新
- ID 在单项运动范围内唯一，非全局唯一

---

## 1. 赛事列表 (Tournaments List Feed)

刷新周期：每 3 小时

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/leagues
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
https://www.goalserve.com/getfeed/{API_KEY}/baseball/1000-2022
```

跨年赛季用 `2021-2022` 格式。

---

## 2. 赛程/结果 (Tournament Fixtures/Results Feed)

刷新周期：每 1 小时，可按联赛 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/1000
```

### 比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 开始时间 |
| status | string | Not Started / Finished / Cancelled / Suspended / Awarded / Walk over / Postponed / Abandoned |
| stats_id | int | MLB 球员统计 ID（仅 MLB） |
| extra_inn | int | 加赛局数 |
| venue | string | 场馆名 |
| id | int | 比赛 ID |

### 队伍字段（localteam / awayteam）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 队名 |
| totalscore | int | 总分（全部局 + 加赛局之和） |
| in1–in9 | int | 第 1–9 局得分 |
| extra | int | 加赛局得分 |
| hits | int | 安打数 |
| errors | int | 失误数 |
| id | int | 球队 ID |

---

## 3. 实时比分 (Livescore Feed)

刷新周期：每 5 秒，可按联赛 ID 过滤，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/home   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/baseball/d1     # 明日
https://www.goalserve.com/getfeed/{API_KEY}/baseball/d-1    # 昨日
```

字段格式同赛程/结果 Feed。

---

## 4. 实时球员统计 (Live Game Stats Feed)

刷新周期：每 60 秒，仅支持部分联赛（MLB），每天太平洋时区午夜切换。
可通过 `stats_id` 和 `date` 字段与 Livescore / Fixtures Feed 关联。

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/usa          # 今日
https://www.goalserve.com/getfeed/{API_KEY}/baseball/yesterday    # 昨日
```

### 比赛字段（额外）

| 字段 | 类型 | 说明 |
|------|------|------|
| timezone | string | ET（GMT-4）或 EDT（GMT-5） |
| status | string | Postponed / Cancelled / Abandoned / Live / Not Started / Final / Final/11 / Top of 1st / Bot of 1st … / Break Time / Awarded / Walk Over / Interrupted / Delayed |
| venue_name | string | 场馆名 |
| venue_id | int | 场馆 ID |
| attendance | int | 上座人数 |
| broadcast | string | 美国转播频道（"\|"分隔） |
| datetime_utc | datetime | UTC 时间 |

### 详细局分

```xml
<innings>
  <inning number="1" score="0" hits="1" />
  ...
</innings>
```

### 关键进攻事件 (Scoring Plays)

```xml
<events>
  <event team="awayteam" inn="5" desc="Taylor doubled to right..." chw="0" cle="1" />
</events>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| team | string | awayteam / hometeam |
| inn | int | 局数 |
| desc | string | 事件描述 |
| chw | int | 主队得分（事件后） |
| cle | int | 客队得分（事件后） |

### 打者统计 (Hitters)

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球员名 |
| pos | string | 位置 |
| at_bats | int | 打席 |
| runs | int | 得分 |
| hits | int | 安打 |
| doubles | int | 二垒安打 |
| triples | int | 三垒安打 |
| home_runs | int | 全垒打 |
| sac_fly | int | 牺牲飞球 |
| hit_by_pitch | int | 触身球 |
| runs_batted_in | int | 打点 |
| walks | int | 保送 |
| strikeouts | int | 三振 |
| average | decimal | 本场打击率 |
| stolen_bases | int | 盗垒 |
| on_base_percentage | decimal | 上垒率 |
| slugging_percentage | decimal | 长打率 |
| cs | int | 盗垒失败 |
| id | int | 球员 ID |

### 投手统计 (Pitchers)

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球员名 |
| innings_pitched | int | 投球局数 |
| runs | int | 失分 |
| earned_runs | int | 自责分 |
| hits | int | 被安打 |
| walks | int | 保送 |
| strikeouts | int | 三振 |
| home_runs | int | 被全垒打 |
| pc-st | int | 投球数（总 / 好球） |
| earned_runs_average | decimal | 自责分率 |
| id | int | 球员 ID |

**历史数据（按日期，2010-01-01 起）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/usa?date=15.07.2025
```

**MLB 球队名单（按球队 ID）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/1001_rosters
```

---

## 5. 赛前赔率 (Pregame Odds Comparison Feed)

- 仅显示未开赛比赛（无开赛标志，需自行根据开赛时间关闭）
- **请求限制：每运动每 10 秒 1 次**
- 赔率平均每 30 秒更新一次
- 停盘/暂停赔率标记 `stop=True`
- 支持时间戳增量更新
- 未过滤时数据量 >10MB，**必须启用 GZIP 解压**
- 不提供比分/结果，仅赔率

```
https://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=baseball_10
```

**增量更新（ts 参数）：**

首次拉取后，从根节点提取 `ts` 属性，后续请求附加：

```
&ts=1474825423341
```

### 赔率 Feed 字段

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
| `league=1000` | 按联赛 ID 过滤（逗号分隔多个） |
| `league=_1287` | 按 gid 过滤（前缀 `_`） |
| `bm=16` | 按庄家 ID 过滤 |
| `market=16` | 按市场 ID 过滤 |
| `match=337994` | 按比赛 ID 过滤 |

**字典接口：**

```
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/pregame_markets
https://www.goalserve.com/getfeed/{API_KEY}/bsktbl/pregame_bookmakers
```

**Inplay 映射：**

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/inplay-mapping
```

```xml
<match pregame_match_id="3423847" pregame_team1_id="5721" pregame_team2_id="5730"
       inplay_match_id="89732575" inplay_team1_id="TeamA" inplay_team2_id="TeamB" />
```

---

## 6. Head-to-Head 对比 (H2H Feed)

刷新周期：每 1 小时，使用两支球队 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/baseball/h2h_1425-1130
```

| 节点 | 说明 |
|------|------|
| top50 | 两队最近 50 场比赛 |
| overall | 双方总胜负统计 |
| leagues | 按联赛分类的对比数据 |
