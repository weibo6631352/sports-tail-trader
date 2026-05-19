# Goalserve 全套 Feed 接口汇总（中文版）

> **注意：** 真实 API Key 存放在 `.env` 的 `GOALSERVE_API_KEY` 中，以下所有 URL 均用 `{API_KEY}` 代替。
> 获取 JSON 输出：在 URL 末尾加 `?json=1`（足球用 `?json=true`）。

---

## 通用说明

- 所有 Feed 均走 HTTP 轮询，无 WebSocket
- ID 在同一运动内唯一，跨运动不唯一，且跨赛季永不变更
- 赛前赔率 Feed 数据量极大（>100MB），**必须启用 GZIP 解压**
- 赔率增量更新：首次请求后保存响应中的 `ts` 时间戳，下次请求时带上 `&ts=...` 即可只拿更新内容

---

## 一、赛前赔率（Pregame Odds）

### 各运动赔率 URL

| 运动 | URL |
|------|-----|
| 足球 | `…/getodds/soccer?cat=soccer_10` |
| 篮球 | `…/getodds/soccer?cat=basket_10` |
| 网球 | `…/getodds/soccer?cat=tennis_10` |
| 冰球 | `…/getodds/soccer?cat=hockey_10` |
| 手球 | `…/getodds/soccer?cat=handball_10` |
| 排球 | `…/getodds/soccer?cat=volleyball_10` |
| 橄榄球（美式）| `…/getodds/soccer?cat=football_10` |
| 棒球 | `…/getodds/soccer?cat=baseball_10` |
| 板球 | `…/getodds/soccer?cat=cricket_10` |
| 英式橄榄球 | `…/getodds/soccer?cat=rugby_10` |
| 英式橄榄球联赛 | `…/getodds/soccer?cat=rugbyleague_10` |
| 拳击 | `…/getodds/soccer?cat=boxing_10` |
| 电竞 | `…/getodds/soccer?cat=esports_10` |
| 五人制足球 | `…/getodds/soccer?cat=futsal_10` |
| MMA/UFC | `…/getodds/soccer?cat=mma_10` |
| 飞镖 | `…/getodds/soccer?cat=darts_10` |
| 乒乓球 | `…/getodds/soccer?cat=table_tennis_10` |

**完整 URL 格式：**
```
http://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=soccer_10
```

### 过滤参数

| 参数 | 说明 |
|------|------|
| `ts=1474825423341` | 增量更新时间戳，只返回该时间戳之后的变化 |
| `date_start=21.06.2020` | 开始日期（dd.MM.yyyy） |
| `date_end=22.06.2020` | 结束日期（dd.MM.yyyy） |
| `bm=16,2` | 按庄家 ID 过滤（逗号分隔） |
| `market=1,2` | 按市场 ID 过滤（逗号分隔） |
| `league=1204` | 按联赛 ID 过滤；用 `_1204` 则按 gid 过滤 |
| `match=4225845` | 按比赛 ID 过滤（逗号分隔） |

### Inplay-Pregame 映射

```
https://www.goalserve.com/getfeed/{API_KEY}/soccernew/inplay-mapping      # 足球
https://www.goalserve.com/getfeed/{API_KEY}/esports/inplay-mapping        # 电竞
https://www.goalserve.com/getfeed/{API_KEY}/tennis_scores/inplay-mapping  # 网球
https://www.goalserve.com/getfeed/{API_KEY}/basketball/inplay-mapping     # 篮球
https://www.goalserve.com/getfeed/{API_KEY}/baseball/inplay-mapping       # 棒球
```

### 赛前赔率结算（Settlements）

```
# 按单个盘口查结算结果
http://oddsfeed.goalserve.com/api/v1/odds/pre-game/settlement?sportId=4&gsId=85471622&marketId=16&oddname=Under:8&k={API_KEY}

# 按时间戳批量拉取（最近30分钟内更新的）
http://oddsfeed.goalserve.com/api/v1/odds/pre-game/settlements?sportId=4&dateTime=1674834565&k={API_KEY}&json=1

# 按比赛 ID 批量查（最多50个）
http://oddsfeed.goalserve.com/api/v1/odds/pre-game/settlements/matches?sportId=4&matchesIds=4734063,4734063&k={API_KEY}&json=0
```

