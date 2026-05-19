# Tennis Data Feed

## 基本说明

- 时区：UTC
- JSON 输出：在 URL 末尾加 `?json=1`
- 所有 ID 值静态不变，不随赛季更新
- ID 在单项运动范围内唯一，非全局唯一

---

## 1. 赛事列表 (Tournaments List Feed)

刷新周期：每 3 小时

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/leagues
```

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 联赛 ID |
| country | string | 系列赛名称（见下） |
| name | string | 赛事名 |
| season | string | 当前赛季 |
| `<fixture>` | — | 当前赛季赛程/结果 |
| `<history>` | — | 历史赛季结果 |
| `<history-p2p>` | — | 历史逐分比分 |
| `<history-stats>` | — | 历史球员统计 |

**country 字段可能值：**

`Atp-Singles` / `Wta-Singles` / `Atp-Doubles` / `Wta-Doubles` / `Itf-Men-Singles` / `Itf-Women-Singles` / `Itf-Men-Doubles` / `Itf-Women-Doubles` / `Challenger-Men-Singles` / `Challenger-Women-Singles` / `Challenger-Men-Doubles` / `Challenger-Women-Doubles` / `Teams-Men` / `Teams-Women` / `Exhibition-Men` / `Exhibition-Women` / `Mixed-Doubles`

**按赛季查历史数据：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/16129-2009           # 历史结果
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/16161-2016_p2p       # 历史逐分
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/19507-2019-stats     # 历史统计
```

---

## 2. 赛程/结果 (Tournament Fixtures/Results Feed)

刷新周期：每 1 小时，可按联赛 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/19174
```

### 阶段字段

| 字段 | 类型 | 说明 |
|------|------|------|
| number | string | 阶段名称 |
| qualification | bool | True=资格赛；False=正赛 |

### 比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 开始时间 |
| status | string | Not Started / Finished / Retired / Cancelled / Suspended / Awarded / Walk over / Postponed / Abandoned |
| tb | bool | 是否在抢七（实时比分 Feed 有效；结果 Feed 始终为 False） |
| id | int | 比赛 ID |

### 球员字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球员名 |
| serve | bool | 实时 Feed：是否当前发球；结果 Feed 始终 False |
| game_score | string | 实时 Feed：当前局分（""、"0"、"15"、"30"、"40"、"A"）；结果 Feed 为空 |
| s1–s5 | string | 各盘比分；含抢七时用 "." 分隔（如 "6.5" = 赢 6 局，抢七 5 分） |
| totalscore | int | 赢盘数（比赛得分） |
| winner | bool | True=获胜；False=负 |
| id | int | 球员 ID |

---

## 3. 赛程签表 (Tournament Draw)

刷新周期：每 1 小时，提供完整签表和参赛名单

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/19174-draw
```

### 比赛字段（额外）

| 字段 | 类型 | 说明 |
|------|------|------|
| match_number | int | 签表中的比赛编号 |
| next | int | 胜者下一场比赛编号（用于构建晋级路径） |

### 球员 seed 字段

| 值 | 说明 |
|----|------|
| 数字 | 种子选手编号 |
| Alt | 替补 |
| WC | 外卡 |
| Bye | 轮空 |

---

## 4. 实时比分 (Livescore Feed)

刷新周期：每 5 秒，可按联赛 ID 过滤，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/home   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/d1     # 明日
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/d-1    # 昨日
```

**按联赛过滤：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/home?cat=23104
```

**按比赛 ID 过滤：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/match?id=978179
```

### 实时状态（status）

除通用状态外，还有：

| 值 | 说明 |
|----|------|
| Set 1–5 | 对应盘实时进行中 |
| Interrupted | 中断 |
| Retired | 退赛 |

---

## 5. 实时球员统计 (Live Game Player Stats Feed)

刷新周期：每 30 秒，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/home_gamestats   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/d-1_gamestats    # 昨日
```

**历史数据（按联赛和赛季）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/19507-2019-stats
```

---

## 6. 逐分实时 Feed (Live Game Point by Point Feed)

刷新周期：每 60 秒，每天 UTC 午夜切换日期

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/home_p2p   # 今日
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/d-1_p2p    # 昨日
```

**历史数据（按联赛和赛季）：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/16161-2016_p2p
```

---

## 7. 赛前赔率 (Pregame Odds Comparison Feed)

- 仅显示未开赛比赛（无开赛标志，需自行根据开赛时间关闭）
- **请求限制：带 ts 参数每 10 秒 1 次；不带 ts 每 30 秒 1 次**
- 赔率平均每 30 秒更新一次
- 停盘/暂停赔率标记 `stop=True`
- 支持时间戳增量更新
- 未过滤时数据量 >10MB，**必须启用 GZIP 解压**
- 不提供比分/结果，仅赔率

```
https://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=tennis_10
```

**增量更新（ts 参数）：**

```
&ts=1474825423341
```

### 赔率结构

根节点：`<scores sport="tennis" ts="1591175205">`

| 字段 | 类型 | 说明 |
|------|------|------|
| sport | string | 运动名称 |
| ts | bigint | 最后更新时间戳 |

其余赔率结构（`<type>`、`<bookmaker>`、`<total>`、`<handicap>`、`<odd>`）与其他运动相同。

### 过滤参数

| 参数 | 说明 |
|------|------|
| `date_start` / `date_end` | 日期范围（dd.MM.yyyy） |
| `league=24489` | 按联赛 ID 过滤（逗号分隔多个） |
| `league=_10133` | 按 gid 过滤（前缀 `_`） |
| `bm=16` | 按庄家 ID 过滤 |
| `market=16` | 按市场 ID 过滤 |

**字典接口：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/pregame_markets
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/pregame_bookmakers
```

**Inplay 映射：**

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/inplay-mapping
```

---

## 8. 球员排名 (Player Rankings Feed)

刷新周期：每周一 UTC 8:00，支持 ATP 和 WTA

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/atp
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/wta
```

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 球员名 |
| rank | int | 当前排名 |
| movement | string | same / up / down（相比上周） |
| country | string | 球员国籍 |
| points | decimal | 积分 |
| id | int | 球员 ID |

---

## 9. 球员档案 (Player Profiles Feed)

刷新周期：每周一 UTC 8:00，按球员 ID 访问（ID 从排名 Feed 获取）

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/profile?id=2010
```

| 字段 | 说明 |
|------|------|
| `<name>` | 球员名 |
| `<country>` | 国籍 |
| `<rank>` | 当前 ATP/WTA 排名 |
| `<bday>` | 生日（dd.MM.yyyy） |
| `results` | 近年赛事结果 |
| `schedules` | 即将参赛赛事 |
| `<stats>-><singles/doubles>` | 按年份的单/双打综合统计（排名、冠军数、各场地胜负） |
| `<tournaments>-><singles/doubles>` | 按年份的奖金数据 |
| `image` | base64 编码的球员头像（PNG） |

### 统计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | int | 年份 |
| rank | int | 年内最高排名 |
| titles | int | 冠军数 |
| matches_won / matches_lost | int | 全年总胜负 |
| hard_won / hard_lost | int | 硬地胜负 |
| clay_won / clay_lost | int | 红土胜负 |
| grass_won / grass_lost | int | 草地胜负 |

---

## 10. Head-to-Head 对比 (H2H Feed)

刷新周期：每 1 小时，使用两名球员 ID 访问

```
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/h2h_1425-1130
```

| 节点 | 说明 |
|------|------|
| top50 | 两人最近 50 场对阵 |
| overall | 双方总胜负统计 |
| leagues | 按赛事分类的对比数据 |
