# Soccer Data Feed

## 基本说明

- 时区：UTC
- JSON 输出：在 URL 末尾加 `?json=true`（例：`/soccernew/home?json=true`）
- 所有 ID 值静态不变，不随赛季更新
- ID 在单项运动范围内唯一，非全局唯一

---

## 1. 联赛列表 (Fixtures Leagues List Feed)

刷新周期：每 3 小时

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/data/mapping           # 全部联赛
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/data/mapping-current   # 进行中联赛
```

```xml
<mapping id="1204" country="England" name="Premier League" season="2019/2020"
         date_start="09.08.2019" date_end="17.05.2020" iscup="False"
         path="/leagueid/1204.xml" />
```

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 联赛 ID |
| country | string | 国家名 |
| name | string | 联赛名 |
| season | string | 当前赛季 |
| date_start | dd.MM.yyyy | 赛季开始日期（首场比赛日期） |
| date_end | dd.MM.yyyy | 赛季结束日期（末场比赛日期） |
| iscup | bool | True=杯赛；False=联赛 |
| live_lineups | bool | 是否提供实时阵容（Live Stats 包） |
| live_stats | bool | 是否提供实时球员统计（Live Stats 包） |
| live_pbp | bool | 是否提供逐球文字直播（Live Text Commentaries 包） |
| path | string | [废弃] 旧版赛程 Feed 路径 |

---

## 2. 联赛赛季列表 (League Seasons Feed)

提供每个联赛的历史赛季数据列表（结果、积分榜、赔率历史）。

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/data/seasons
```

```xml
<league id="1205" country="England" name="Championship" iscup="False">
  <results>     <!-- 历史结果赛季列表 -->
  <standings>   <!-- 历史积分榜赛季列表 -->
  <odds>        <!-- 历史赔率赛季列表（开发中） -->
```

---

## 3. 联赛球队名单 (League Teams Feed)

刷新周期：每天 UTC 10:00；包含球队阵容及球员本赛季统计

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerleague/{league_id}
```

### 球队字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球队名 |
| id | int | 球队 ID |
| venue name | string | 主场馆名 |
| venue id | int | 场馆 ID |

### 球员字段（squad）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 球员 ID |
| name | string | 球员名 |
| number | int | 球衣号码 |
| age | int | 年龄 |
| position | char | G=门将 / D=后卫 / M=中场 / A=前锋 |
| injured | bool | 伤病标记（开发中） |
| minutes | int | 本赛季上场分钟数 |
| appearences | int | 上场次数（首发+替补） |
| lineups | int | 首发次数 |
| substitute_in | int | 替补入场次数 |
| substitute_out | int | 被替换次数 |
| substitutes_on_bench | int | 坐冷板凳（未上场）次数 |
| goals | int | 进球数 |
| assists | int | 助攻数 |
| yellowcards | int | 黄牌数 |
| yellowred | int | 二黄变红次数 |
| redcards | int | 直接红牌数 |
| isCaptain | int | 担任队长场次 |
| shotsTotal | int | 总射门 |
| shotsOn | int | 射正 |
| goalsConceded | int | 失球（门将） |
| fouldDrawn | int | 被犯规次数 |
| foulsCommitted | int | 犯规次数 |
| tackles | int | 抢断 |
| blocks | int | 封堵 |
| crossesTotal | int | 传中总数 |
| crossesAccurate | int | 成功传中 |
| interceptions | int | 拦截 |
| clearances | int | 解围 |
| dispossesed | int | 失球权次数 |
| saves | int | 扑救（门将） |
| insideBoxSaves | int | 禁区内扑救（门将） |
| duelsTotal | int | 对抗总数 |
| duelsWon | int | 对抗胜利 |
| dribbleAttempts | int | 过人尝试 |
| dribbleSucc | int | 成功过人 |
| penComm | int | 导致对方罚球点数 |
| penWon | int | 赢得点球次数 |
| penScored | int | 点球得分 |
| penMissed | int | 罚丢点球 |
| penSaved | int | 扑出点球（门将） |
| passes | int | 传球总数 |
| pAccuracy | int | 传球成功率（%） |
| keyPasses | int | 关键传球数 |
| woordworks | int | 打中门框次数 |
| rating | float | 综合评分 |

---

## 4. 赛程/结果 (Fixtures/Results Feed)

刷新周期：每 2 小时；比赛取消后仍保留在 Feed 中（状态更新为 Cancl.）

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/leagueid/{league_id}
```