sportId：足球=4，篮球=7，网球=5

结算结果可能值：`Win`（赢）/ `Loose`（输）/ `Stake refund`（退注）/ `Half win`（赢半）/ `Half loose`（输半）

---

## 二、足球（Soccer）

### 实时比分

```
http://livescore.goalserve.com/api/v1/soccer/home?apiKey={API_KEY}   # 今日所有比赛+实时比分
http://livescore.goalserve.com/api/v1/soccer/live?apiKey={API_KEY}   # 仅进行中比赛
```

### 近7天 / 未来7天比分

```
…/soccernew/d-1   # 昨天
…/soccernew/d-2   # 前天
…/soccernew/d-3 ~ d-7
…/soccernew/d1    # 明天
…/soccernew/d2 ~ d7
```

### 赛程与历史数据

```
…/soccerfixtures/data/mapping          # 联赛 ID 列表
…/soccerfixtures/data/seasons          # 各联赛历史赛季列表
…/soccerfixtures/leagueid/1204         # 按联赛 ID 拉取全赛季赛程（1204 为示例）
…/soccernew/abbr                       # 球队缩写
…/soccerhistory/leagueid/1204-2009-2010  # 历史赛季赛程
```

### 实时统计与阵容（Live Stats / Commentaries）

```
…/soccerfixtures/data/mapping          # 可用联赛列表（看 live_stats 属性）
…/soccerleague/1204                    # 按联赛 ID 查球队/球员名单
…/commentaries/1204.xml               # 按联赛 ID 查实时统计
…/commentaries/1.xml                  # 今日所有比赛
…/commentaries/1204-text.xml          # 逐球文字直播
…/commentaries/1204?date=21.09.2018   # 按日期查历史统计
…/commentaries/match?id=2505310&league=1457  # 按 static_id 查单场
…/commentaries/1204_predicted.xml     # 预测阵容（支持部分联赛）
```

支持预测阵容的联赛示例：1204（英超）、1229（德甲）、1221（西甲）、1269（法甲）、1399（意甲）、1005（欧冠）、1007（欧联杯）

### 伤情

```
http://www.goalserve.com/getfeed/{API_KEY}/soccernew/injuries
```

### 赔率

```
http://www.goalserve.com/getfeed/{API_KEY}/getodds/soccer?cat=soccer_10
```

### 视频集锦

```
…/soccerhighlights/home
…/soccerhighlights/d-1 ~ d-7
```

### 积分榜

```
…/standings/1204.xml                          # 当前赛季
…/standings/1390?season=2017-2018             # 历史赛季
```

### 射手榜

```
…/topscorers/1022         # 射手榜（1022 为联赛 ID）
…/topscorers/1022_assists # 助攻榜
…/topscorers/1022_cards   # 黄红牌榜
```

### 球队/球员/教练档案

```
…/soccerstats/team/9260                    # 球队档案
…/soccerstats/player/193                   # 球员档案
…/soccerstats/coach/105040                 # 教练档案
…/soccerstats/team/updated_list            # 最近更新的球队列表
…/soccerstats/player/updated_list          # 最近更新的球员列表
…/soccerstats/coach/updated_list           # 最近更新的教练列表
…/soccerleague/1204                        # 联赛下所有球队和球员
```

### H2H（两队对比）

```
http://www.goalserve.com/getfeed/{API_KEY}/h2h/9249/9002
```

---

## 三、棒球（Baseball）

### MLB

```
…/baseball/mlb_shedule    # 赛程
…/baseball/mlb-scores     # 实时比分
…/baseball/mlb-playbyplay # 逐球数据
…/baseball/mlb_standings  # 积分榜
…/baseball/usa?date=dd.MM.yyyy   # 历史比分（2010年至今）
…/baseball/mlb_shedule?date1=30.03.2023&date2=30.03.2023&showodds=true  # 赔率赛程
```

球队数据（以球队 ID=1027 为例）：
```
…/baseball/1027_stats      # 赛季统计
…/baseball/1027_injuries   # 伤情
…/baseball/1027_rosters    # 名单
…/baseball/usa?playerimage=15826  # 球员图片
```

