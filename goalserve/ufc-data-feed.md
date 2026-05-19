# UFC Data Feed

## 基本说明

- **时区：东部时区（Eastern Timezone）**，与其他运动不同（其他为 UTC）
- JSON 输出：在 URL 末尾加 `?json=1`
- 所有 ID 值静态不变，不随赛季更新
- ID 在单项运动范围内唯一，非全局唯一
- **请求限制：每个接口每秒最多 1 次**

---

## 1. 赛事日程 (Tournament Schedule Feed)

刷新周期：每 2 小时；快速结果更新请使用实时比分接口

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/schedule
```

### 赛事字段

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 赛事 ID |
| name | string | 赛事系列名（如 "UFC Fight Night: Dern vs Ribas 2"） |
| date | dd.MM.yyyy | 赛事开始日期 |

### 单场比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 比赛时间 |
| status | string | Final / Not Started / Postponed / Cancelled |
| ismain | bool | 是否为本场赛事主赛；一场赛事可有多个主赛 |
| type | string | 按体重级别分类（见下） |
| id | int | 比赛 ID |

**体重级别（type）：**

男子：Bantamweight / Flyweight / Middleweight / Light Heavyweight / Lightweight / Heavyweight / Featherweight / Welterweight

女子：Women's Bantamweight / Women's Flyweight / Women's Middleweight / Women's Light Heavyweight / Women's Lightweight / Women's Heavyweight / Women's Featherweight / Women's Welterweight

### 选手字段（localteam / awayteam）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 选手名 |
| winner | bool | 比赛结束后是否获胜 |
| id | int | 选手 ID |

---

## 2. 实时比分/比赛统计 (Live Fight Stats Feed)

刷新周期：每 30 秒，包含每位选手详细统计；历史数据从 1993 年 11 月 12 日首场官方 UFC 比赛起均可查

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/live
```

**按日期查询：**

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/live?date=18.01.2025
```

### 胜负结果

```xml
<win_result>
  <won_by type="Points" round="3" minute="5:00">
    <ko type="" target="" />
    <sub type="" />
    <points score="30-27 | 30-27 | 30-27" />
  </won_by>
</win_result>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| type | string | 胜利方式：Points（判定）/ KO（击倒）/ S Dec（裁判决定）/ SUB（降伏） |
| round | int | 比赛结束局数 |
| minute | MM:ss | 结束时刻 |

**KO 类型（type）：** Punch / Punches / Kick / Elbows

**points score 格式：** `主队-客队 | 主队-客队 | 主队-客队`（三位裁判各自评分）

### 选手统计

```xml
<stats>
  <localteam>
    <strikes_total head="81" body="3" legs="1" />
    <strikes_power head="59" body="2" legs="1" />
    <takedowns att="15" landed="8" />
    <submissions total="2" />
    <control_time total="9:43" />
    <knockdowns total="0" />
  </localteam>
</stats>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| strikes_total | — | 总击打次数，按头部/身体/腿部分类 |
| strikes_power | — | 强力击打（远距离 + 近战/地面强力击打，不含近战/地面短促击打） |
| takedowns att/landed | int | 摔跤尝试次数 / 成功次数 |
| submissions total | int | 降伏技尝试次数 |
| control_time total | MM:ss | 控制时间（在地面或贴身位置的主导时间） |
| knockdowns total | int | 击倒次数 |

---

## 3. 赛前赔率 (Pregame Odds Comparison Feed)

- 仅显示未开赛比赛（无开赛标志，需自行根据开赛时间关闭）
- 赔率平均每 30 秒更新一次
- 停盘赔率在下次请求时从 Feed 中移除
- 支持 date1/date2 日期范围过滤
- **必须启用 GZIP 解压**
- 不提供比分/结果，仅赔率

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/schedule?showodds=1
```

**按日期范围过滤：**

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/schedule?date1=17.11.2021&date2=20.11.2021&showodds=1
```

### 赔率节点额外字段

| 字段 | 类型 | 说明 |
|------|------|------|
| ts | bigint | 最后更新时间戳（ticks） |
| rotation_home | int | 主选手 rotation 编号 |
| rotation_away | int | 客选手 rotation 编号 |

### odd 节点额外字段

| 字段 | 类型 | 说明 |
|------|------|------|
| value | decimal | decimal 格式赔率 |
| dp3 | decimal | 保留 3 位小数的 decimal 赔率 |
| us | int | 美式赔率格式 |

**注意：** 每个 `<odd>` 子节点有独立的 `handicap` 属性用于让分盘的正确映射。

### 过滤参数

| 参数 | 说明 |
|------|------|
| `date1` / `date2` | 日期范围（dd.MM.yyyy） |
| `bm=82,` | 按庄家 ID 过滤（过滤单个庄家时需在 ID 后加逗号） |
| `market=2,` | 按市场 ID 过滤（过滤单个市场时需在 ID 后加逗号） |

**字典接口：**

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/markets
https://www.goalserve.com/getfeed/{API_KEY}/mma/bookmakers
```

---

## 4. 选手档案 (Fighter Profiles / Headshots)

刷新周期：每周一 UTC 8:00，包含选手简历、职业生涯统计和头像（base64 编码）

```
https://www.goalserve.com/getfeed/{API_KEY}/mma/fighter?profile=84279
```
