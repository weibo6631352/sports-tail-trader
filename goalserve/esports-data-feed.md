# Esports Data Feed

## 基本说明

- 时区：UTC
- JSON 输出：在 URL 末尾加 `?json=1`
- 所有 ID 值静态不变，不随赛季更新

---

## 1. 赛程 Feed (Fixtures Feed)

刷新周期：每 2 小时

```
https://goalserve.com/getfeed/{API_KEY}/esports/d1   # 明日
https://goalserve.com/getfeed/{API_KEY}/esports/d2   # 后天
https://goalserve.com/getfeed/{API_KEY}/esports/d3   # 3 天后
https://goalserve.com/getfeed/{API_KEY}/esports/d4   # 4 天后
https://goalserve.com/getfeed/{API_KEY}/esports/d5   # 5 天后
https://goalserve.com/getfeed/{API_KEY}/esports/d6   # 6 天后
https://goalserve.com/getfeed/{API_KEY}/esports/d7   # 7 天后
```

### 比赛字段

| 字段 | 类型 | 说明 |
|------|------|------|
| status | string | Not Started / Started / Finished / Awarded / Cancleled / Postponed |
| id | bigint | 比赛唯一 ID |
| league_id | int | 联赛唯一 ID |
| league | string | 联赛名称 |
| round | string | 赛段（可选，非空时表示阶段名） |
| type | string | 游戏类型（见下） |
| timer | int | 实时比赛分钟（非空时有效） |
| date | dd.MM.yyyy | 比赛日期 |
| time | HH:mm | 比赛时间（UTC） |

**游戏类型（type）：**

`CS GO` / `DOTA 2` / `League Of Legends` / `Overwatch` / `PUBG` / `FIFA` / `NBA2K` / `NHL20` / `CyberTennis` / `Starcraft2`

### 队伍/玩家字段

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 主队/主玩家名（localteam）或客队/客玩家名（visitorteam） |
| id | int | 唯一 ID，永不变更 |
| score | int | 各期合计总分 |

### 计分板

```xml
<scoreboard>
  <period name="1q" localteam="22" visitorteam="26"/>
</scoreboard>
```

| 字段 | 说明 |
|------|------|
| name | 节/盘/局名称 |
| localteam | 主队本节得分 |
| visitorteam | 客队本节得分 |

### 阵容

```xml
<lineups>
  <localteam><player id="..." name="..."/></localteam>
  <awayteam><player id="..." name="..."/></awayteam>
</lineups>
```

### 地图信息（CS:GO 等）

```xml
<maps>
  <map name="Vertigo">
    <scores>
      <localteam total="16" fh="9" sh="7" ot=""/>
      <awayteam total="14" fh="6" sh="8" ot=""/>
    </scores>
    <rounds>
      <firsthalf localteam="ct" awayteam="t">
        <round localteam_score="1" awayteam_score="0" result="CtWin" winner="ct" number="1"/>
      </firsthalf>
      <secondhalf localteam="ct" awayteam="t">...</secondhalf>
    </rounds>
  </map>
</maps>
```

**地图分数字段：**

| 字段 | 说明 |
|------|------|
| total | 地图总得分 |
| fh | 上半场得分 |
| sh | 下半场得分 |
| ot | 加时赛得分（如有） |

**半场字段：**

| 字段 | 说明 |
|------|------|
| localteam / awayteam | 该半场阵营：`ct`（反恐）或 `t`（恐怖分子） |

**局结果字段：**

| 字段 | 说明 |
|------|------|
| localteam_score | 主队当前局分 |
| awayteam_score | 客队当前局分 |
| result | CtWin / TWin / BombDefused / BombExploded |
| winner | ct / t |
| number | 局编号 |

### 球员地图统计（CS:GO）

```xml
<stats>
  <localteam>
    <player id="1287" name="Geniuss" kills="21" headshots="10" assists="22"
            flash_assists="10" death="13" kast="78.3" adr="86.2"
            first_kills_diff="4" rating_2.0="1.49"/>
  </localteam>
</stats>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| kills | int | 地图总击杀 |
| headshots | int | 爆头击杀 |
| assists | int | 助攻 |
| flash_assists | int | 闪光弹助攻 |
| death | int | 死亡次数 |
| kast | float | KAST：有击杀/助攻/存活/被换的回合占比 |
| adr | float | ADR：每回合平均伤害 |
| first_kills_diff | int | 首杀数减首死数 |
| rating_2.0 | float | Rating 2.0 综合评分 |

### 比赛直播流

```xml
<streams>
  <stream title="GOTV Demo" url="http://www.hltv.org/download/demo/57485"/>
</streams>
```

---

## 2. 实时比分 Feed (Livescore Feed)

刷新周期：每 1 分钟

```
https://www.goalserve.com/getfeed/{API_KEY}/esports/home
```

字段格式与赛程 Feed 完全相同。

---

## 3. 历史结果 Feed (Historical Results Feed)

刷新周期：每 2 小时

```
https://www.goalserve.com/getfeed/{API_KEY}/esports/home?d=20.04.2020
```

`d` 参数格式：`dd.MM.yyyy`；字段格式与赛程 Feed 完全相同。

---

## 4. Inplay-Pregame 映射 (Inplay-Pregame Odds Matches Mappings Feed)

```
https://www.goalserve.com/getfeed/{API_KEY}/esports/inplay-mapping
```

```xml
<mappings sport="esports">
  <match pregame_match_id="118181" pregame_team1_id="1102" pregame_team2_id="1250"
         inplay_match_id="89975920" inplay_team1_id="FURIA" inplay_team2_id="Infinity"/>
</mappings>
```

| 字段 | 类型 | 说明 |
|------|------|------|
| pregame_match_id | bigint | Pregame Feed 唯一比赛 ID |
| pregame_team1_id | int | Pregame Feed 主队唯一 ID |
| pregame_team2_id | int | Pregame Feed 客队唯一 ID |
| inplay_match_id | bigint | Inplay Feed 唯一比赛 ID |
| inplay_team1_id | string | Inplay Feed 主队名（用作 ID） |
| inplay_team2_id | string | Inplay Feed 客队名（用作 ID） |

---

## 5. Inplay 赔率 Feed

```
http://69.64.68.124:8083/api/v1/mappings/esports/{MATCH_ID}?key={INPLAY_KEY}
```

`MATCH_ID` 为 Inplay Feed 中的比赛 ID。