赛季统计（按投手/打者/外野）：
```
…/baseball/mlb_player_batting / mlb_player_fielding / mlb_player_pitching
…/baseball/mlb_team_batting   / mlb_team_fielding   / mlb_team_pitching
…/baseball/nl_player_batting  / nl_player_fielding  / nl_player_pitching
…/baseball/nl_team_batting    / nl_team_fielding    / nl_team_pitching
```

### KBO（韩国职棒）

```
…/baseball/1118-scores                       # 实时比分
…/baseball/1118-scores?date=dd.MM.yyyy       # 历史比分
…/baseball/3712-rosters                      # 球队名单
```

### NPB（日本职棒）

```
…/baseball/1011-scores
…/baseball/1011-scores?date=dd.MM.yyyy
```

### 其他联赛

```
…/baseball/leagues            # 联赛列表
…/baseball/1118               # 按联赛 ID 查赛程
…/baseball/1018_table         # 按联赛 ID 查积分榜
…/baseball/home               # 实时比分（所有联赛）
…/baseball/d-1 ~ d-7         # 过去7天
…/baseball/d1 ~ d7            # 未来7天
```

---

## 四、美式橄榄球（American Football）

### NFL

```
…/football/nfl-scores                    # 实时比分
…/football/nfl-scores?date=dd.MM.yyyy    # 历史比分（2010年至今）
…/football/nfl-playbyplay-scores         # 实时逐球
…/football/nfl-shedule                   # 赛程
…/football/nfl-standings                 # 积分榜
…/football/nfl-shedule?date1=18.05.2018&date2=18.05.2018&showodds=1  # 含赔率赛程
```

球队数据（以 ID=1691 为例）：
```
…/football/1691_rosters       # 名单（含队标）
…/football/1691_injuries      # 伤情
…/football/1691_player_stats  # 球员统计
…/football/usa?playerimage=15826  # 球员图片
```

### NCAA（大学联赛）

```
…/football/fbs-scores / fbs-playbyplay-scores / fbs-shedule / fbs-standings
…/football/fbs-shedule?date1=…&date2=…&showodds=1
…/football/div3-scores / div3-shedule / div3-standings
…/football/fcs-scores  / fcs-shedule  / fcs-stand
…/football/1153_rosters / 1153_stats / 1153_player_stats
```

### XFL

```
…/xfl/xfl-scores / xfl-shedule / xfl-shedule?showodds=1
…/xfl/106_roster  # 按球队 ID 查名单
```

---

## 五、篮球（Basketball）

### 通用接口

```
…/bsktbl/leagues              # 联赛列表
…/bsktbl/1170                 # 按联赛 ID 查赛程+球员统计
…/bsktbl/1170_table           # 按联赛 ID 查积分榜
…/bsktbl/home                 # 今日实时比分
…/bsktbl/d-1 ~ d-7            # 过去7天
…/bsktbl/d1 ~ d7              # 未来7天
…/bsktbl/home_p2p / d-1_p2p  # 逐球历史
…/bsktbl/home_stats / d-1_stats  # 球员实时统计
…/bsktbl/h2h_1375-1562        # H2H 对比（按球队 ID）
```

球队/球员档案：
```
…/bsktbl/team_profile?id=1062    # 球队档案（最多20个ID，逗号分隔）
…/bsktbl/player_profile?id=1062  # 球员档案
```

### NBA

```
…/bsktbl/nba-shedule / nba-scores / nba-standings
…/bsktbl/nba-scores?date=dd.MM.yyyy          # 历史比分（2010年至今）
…/bsktbl/nba-playbyplay                       # 实时逐球
…/bsktbl/nba-scores?date=22.11.2010_pbp       # 历史逐球
…/bsktbl/nba-shedule?date1=…&date2=…&showodds=1  # 含赔率赛程
…/bsktbl/1193_rosters / 1193_stats / 1193_injuries  # 按球队ID
…/bsktbl/usa?playerimage=2011                 # 球员图片
```

### NCAA 大学篮球

```
…/bsktbl/ncaa-all-scores / ncaa-scores / ncaa-playbyplay / ncaa-shedule / ncaa-standings
…/bsktbl/ncaa-scores?date=19.01.2022_all      # 历史数据
…/bsktbl/ncaa-shedule?date1=…&date2=…&showodds=1
…/bsktbl/1985_rosters / 1985_stats / ap-rankings
```