**按日期范围过滤：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/league/1229?date_start=16.05.2020&date_end=16.05.2020
```

**按 static_id 查单场：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/1204/2618410
```

### Feed 结构（3 种模式）

1. **单阶段联赛**（如英超）：`<results>→<tournament>→<week>→<match>`
2. **多阶段淘汰赛**（如英格兰足总杯）：`<results>→<tournament>→<stage>→<week>→<match>`
3. **两回合淘汰赛**（如欧冠）：`<results>→<tournament>→<stage>→<aggregate>→<match>`

### 赛事阶段字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 阶段名（含子阶段，如"Final"） |
| round | string | 主阶段名（对应 gid） |
| gid | int | 主阶段 ID，跨赛季不变，用于跨 Feed 映射 |
| is_current | bool | 当前阶段是否进行中 |
| stage_id | int | [废弃] 旧版阶段 ID |

### 两回合聚合字段

| 字段 | 类型 | 说明 |
|------|------|------|
| firstteam / secondteam | string | 两队名 |
| winner | int | 1=第一队胜；2=第二队胜 |
| score | string | 两回合总比分 |

### 比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 开始时间（UTC）；TBA=待公布 |
| status | string | 见下方状态表 |
| venue / venue_id / venue_city | — | 场馆信息 |
| static_id | int | **主键**，永不变更，用于跨 Feed 映射 |
| id | int | 比赛 ID，改期后会变更（用于实时 Feed 和赔率 Feed） |
| groupId | int | 分组 ID（用于含组别赛事，与积分榜 Feed 映射） |

**status 值：**

| 值 | 说明 |
|----|------|
| HH:mm | 未开赛（显示开球时间） |
| FT | 常规时间结束 |
| AET | 加时赛后结束 |
| Pen. | 点球大战后结束 |
| Postp. | 推迟 |
| Cancl. | 取消 |
| Aban. | 中止 |
| Susp. | 暂停（将在另一日继续，static_id 不变，id 会变） |

### 队伍字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 队名 |
| score | int | 含加时在内的总分 |
| ft_score | int | 常规时间（90分钟）得分 |
| et_score | int | 加时赛得分（若有） |
| pen_score | int | 点球大战得分（若有） |
| id | int | 球队 ID，永不变更 |

### 进球事件

```xml
<goal team="localteam" minute="17" player="N. Zugić" score="1 - 0"
      playerid="265716" assist="Joãozinho" assistid="112955" />
```

| 字段 | 说明 |
|------|------|
| team | localteam / visitorteam |
| minute | 进球分钟；补时用 "+" 表示（如 "90+3"） |
| player | 进球球员名；`(OG)` 表示乌龙球，`(PG)` 表示点球进球 |
| score | 进球后比分 |
| playerid / assistid | 球员/助攻球员 ID（不变） |

### 阵容（lineups）

```xml
<player number="6" name="S. Gentsoglou" booking="YC 87" id="66307" />
```

**booking 格式：**
- `""` — 未收到牌
- `"YC 87"` — 第87分钟黄牌
- `"YC 45 87"` — 第45分钟黄牌，第87分钟第二张黄牌（红）
- `"RC 87"` — 第87分钟直接红牌

### 换人（substitutions）

```xml
<substitution player_in_number="11" player_in_name="Moussa Al Tamari"
              player_in_booking="" player_in_id="454613"
              player_out_name="A. Jakoliš" player_out_id="85792" minute="46" />
```

若 `player_out_name` 为空，则该球员未上场（仅为备选球员）。

---

## 5. 实时比分 (Livescore Feed)

刷新周期：每 5 秒；每天 UTC 零点切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/home    # 今日所有比赛
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/live    # 仅进行中比赛
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/d1      # 明日
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/d-1     # 昨日
```

**按联赛过滤：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/home?cat=1204     # 按联赛 ID
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/home?gid=1204     # 按 gid
```

### 比赛实时状态（status）

