# Ice Hockey Data Feed

## 基本说明

- 时区：UTC
- JSON 输出：在 URL 末尾加 `?json=1`
- 所有 ID 值静态不变，不随赛季更新
- ID 在单项运动范围内唯一，非全局唯一

---

## 1. 赛事列表 (Tournaments List Feed)

刷新周期：每 3 小时

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/leagues
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
https://www.goalserve.com/getfeed/{API_KEY}/hockey/1000-2022-2023
```

**按赛季查历史积分榜：**

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/1000-2022-2023_table
```

跨年赛季用 `2021-2022` 格式。

---

## 2. 赛程/结果 (Tournament Fixtures/Results Feed)

刷新周期：每 1 小时，结果和统计在比赛结束后更新，无实时更新

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/1000
```

### 比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 开始时间 |
| status | string | Not Started / Finished / Cancelled / Suspended / Awarded / Walk over / After Over Time / After Penalties / Postponed / Abandoned |
| venue | string | 场馆名（城市） |
| id | int | 比赛 ID |

### 队伍字段（localteam / awayteam）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 队名 |
| totalscore | int | 总分（所有节之和） |
| id | int | 球队 ID |

### 比赛事件

```xml
<events>
  <firstperiod score="1 - 2">
    <event team="localteam" min="07" player="P. Siikanen" playerid="79538"
           assist="A. Kalapudas,M. Salomaki" assistid="79461,96414"
           comment="" result="[1 - 0]" type="goal"/>
  </firstperiod>
  <secondperiod score="1 - 0">
    <event team="visitorteam" min="01" player="B. Korhonen" playerid="79311"
           assist="" assistid="" comment="High sticking" result="" type="penalty"/>
  </secondperiod>
  <thirdperiod score="1 - 1"/>
  <overtime score=""/>
  <penalties score=""/>
</events>
```

**节段代码：** `firstperiod` / `secondperiod` / `thirdperiod` / `overtime` / `penalties`

`score` 属性表示该节得分；未进行的节 `score` 为空。

| 字段 | 类型 | 说明 |
|------|------|------|
| team | string | localteam / awayteam |
| min | int | 事件发生分钟 |
| player | string | 进球球员（goal）或犯规球员（penalty） |
| playerid | int | 球员 ID |
| assist | string | 助攻球员，多人用逗号分隔（goal 事件）；penalty 为空 |
| assistid | string | 助攻球员 ID，多人用逗号分隔 |
| comment | string | 犯规描述（penalty 事件） |
| result | string | 进球后比分 |
| type | string | goal / penalty |

### 球队统计

```xml
<team_stats>
  <hometeam>
    <shots ongoal="36" offgoal="8" bocked_shots="14"/>
    <saves total="21" saves_pct="84"/>
    <penalties penalties="2" minutes="4"/>
    <goals pp_goals="0" sh_goals="0" pp_pct="0" pen_kill_pct="100" empty_net_goals="0"/>
    <faceoffs won="32" pct="32"/>
  </hometeam>
  <awayteam>...</awayteam>
</team_stats>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| ongoal | int | 射正 |
| offgoal | int | 射偏 |
| blocked_shots | int | 被封堵射门 |
| total (saves) | int | 扑救次数 |
| saves_pct | int | 扑救率 |
| penalties | int | 犯规次数 |
| minutes (penalties) | int | 犯规总分钟数 |
| pp_goals | int | 强攻（PP）进球 |
| sh_goals | int | 少打（SH）进球 |
| pp_pct | int | 强攻转化率 |
| pen_kill_pct | int | 杀禁成功率 |
| empty_net_goals | int | 空门进球 |
| won (faceoffs) | int | 争球胜利次数 |
| pct (faceoffs) | int | 争球胜率 |

### 球员统计

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球员名 |
| goals | int | 总进球（含 PP/SH/常规） |
| assists | int | 助攻 |
| plus_minus | int | 正负值 |
| pp_goals | int | 强攻进球 |
| sh_goals | int | 少打进球 |
| shots_on_goal | int | 射正 |
| blocked_shots | int | 封堵 |
| penalty_minutes | int | 犯规分钟数 |
| hits | int | 冲撞 |
| shifts | int | 上场轮次 |
| time_on_ice | string | 上冰时间 |
| fouls_against | int | 被侵犯次数 |
| faceoffs_won | int | 争球胜利 |
| faceoffs_lost | int | 争球失败 |
| points | int | 积分（进球+助攻） |
| id | int | 球员 ID |

---

## 3. 实时比分 (Livescore Feed)

刷新周期：每 5 秒，可按联赛 ID 过滤，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/home   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/hockey/d1     # 明日
https://www.goalserve.com/getfeed/{API_KEY}/hockey/d-1    # 昨日
```

字段格式同赛程/结果 Feed，包含 `timer`（节内计时）字段。

---

## 4. 赛前赔率 (Pregame Odds Comparison Feed)

- 仅显示未开赛比赛（无开赛标志，需自行根据开赛时间关闭）
- **请求限制：每运动每 10 秒 1 次**
- 赔率平均每 30 秒更新一次
- 停盘/暂停赔率标记 `stop=True`
- 支持时间戳增量更新
- 未过滤时数据量 >10MB，**必须启用 GZIP 解压**
- 不提供比分/结果，仅赔率

```
https://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=hockey_10
```

**增量更新（ts 参数）：**

```
&ts=1474825423341
```

### 赔率结构

与篮球/棒球相同，包含 `<type>`（盘口）、`<bookmaker>`（庄家）、`<total>`（大小球）、`<handicap>`（让分）、`<odd>`（赔率值）节点。

**让分示例：**
- `<handicap name="+2.5">` → Home +2.5，Away -2.5
- `<handicap name="-2.5">` → Home -2.5，Away +2.5

### 过滤参数

| 参数 | 说明 |
|------|------|
| `date_start` / `date_end` | 日期范围（dd.MM.yyyy） |
| `league=1007` | 按联赛 ID 过滤（逗号分隔多个） |
| `league=_1287` | 按 gid 过滤（前缀 `_`） |
| `bm=16` | 按庄家 ID 过滤 |
| `market=16` | 按市场 ID 过滤 |

**字典接口：**

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/pregame-markets
https://www.goalserve.com/getfeed/{API_KEY}/hockey/pregame-bookmakers
```

**Inplay 映射：**

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/inplay-mapping
```

```xml
<match pregame_match_id="3423847" pregame_team1_id="5721" pregame_team2_id="5730"
       inplay_match_id="89732575" inplay_team1_id="TeamA" inplay_team2_id="TeamB" />
```

---

## 5. Head-to-Head 对比 (H2H Feed)

刷新周期：每 1 小时，使用两支球队 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/hockey/h2h_1425-1130
```

| 节点 | 说明 |
|------|------|
| top50 | 两队最近 50 场比赛 |
| overall | 双方总胜负统计 |
| leagues | 按联赛分类的对比数据 |