### WNBA

```
…/bsktbl/wnba-shedule / wnba-standings / wnba-scores
…/bsktbl/wnba-scores?date=dd.MM.yyyy
# WNBA 各队名单/统计（球队缩写示例）：w_atl、w_chi、w_con、w_ind、w_nyl、w_was、w_dal、w_lva、w_las、w_min、w_pho、w_sea
…/bsktbl/w_atl_rosters / w_atl_stats  # 以此类推
```

### 其他联赛

| 联赛 | 实时比分 URL |
|------|------------|
| 欧洲联赛（Euroleague） | `…/bsktbl/1287-scores` |
| 澳大利亚 NBL | `…/bsktbl/1183-scores` |
| 希腊篮球联赛 | `…/bsktbl/2490-scores` |
| 韩国 KBL | `…/bsktbl/1519-scores` |

各联赛历史比分加 `?date=12.02.2018`

国家赛程 Feed（`argentina_shedule`、`france_shedule`、`nba_shedule`… 等30余个国家，格式统一为 `…/bsktbl/{国家}_shedule`）

---

## 六、网球（Tennis）

### 赛程与比分

```
…/tennis_scores/leagues               # 联赛列表
…/tennis_scores/atp_tournaments       # ATP 赛季赛程
…/tennis_scores/wta_tournaments       # WTA 赛季赛程
…/tennis_scores/10131                 # 按赛事 ID 查赛程和结果
…/tennis_scores/10131-draw            # 赛事签表
…/tennis_scores/home                  # 今日实时比分
…/tennis_scores/itf_today             # ITF 今日实时比分
…/tennis_scores/itf_d1                # ITF 明日赛程
…/tennis_scores/tt_live               # 乒乓球实时比分
…/tennis_scores/d-1 ~ d-7            # 过去7天
…/tennis_scores/d1 ~ d7              # 未来7天
…/tennis_scores/home_gamestats        # 重点比赛实时统计
…/tennis_scores/d-1_gamestats
…/tennis_scores/home_p2p / d-1_p2p   # 逐分
…/tennis_scores/profile?id=2010       # 球员档案
…/tennis_scores/h2h_1998-1426         # H2H 对比
```

### 赛程汇总

```
…/tennis_scores/atp_singles_shedule / wta_singles_shedule
…/tennis_scores/atp_doubles_shedule / wta_doubles_shedule
…/tennis_scores/challenger_shedule / challenger_women_shedule
…/tennis_scores/itf_men_singles_shedule / itf_women_singles_shedule
…/tennis_scores/itf_men_doubles_shedule / itf_women_doubles_shedule
…/tennis_scores/teams_men_shedule / teams_women_shedule
…/tennis_scores/itf / itf_shedule   # ITF 实时比分和赛程
```

### 球员排名

```
…/tennis_scores/atp / wta              # 常规排名
…/tennis_scores/atp_race / wta_race    # 年终总决赛积分榜
…/tennis_scores/atp_doubles / wta_doubles  # 双打排名
…/tennis_scores/atp_live / wta_live    # 实时排名（含本周积分变化）
…/tennis_scores/atp_race_live / wta_race_live
…/tennis_scores/atp_doubles_live / wta_doubles_live
```

### 乒乓球

```
…/tennis_scores/ttf_today   # 今日赛程
…/tennis_scores/ttf_d1      # 明日赛程
```

---

## 七、板球（Cricket）

```
…/cricket/livescore                   # 实时比分
…/cricket/schedule                    # 即将到来的比赛
…/cricketfixtures/tours/tours         # 所有系列赛列表（含阵容、赛程、积分榜路径）
…/cricketfixtures/intl/1015           # 系列赛完整赛程（每小时刷新）
…/cricketfixtures/intl/1015?status=scheduled  # 只看未开赛
…/cricketfixtures/intl/1015?match=13071991549  # 按比赛 ID 过滤
…/cricketfixtures/intl/1015_squads    # 球队阵容（含 T20/ODI/Test 标记）
…/cricketfixtures/intl/1015_table     # 积分榜
…/cricket/profile?id=49920            # 球员档案
```