| 值 | 说明 |
|----|------|
| HH:mm | 未开赛 |
| 数字（如 23） | 比赛进行中（当前分钟） |
| HT | 中场休息 |
| FT | 全场结束 |
| ET | 加时赛进行中 |
| AET | 加时赛后结束 |
| P | 点球大战进行中 |
| Pen. | 点球大战后结束 |
| Break Time | 加时赛两段之间或点球前休息 |
| Postp. / Aban. / Cancl. / Susp. / Int. / Delayed / Awarded | 见赛程 Feed |

### 补时字段

| 字段 | 说明 |
|------|------|
| timer | 常规时间结束后的补时分钟（如上半场 status="45" timer="1"） |
| inj_time | 裁判添加的总补时分钟数 |
| inj_minute | 补时阶段的当前实际分钟数 |

### 比赛事件（events）

事件类型：`goal` / `yellowcard` / `yellowred` / `redcard` / `subst`

- **进球**：`player`（球员名，可含 `(o.g.)` 乌龙球或 `(pen.)` 点球）、`result`（进球后比分）、`assist` / `assistid`
- **换人（subst）**：`player`=换出球员名，`playerId`=换出 ID；`assist`=换入球员名，`assistid`=换入 ID
- **VAR 取消事件**：独立的 `<var_cancelled>` 节点，含取消原因（`reason`）

### 实时统计（live_stats）

在 `<live_stats value="..."/>` 中以键值对形式提供：

```
ICorner=home:0,away:2 | IAttacks=home:33,away:56 | IDangerousAttacks=home:21,away:39
IOnTarget=home:2,away:3 | IPosession=home:40,away:60 | ...
```

包含：Corner / YellowCard / RedCard / Attacks / DangerousAttacks / OnTarget / OffTarget / Posession 等。

### 比分字段

```xml
<ht score="[1-2]" />     <!-- 半场比分 -->
<ft score="[1-2]" />     <!-- 常规时间结束比分 -->
<et score="[0-0]" />     <!-- 加时赛得分（若有） -->
<penalty localteam="5" visitorteam="5" />  <!-- 点球大战比分及逐球事件（若有） -->
```

### commentary_available / heatmap

- `commentary_available` 非空：该比赛有实时统计 Feed，值为联赛 ID，通过 `/commentaries/{id}.xml` 访问
- `heatmap` 非空：有热力图坐标 Feed，通过 `/commentaries/{id}_heatmap.xml` 访问

---

## 6. 实时比赛统计/阵容 (Live Game Stats Feed)

刷新周期：每 30 秒；仅限顶级联赛（可用联赛通过 `live_stats` 属性或联赛列表查询）

```
https://www.goalserve.com/getfeed/{API_KEY}/commentaries/1204.xml        # 按联赛 ID
https://www.goalserve.com/getfeed/{API_KEY}/commentaries/1.xml           # 今日全部
https://www.goalserve.com/getfeed/{API_KEY}/commentaries/1204-text.xml   # 逐球文字直播
```

**按日期查询历史数据：**

```
https://www.goalserve.com/getfeed/{API_KEY}/commentaries/1204?date=21.09.2018
```

**按 static_id 查单场：**

```
https://www.goalserve.com/getfeed/{API_KEY}/commentaries/match?id=2505310&league=1457
```

**预测阵容（赛前）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/commentaries/1204_predicted.xml
```

历史数据从 2015-2016 赛季起可查。

### 比赛状态（commentaries Feed 版本）

| 值 | 说明 |
|----|------|
| Not Started | 未开赛 |
| First Half / Second Half | 进行中 |
| Half-time / Full-time | 中场 / 结束 |
| Extra Time / After Extra Time | 加时赛 |
| Penalties / After Penalties | 点球大战 |
| Postponed / Abandoned / Canceled / Suspended / Interrupted / Delayed / Awarded | 异常状态 |

### 队伍得分字段

| 字段 | 说明 |
|------|------|
| goals | 含加时在内的总进球 |
| ht_score | 半场进球 |
| ft_score | 常规时间进球 |
| et_score | 加时赛进球 |
| pen_score | 点球大战进球 |

### summary 节点（事件列表）

分为 `<goals>` / `<yellowcards>` / `<redcards>`，每条记录包含：

- 进球：`name`、`minute`、`extra_min`、`owngoal`、`penalty`、`penalty_missed`、`id`、`assist_name`、`assist_id`
- 牌：`name`、`minute`、`extra_min`、`id`、`comment`（原因）
- VAR 检查：`event_type`（Penalty/Goal cancelled/confirmed/Card reviewed/upgraded/Red card cancelled）、`ref_decision`、`var_decision`

### 球队统计（stats）

| 字段 | 说明 |
|------|------|
| shots total/ongoal/offgoal/blocked/insidebox/outsidebox | 射门统计 |
| fouls total | 犯规数 |
| corners total | 角球数 |
| offsides total | 越位数 |
| possessiontime total | 控球率（%） |
| yellowcards / redcards total | 黄/红牌数 |
| saves total | 扑救数 |
| passes total/accurate/pct | 传球/成功传球/成功率 |

### 阵容（teams）

```xml
<localteam formation="4-2-3-1">
  <player number="2" name="Kyle Walker" pos="D" formation_pos="2" id="68532" />
</localteam>
```

`formation_pos`：球员在阵型中的编号（门将固定为 1，从左到右、从后卫到前锋排列）

### 球员实时统计（player_stats）

| 字段 | 说明 |
|------|------|
| shots_total / shots_on_goal | 射门/射正 |
| goals / goals_conceded | 进球/失球（门将） |
| minus_goals | 在场期间球队失球数 |
| assists | 助攻 |
| fouls_drawn / fouls_commited | 被犯规/犯规 |
| tackles / blocks | 抢断/封堵 |
| total_crosses / acc_crosses | 传中/成功传中 |
| interceptions / clearances | 拦截/解围 |
| saves / savesInsideBox | 扑救/禁区内扑救（门将） |
| duelsTotal / duelsWon | 对抗/胜利 |
| dribbleAttempts / dribbleSucc / dribbledPast | 过人尝试/成功/被过 |
| yellowcards / redcards | 黄/红牌 |
| pen_score / pen_miss / pen_save / pen_committed / pen_won | 点球统计 |
| passes / passes_acc / keyPasses | 传球/成功率/关键传球 |
| minutes_played | 上场分钟数 |
| rating | 综合评分 |

### 逐球文字直播（play-by-play）

```xml
<comment important="False" team="visitorteam" isgoal="True" type="Shot On Target"
         minute="85'" comment="..." pl_name1="Savinho" pl_id1="661412"
         pl_name2="Ilkay Gündogan" pl_id2="" x="0.362" y="0.562"
         timestamp="2025-01-15T08:04:14Z" id="44512891" />
```

**type 可能值：** Shot On Target / Shot Off Target / Penalty / Corner / Goal / Goal - Header / Offside / First Half starts / First half ends / Second Half starts / Second Half ends / Shot Blocked / Shot Hit Woodwork / Foul / Substitution / Corner Kick / Yellow Card / Red Card / VAR – Referee decision cancelled / VAR – Referee decision confirmed / Delay in match

`x` / `y`：事件发生的球场坐标（若有）

---

## 7. 球员伤情 (Player Injuries Feed)

刷新周期：每 1 小时；仅显示未来 2-4 天内的未开赛比赛，无比分更新

```
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/injuries
```

结构与实时比分 Feed 相同，每支球队包含三个节点：

```xml
<to_miss>     <!-- 100% 缺席（停赛或伤病确认） -->
<questionable>  <!-- 待确认（可能出现在首发名单） -->
```

```xml
<player name="S. Ascacibar" status="Foot Injury" id="432202" />
```

---

## 8. 赛前赔率 (Pregame Odds Comparison Feed)

- 仅显示未开赛比赛（无开赛标志，需自行根据开球时间关闭）
- **请求限制：每运动每 10 秒 1 次**
- 赔率平均每 30 秒更新一次
- 停盘/暂停赔率标记 `stop=True`
- 支持时间戳增量更新
- 未过滤时数据量 >100MB，**必须启用 GZIP 解压**
- 不提供比分/结果，仅赔率

```
https://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=soccer_10
```

**增量更新（ts 参数）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=soccer_10&ts=1474825423341
```