---

## 八、冰球（Ice Hockey）

```
…/hockey/leagues              # 联赛列表
…/hockey/1002                 # 按联赛 ID 查赛程（含球员统计）
…/hockey/1002_table           # 按联赛 ID 查积分榜
…/hockey/home                 # 今日实时比分
…/hockey/d-1 ~ d-7            # 过去7天
…/hockey/d1 ~ d7              # 未来7天
…/hockey/h2h_1375-1562        # H2H 对比
…/getodds/soccer?cat=hockey_10  # 赔率
```

### NHL

```
…/hockey/nhl-scores                   # 实时比分
…/hockey/nhl-scores?date=12.02.2018   # 历史比分
…/hockey/nhl-shedule / nhl-standings
…/hockey/1144_rosters / 1144_stats / 1144_injuries  # 按球队 ID
```

### 其他联赛

| 联赛 | 实时比分 URL |
|------|------------|
| KHL（俄超）| `…/hockey/1002-scores` |
| 芬兰 Liiga | `…/hockey/1000-scores` |
| 捷克联赛 | `…/hockey/1006-scores` |
| 瑞典 SHL | `…/hockey/1004-scores` |

各联赛历史比分加 `?date=12.02.2018`；名单统一用 `…/hockey/1613_rosters`

---

## 九、赛马（Horse Racing）

```
…/racing/usa / usa_tomorrow / usa?date=dd.MM.yyyy
…/racing/uk  / uk_tomorrow  / uk?date=dd.MM.yyyy
…/racing/australia / australia_tomorrow / australia?date=dd.MM.yyyy
…/racing/hk  / hk_tomorrow  / hk?date=dd.MM.yyyy
…/racing/singapore / singapore_tomorrow / singapore?date=dd.MM.yyyy
…/racing/dubai / dubai_tomorrow / dubai?date=dd.MM.yyyy
…/racing/sa / sa?date=dd.MM.yyyy
…/racing/france / france?date=dd.MM.yyyy
…/racing/sweden / sweden?date=dd.MM.yyyy
```

---

## 十、高尔夫（Golf）

```
…/golf/live                    # PGA 实时计分板
…/golf/european_live           # DP Tour 实时
…/golf/liv_live                # LIV 实时
…/golf/lpga_live               # LPGA 实时
…/golf/champions_live          # Champions Tour 实时
…/golf/live?date=dd.MM.yyyy    # 历史比分
…/golf/pga_schedule / lpga_schedule / liv_schedule / european_schedule / champions_schedule
…/golf/rankings                # OWGR 球员世界排名
…/golf/profile?id=1153         # 球员档案
```

---

## 十一、电竞（Esports）

```
…/esports/home                        # 今日实时比分
…/esports/d-1                         # 昨日
…/esports/d1 ~ d7                     # 未来7天赛程
…/esports/home?date=dd.MM.yyyy        # 历史比分
…/esports/inplay-mapping              # Inplay-Pregame 映射
…/getodds/soccer?cat=esports_10       # 赔率
```

---

## 十二、赛车（Motor Racing）

```
…/motors/leagues                      # 支持的系列赛列表（含历史赛季、路径）
```

### Formula 1

```
…/f1/f1-live                          # 实时逐圈（每10秒更新）
…/f1/f1-results                       # 赛季赛程/结果
…/f1/f1-drivers / f1-teams            # 车手/车队积分榜
```

### MotoGP

```
…/motors/motogp-live / motogp-schedule / motogp-drivers / motogp-teams
…/motors/motogp-schedule-2021         # 历史赛季赛程（近10年）
…/motors/motogp-drivers-2021          # 历史积分榜
…/motors/1035-2021                    # 按分站 ID+赛季查历史结果
```

### 其他赛车系列

```
…/motors/moto2-live / moto2-schedule / moto2-drivers
…/motors/moto3-live / moto3-schedule / moto3-drivers
…/motors/formula2-live / formula2-schedule / formula2-drivers / formula2-teams
…/motors/formulae-live / formulae-schedule / formulae-drivers / formulae-teams  # 电动方程式
…/motors/indycar-live / indycar-schedule / indycar-drivers
…/motors/nascar-live / nascar-schedule / nascar-drivers
```

---

## 十三、英式橄榄球联合（Rugby Union）

```
…/rugby/leagues
…/rugby/home / live_stats
…/rugby/live_stats?date=dd.MM.yyyy
…/rugby/h2h_1375-1562
# 各地区：england / england_shedule、europe / europe_shedule、france、italy、worldcup、southafrica、newzealand、georgia、wales（均有 _shedule 版本）
```

---

## 十四、英式橄榄球联赛（Rugby League）

```
…/rugbyleague/home
…/rugbyleague/england / england_shedule、europe / europe_shedule、australia / australia_shedule、worldcup / worldcup_shedule
…/rugbyleague/1006-scores                           # NRL 实时统计
…/rugbyleague/1006-scores?date=12.03.2020           # 按日期查历史
```

---

## 十五、手球（Handball）

```
…/handball/leagues
…/handball/home / latest / live
…/handball/h2h_1375-1562
# 各国：austria / croatia / czechia / denmark / eurocups / finland / france / friendly / germany / norway / olympic / poland / romania / serbia / slovakia / slovenia / spain / sweden / switzerland / worldcup
# 每个国家均有 _shedule 和 _standings 版本
```

---

## 十六、AFL（澳式橄榄球）

```
…/afl/home / schedule / standings
```

---

## 十七、排球（Volleyball）

```
…/volleyball/leagues
…/volleyball/home / latest
…/volleyball/h2h_1375-1562
# 各国：croatia / czechia / denmark / eurocups / finland / france / germany / greece / italy / norway / poland / russia / slovenia / spain / turkey / worldcup
# 每个国家均有 _shedule 和 _standings 版本
```

---

## 十八、拳击（Boxing）

```
…/boxing/home
# 各量级（均有 _shedule 版本）：
# bantamweight / catchweight / cruiserweight / featherweight / featherweight_women
# flyweight / flyweight_women / heavyweight / lightheavyweight / lightmiddleweight
# lightweight / lightweight_women / lightwelterweight / middleweight / middleweight_women
# superbantamweight / superfeatherweight / superflyweight / superflyweight_women
# superheavyweight / supermiddleweight / supermiddleweight_women
# welterweight / welterweight_women
```

---

## 十九、MMA/UFC

```
…/mma/schedule                        # 赛事日程
…/mma/live                            # 实时比赛统计
…/mma/live?date=dd.MM.yyyy            # 历史比赛统计
…/mma/fighters                        # 选手列表
…/mma/fighter?profile=86637           # 选手档案（按 ID）
```

---

## 二十、斯诺克（Snooker）

```
…/snooker/home
# 各地区（均有 _shedule 版本）：asia / australia / brazil / china / eurocups / germany / scotland / thailand / uk / wales / worldcup
```

---

## 二十一、飞镖（Darts）

```
…/darts/home
…/darts/denmark / denmark_shedule
…/darts/uae / uae_shedule
…/darts/eurocups / eurocups_shedule
…/darts/worldcup / worldcup_shedule
```

---

## 二十二、奥运会（Olympics）

```
…/olympic/medals    # 奖牌榜
…/olympic/results   # 赛果
…/olympic/schedules # 赛程
```

---

## 二十三、图标/图片（Logos & Images）

```
# 联赛图标（足球，按联赛 ID，逗号分隔多个）
http://data2.goalserve.com:8084/api/v1/logotips/soccer/leagues?k={API_KEY}&ids=1151

# 球队图标（足球，按球队 ID）
http://data2.goalserve.com:8084/api/v1/logotips/soccer/teams?k={API_KEY}&ids=9002,9240

# 球员图片（高尔夫）
http://data2.goalserve.com:8084/api/v1/logotips/golf/players?k={API_KEY}&ids=1151
```

**通用格式：**
```
http://data2.goalserve.com:8084/api/v1/logotips/{运动}/{类别}?k={API_KEY}&ids={ID列表}
```

- **运动（sport）：** soccer / basketball / baseball / amfootball / hockey / cricket / golf / rugby_union / rugby_league
- **类别（category）：** leagues（联赛）/ teams（球队）/ players（球员）
- **限制：** 每秒最多 1 次请求