### 赔率结构

根节点：`<scores sport="soccer" ts="1591175205">`

联赛节点：

```xml
<category name="Belarus: Vysshaya Liga" gid="1099" id="1099" file_group="belarus" iscup="False">
```

| 字段 | 说明 |
|------|------|
| gid | 主阶段 ID，跨赛季不变，用于跨 Feed 映射 |
| iscup | 杯赛标记 |

比赛节点（`static_id` 与赛程 Feed 一致，可跨 Feed 映射）：

```xml
<match status="FT" static_id="2802908" fix_id="3310076" id="3472200">
```

**status 值：** `14:30`（未开赛） / `WO` / `Postp.` / `Aban.` / `Cancl.` / `Susp.` / `Int.` / `Delayed` / `Awarded`

市场/盘口节点：

```xml
<type value="Match Winner" stop="False" id="1">
  <bookmaker name="bwin" stop="False" ts="1591116398" id="2">
    <total name="3.5" ismain="False" stop="False">   <!-- 大小球 -->
    <handicap name="+2.5" ismain="False" stop="False">  <!-- 让分 -->
      <odd name="Home" value="1.05" us="-2000"/>
    </handicap>
  </bookmaker>
</type>
```

**handicap 方向规则：** `name` 属性表示第一个 `<odd>` 子节点的让分值，第二个子节点为相反值。

| 字段 | 说明 |
|------|------|
| type stop | 该市场是否全体停盘 |
| bookmaker stop | 该庄家是否单独停盘 |
| bookmaker ts | 该庄家该市场最后更新时间戳 |
| total name | 大小球线值 |
| handicap name | 让分值 |
| ismain | 是否为当前主线 |
| odd value | decimal 格式赔率 |
| odd us | [开发中] 美式赔率 |

### 过滤参数

| 参数 | 说明 |
|------|------|
| `date_start` / `date_end` | 日期范围（dd.MM.yyyy） |
| `league=1204` | 按联赛 ID 过滤（逗号分隔多个） |
| `league=_1204` | 按 gid 过滤（前缀 `_`） |
| `match=4225845` | 按比赛 id 过滤（逗号分隔多个） |
| `bm=16` | 按庄家 ID 过滤（逗号分隔多个） |
| `market=16` | 按市场 ID 过滤（逗号分隔多个） |

**字典接口：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/pregame-markets
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/pregame-bookmakers
```

**Inplay 映射：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/inplay-mapping
```

```xml
<match pregame_match_id="3423847" pregame_team1_id="5721" pregame_team2_id="5730"
       inplay_match_id="89732575" inplay_team1_id="Laci" inplay_team2_id="KF Teuta" />
```

---

## 9. 联赛积分榜 (League Standings Feed)

刷新周期：每 10 分钟

```
https://www.goalserve.com/getfeed/{API_KEY}/standings/{league_id}.xml
https://www.goalserve.com/getfeed/{API_KEY}/standings/1205?season=2018-2019   # 历史赛季
```

### 赛事节点

| 字段 | 说明 |
|------|------|
| name | 联赛名（含分组时格式：`"UEFA Champions League: Group A"`） |
| gid | 主阶段 ID（与实时 Feed、赛程 Feed 映射） |
| groupId | 分组 ID（用于与赛程 Feed 中的 groupId 对应） |
| round | 当前轮次（实际比赛轮数，不含未踢延期场次） |
| is_current | 该阶段是否激活 |

### 球队节点

```xml
<team position="1" status="same" name="PSG" id="10061" recent_form="WDWWW">
  <overall gp="6" w="5" d="1" l="0" gs="17" ga="2" />
  <home gp="3" w="3" d="0" l="0" gs="9" ga="0" />
  <away gp="3" w="2" d="1" l="0" gs="8" ga="2" />
  <total gd="+15" p="16" />
  <description value="8th Finals" />
</team>
```

| 字段 | 说明 |
|------|------|
| position | 排名 |
| status | 排名变化：up / down / same |
| recent_form | 最近 5 场：W=胜，D=平，L=负 |
| gp / w / d / l | 场次/胜/平/负 |
| gs / ga | 进球/失球 |
| gd | 净胜球 |
| p | 积分 |

---

## 10. 球队档案 (Team Profiles Feed)

刷新周期：每天 UTC 8:00；包含队标、主场图、阵容、转会、奖杯、赛季统计

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/team/{team_id}
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/team/9260,9249     # 批量（最多 50）
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/team/updated_list  # 最近更新列表
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/team/updated_list?date=15.03.2021
```

### 图片

```xml
<image></image>          <!-- 队标（base64 JPEG） -->
<venue_image></venue_image>  <!-- 主场图（base64 JPEG） -->
```

### 转会（transfers）

```xml
<in>
  <player id="276371" name="Bruno Fernandes" date="30.01.20" from="Sporting CP"
          team_id="14448" type="€ 55M"/>
</in>
<out>
  <player id="68939" name="M. Rojo" date="30.01.20" to="Estudiantes"
          team_id="5976" type="Loan"/>
</out>
```

type 可能值：金额（如 `€ 55M`）/ `Loan` / `Unknown`

### 球队统计（statistics / detailed_stats）

- `statistics`：本赛季国内联赛汇总统计
- `detailed_stats`：按联赛分类的详细统计，再细分为 `fulltime` / `firsthalf` / `secondhalf`

主要统计字段：`rank`、`win/draw/lost`（总/主/客）、`goals_for/against`、`clean_sheet`、`avg_goals_per_game_scored/conceded`、`biggest_victory/defeat`、`avg_first_goal_scored/conceded`、`shotsTotal/shotsOnGoal`、`corners`、`possession`、`fouls`、`yellowcards/redcards`

另有 `<scoring_minutes>` / `<goals_conceded_minutes>`：按 15 分钟时间段统计进/失球分布。

---

## 11. 球员档案 (Player Profiles Feed)

刷新周期：每天 UTC 8:00；包含球员头像、个人信息、历史统计、转会、奖杯、伤病

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/player/{player_id}
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/player/138653,193   # 批量（最多 50）
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/player/updated_list
https://www.goalserve.com/getfeed/{API_KEY}/soccerstats/player/updated_list?date=15.03.2021
```

### 主要节点

| 节点 | 说明 |
|------|------|
| `<image>` | base64 PNG（150×150）球员头像 |
| `<position>` | Attacker / Defender / Goalkeeper / Midfielder |
| `<statistics>` | 国内联赛历史统计（按赛季/俱乐部） |
| `<statistics_cups>` | 国内杯赛历史统计 |
| `<statistics_cups_intl>` | 国际杯赛/联赛历史统计 |
| `<statistics_intl>` | 国家队历史统计 |
| `<trophies>` | 职业生涯奖杯 |
| `<tranfers>` | 完整转会历史（含金额） |
| `<sidelined>` | 历史伤病/停赛记录 |
| `<overall_clubs>` | 所有赛季所有联赛汇总统计 |

---

## 12. Head-to-Head 对比 (H2H Feed)

刷新周期：每天 UTC 8:00；**限制：每秒最多 1 次，不允许并发请求**

```
https://www.goalserve.com/getfeed/{API_KEY}/h2h/{team1_id}/{team2_id}
```

| 节点 | 说明 |
|------|------|
| `<top50>` | 两队最近 50 场对阵（含 static_id，可与赛史 Feed 映射） |
| `<overall>` | 总胜负统计（全部/主场/客场） |
| `<leagues>` | 按联赛分类统计 |
| `<goals>` | 双方进/失球统计 |
| `<biggest_victory>` | 历史最大胜绩 |
| `<biggest_defeat>` | 历史最大败绩 |
| `<last5_home>` | 每队最近 5 场主场比赛 |
| `<last5_away>` | 每队最近 5 场客场比赛 |

---

## 13. 历史结果与积分榜 (Historical Results and Standings Feed)

查询各联赛可用历史赛季列表，再按赛季拉取历史数据。

**查询可用联赛和赛季：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerfixtures/data/seasons
```

**按赛季拉取历史结果（输出格式同赛程 Feed）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/soccerhistory/leagueid/1457-2015-2016
```

**按赛季拉取历史积分榜：**

```
https://www.goalserve.com/getfeed/{API_KEY}/standings/1205?season=2018-2019
```
