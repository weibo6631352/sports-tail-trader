import { useQuery } from '@tanstack/react-query'
import { Anchor, Box, Divider, Group, SimpleGrid, Stack, Text, Tooltip } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import { qk } from '@core/api/keys'
import { decisionsApi, marketsApi, sportsApi } from '@core/api/resources'
import type {
  PositionRow,
  KellyInternals,
  OrderbookSnapshot,
  LiveStateRow,
  MarketLiquidity,
  PricesHistory,
} from '@core/api/types'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'
import { InlineActionButton } from '@shared/ui/InlineActionButton'

// 单仓位实时分析卡——把所有"输入量化决策器之前的原始数据" + 决策器输出
// 一次性呈现, 让操盘人凭这些数据判断决策器行为是否合理.
//
// 三大区段:
//   1. 比赛 + 全赔率源: live_game 完整字段 + 4 类 Goalserve 赔率 + 信号源元数据
//   2. Polymarket 盘口: orderbook + liquidity 分析 + 价格 sparkline
//   3. 持仓 + 量化决策: PnL + 最新 quant_decide reason + Kelly 内核
//
// 5s 自动刷新; sparkline 30s 节流.

function fmtPct(v: unknown, dp = 1): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${(n * 100).toFixed(dp)}%`
}

function fmtPp(v: unknown, dp = 2): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${n > 0 ? '+' : ''}${(n * 100).toFixed(dp)}pp`
}

function fmtNum(v: unknown, dp = 3): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toFixed(dp)
}

function fmtUsdc(v: unknown): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `$${n.toFixed(2)}`
}

function pnlTone(v: unknown): 'pos' | 'neg' | 'neutral' {
  if (v == null) return 'neutral'
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'neutral'
  return n > 0 ? 'pos' : 'neg'
}

function pnlColor(tone: 'pos' | 'neg' | 'neutral'): string {
  if (tone === 'pos') return '#5cd9c5'
  if (tone === 'neg') return '#ff6b6b'
  return 'inherit'
}

function edgeTone(v: unknown): 'success' | 'danger' | 'neutral' {
  if (v == null) return 'neutral'
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'neutral'
  return n > 0 ? 'success' : 'danger'
}

function ageStr(iso?: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return '—'
  const s = (Date.now() - t) / 1000
  if (s < 60) return `${Math.floor(s)}s 前`
  if (s < 3600) return `${Math.floor(s / 60)}m 前`
  return `${Math.floor(s / 3600)}h 前`
}

function gameStatusLabel(status?: string | null): { tone: 'success' | 'warning' | 'neutral' | 'danger'; text: string } {
  const s = (status ?? '').toLowerCase()
  if (s === 'live') return { tone: 'success', text: '进行中' }
  if (s === 'paused') return { tone: 'warning', text: '暂停' }
  if (s === 'ended') return { tone: 'neutral', text: '已结束' }
  if (s === 'postponed') return { tone: 'danger', text: '延期' }
  if (s === 'cancelled') return { tone: 'danger', text: '取消' }
  if (s === 'retired') return { tone: 'danger', text: '弃赛' }
  return { tone: 'neutral', text: status ?? '—' }
}

// 轻量 SVG sparkline,无外部依赖.
function Sparkline({ points, width = 240, height = 36 }: { points: number[]; width?: number; height?: number }) {
  if (points.length < 2) return null
  const min = Math.min(...points)
  const max = Math.max(...points)
  const range = max - min || 1
  const stepX = width / (points.length - 1)
  const path = points
    .map((y, i) => {
      const px = i * stepX
      const py = height - ((y - min) / range) * height
      return `${i === 0 ? 'M' : 'L'} ${px.toFixed(1)} ${py.toFixed(1)}`
    })
    .join(' ')
  const last = points[points.length - 1]
  const first = points[0]
  const color = last > first ? '#5cd9c5' : last < first ? '#ff9f5b' : '#97a6c2'
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
      <path d={path} stroke={color} strokeWidth={1.2} fill="none" />
    </svg>
  )
}

// 一对 odds badge: 主/客 implied prob + 处暂停时显示 suspended.
function OddsBadgePair({
  market,
  homeLabel,
  awayLabel,
  homeProb,
  awayProb,
  suspended,
  homeSuspended,
  awaySuspended,
  extras,
}: {
  market: string
  homeLabel: string
  awayLabel: string
  homeProb?: number | null
  awayProb?: number | null
  suspended?: boolean
  homeSuspended?: boolean
  awaySuspended?: boolean
  extras?: string
}) {
  if (homeProb == null && awayProb == null) {
    return (
      <Stack gap={2}>
        <Text size="xs" c="dimmed">{market}</Text>
        <Text size="xs" c="dimmed">—</Text>
      </Stack>
    )
  }
  return (
    <Stack gap={2}>
      <Group gap={4}>
        <Text size="xs" c="dimmed">{market}</Text>
        {extras ? <Text size="xs" c="dimmed">{extras}</Text> : null}
        {suspended ? <StatusPill tone="warning" size="xs">锁盘</StatusPill> : null}
      </Group>
      <Group gap={4}>
        <Text size="xs" ff="monospace" c={homeSuspended ? 'dimmed' : undefined}>
          {homeLabel} {fmtPct(homeProb, 1)}
        </Text>
        <Text size="xs" c="dimmed">/</Text>
        <Text size="xs" ff="monospace" c={awaySuspended ? 'dimmed' : undefined}>
          {awayLabel} {fmtPct(awayProb, 1)}
        </Text>
      </Group>
    </Stack>
  )
}

// sport-specific state 总览(每种 sport 一段).
function SportStateLine({ live_game }: { live_game: Record<string, unknown> | undefined }) {
  if (!live_game) return null
  const lg = live_game as {
    esports_state?: { best_of?: number | null; current_map_index?: number | null; home_maps_won?: number; away_maps_won?: number; home_current_map_score?: number | null; away_current_map_score?: number | null; map_winners?: unknown[] }
    baseball_state?: { inning?: number | null; inning_half?: string | null; outs?: number | null; balls?: number | null; strikes?: number | null; on_base?: unknown }
    basketball_state?: { quarter?: number | null; time_remaining_seconds?: number | null }
    tennis_state?: { set?: number | null; game?: number | null; server?: string | null; home_sets?: number | null; away_sets?: number | null }
    soccer_state?: { half?: number | null; minute?: string | null; match_events?: unknown[] }
    cricket_state?: Record<string, unknown> | null
    handball_state?: Record<string, unknown> | null
    volleyball_state?: Record<string, unknown> | null
    race_state?: Record<string, unknown> | null
  }
  // esports
  if (lg.esports_state) {
    const s = lg.esports_state
    return (
      <Text size="xs" ff="monospace" c="dimmed">
        BO{s.best_of ?? '?'} · maps {s.home_maps_won ?? 0}-{s.away_maps_won ?? 0}
        {s.current_map_index != null ? ` · map ${s.current_map_index + 1}` : ''}
        {s.home_current_map_score != null
          ? ` · 当前局 ${s.home_current_map_score}-${s.away_current_map_score ?? '?'}`
          : ''}
      </Text>
    )
  }
  if (lg.baseball_state) {
    const s = lg.baseball_state
    return (
      <Text size="xs" ff="monospace" c="dimmed">
        {s.inning_half ?? ''}{s.inning ?? '?'} · {s.outs ?? '?'} 出局
        {s.balls != null ? ` · ${s.balls}-${s.strikes ?? '?'}` : ''}
      </Text>
    )
  }
  if (lg.basketball_state) {
    const s = lg.basketball_state
    const time = s.time_remaining_seconds != null
      ? `${Math.floor(s.time_remaining_seconds / 60)}:${(s.time_remaining_seconds % 60).toString().padStart(2, '0')}`
      : '—'
    return <Text size="xs" ff="monospace" c="dimmed">Q{s.quarter ?? '?'} · {time}</Text>
  }
  if (lg.tennis_state) {
    const s = lg.tennis_state
    return (
      <Text size="xs" ff="monospace" c="dimmed">
        盘 {s.home_sets ?? 0}-{s.away_sets ?? 0} · set {s.set ?? '?'} · game {s.game ?? '?'}
        {s.server ? ` · 发球 ${s.server}` : ''}
      </Text>
    )
  }
  if (lg.soccer_state) {
    const s = lg.soccer_state
    const evs = Array.isArray(s.match_events) ? s.match_events.length : 0
    return <Text size="xs" ff="monospace" c="dimmed">半 {s.half ?? '?'} · {s.minute ?? '?'} 分 · {evs} 事件</Text>
  }
  return null
}

type Props = {
  position: PositionRow
  onForceExit: () => void
}

export function PositionAnalysisCard({ position, onForceExit }: Props) {
  const navigate = useNavigate()
  const tokenId = position.token_id

  // 实时拉所有数据源
  const orderbookQuery = useQuery({
    queryKey: qk.markets.orderbook(tokenId),
    queryFn: ({ signal }) => marketsApi.orderbook({ token_id: tokenId }, signal),
    enabled: !!tokenId && !position.redeemable,
    refetchInterval: 5000,
  })

  const liquidityQuery = useQuery({
    queryKey: ['markets', 'liquidity', tokenId],
    queryFn: ({ signal }) => marketsApi.liquidity({ token_id: tokenId }, signal),
    enabled: !!tokenId && !position.redeemable,
    refetchInterval: 5000,
  })

  const liveStateQuery = useQuery({
    queryKey: ['live-states', 'all'],
    queryFn: ({ signal }) => sportsApi.liveStates({ limit: 100 }, signal),
    refetchInterval: 5000,
    staleTime: 4000,
  })
  const liveState: LiveStateRow | undefined = liveStateQuery.data?.items.find(
    (it) => it.condition_id === position.condition_id,
  )

  const priceHistoryQuery = useQuery({
    queryKey: ['markets', 'prices-history', tokenId, '1h'],
    queryFn: ({ signal }) =>
      marketsApi.pricesHistory({ token_id: tokenId, interval: '1h' as const }, signal),
    enabled: !!tokenId && !position.redeemable,
    refetchInterval: 30_000,
    staleTime: 25_000,
  })

  const decisionQuery = useQuery({
    queryKey: qk.decisions.list({ condition_id: position.condition_id, limit: 10 }),
    queryFn: ({ signal }) =>
      decisionsApi.list({ condition_id: position.condition_id, limit: 10 }, signal),
    enabled: !!position.condition_id,
    refetchInterval: 5000,
  })

  const ob: OrderbookSnapshot | undefined = orderbookQuery.data?.orderbook
  const liq: MarketLiquidity | undefined = liquidityQuery.data as MarketLiquidity | undefined
  const ph: PricesHistory | undefined = priceHistoryQuery.data
  const md = (liveState?.metadata as Record<string, unknown> | undefined) ?? {}
  const lg = md.live_game as (Record<string, unknown> & {
    home_name?: string
    away_name?: string
    home_score?: number
    away_score?: number
    status?: string
    period?: string
    raw_status?: string
    league?: string
    sport?: string
    kind?: string
    observed_at?: string
    server_clock_at?: string
    event_name?: string
    source?: string
    source_event_id?: string
    seconds_remaining?: number | null
    contributing_sources?: string[]
    source_conflicts?: string[]
  }) | undefined
  const gml = md.goalserve_moneyline as
    | { home_implied_prob?: number; away_implied_prob?: number; suspended?: boolean; home_suspended?: boolean; away_suspended?: boolean; home_eu?: number; away_eu?: number }
    | undefined
  const gsp = md.goalserve_spread as
    | {
        home_implied_prob?: number
        away_implied_prob?: number
        home_handicap?: number
        away_handicap?: number
        suspended?: boolean
        home_suspended?: boolean
        away_suspended?: boolean
      }
    | undefined
  const gtot = md.goalserve_totals as
    | {
        over_implied_prob?: number
        under_implied_prob?: number
        total_line?: number
        over_suspended?: boolean
        under_suspended?: boolean
        suspended?: boolean
      }
    | undefined
  const ght = md.goalserve_halftime as
    | { home_implied_prob?: number; away_implied_prob?: number; suspended?: boolean }
    | undefined
  const signalAllowed = (liveState as { live_state_signal_allowed?: boolean | null })?.live_state_signal_allowed
  const signalReason = (liveState as { live_state_signal_reason?: string | null })?.live_state_signal_reason

  // 不平衡 ratio
  const bidAskImbalance =
    liq?.total_bid_size != null && liq?.total_ask_size != null && Number(liq.total_ask_size) > 0
      ? Number(liq.total_bid_size) / Number(liq.total_ask_size)
      : null

  // 最新决策器输出
  const latestDecision =
    decisionQuery.data?.items?.find((d) => d.token_id === tokenId) ?? decisionQuery.data?.items?.[0]
  const kelly: KellyInternals | undefined = latestDecision?.decision_output?.metadata?.kelly
  const decisionAgeS = latestDecision?.created_at
    ? Math.floor((Date.now() - new Date(latestDecision.created_at).getTime()) / 1000)
    : null

  const cashPnl = position.cash_pnl
  const cashPnlTone = pnlTone(cashPnl)

  // observed_at > 5min 即视为 stale: Goalserve 对已结算赛事停止推送 inplay,
  // 缓存的 status="live"/score 等是冻结值,不再反映真实状态——必须显式标 stale
  // 否则 UX 把"3h 前的 live"显示成进行中误导操盘.
  const observedAgeS = lg?.observed_at
    ? Math.floor((Date.now() - new Date(lg.observed_at).getTime()) / 1000)
    : null
  const isStaleSnapshot = observedAgeS != null && observedAgeS > 300
  const gameMeta = isStaleSnapshot
    ? ({ tone: 'neutral' as const, text: 'stale·缓存' })
    : gameStatusLabel(lg?.status)

  let statusLabel: { tone: 'success' | 'warning' | 'neutral' | 'danger'; text: string }
  const isAnalyzing = !position.redeemable && !position.settled_zero_value
  if (position.settled_zero_value) {
    statusLabel = { tone: 'neutral', text: '归零' }
  } else if (position.redeemable) {
    statusLabel = { tone: 'warning', text: '待赎回·决策器停止评估' }
  } else if (position.is_paused) {
    statusLabel = { tone: 'warning', text: '人工暂停' }
  } else {
    statusLabel = { tone: 'success', text: '决策器接管中' }
  }

  // sparkline points
  const sparkPoints = (ph?.history ?? []).map((h) => Number(h.price)).filter((n) => Number.isFinite(n))

  return (
    <SectionCard
      title={
        <Group gap={8} wrap="nowrap" align="center">
          {isAnalyzing ? (
            <Tooltip label="WS 订阅活跃 · market_tick 实时驱动 quant_decide" withArrow>
              <Box style={{ width: 10, height: 10, borderRadius: '50%', background: '#5cd9c5', boxShadow: '0 0 8px #5cd9c5' }} />
            </Tooltip>
          ) : null}
          <Text fw={600}>{position.outcome ?? '—'}</Text>
          <Text size="xs" c="dimmed">@</Text>
          <Anchor
            size="xs"
            onClick={() => navigate(`/investigate/timeline?condition_id=${encodeURIComponent(position.condition_id)}`)}
            style={{ cursor: 'pointer' }}
          >
            {position.market_slug ?? position.condition_id}
          </Anchor>
          <StatusPill tone={statusLabel.tone} size="xs">{statusLabel.text}</StatusPill>
        </Group>
      }
    >
      <Stack gap="sm">
        <Text size="xs" c="dimmed">{position.event_title ?? '—'}</Text>

        {/* ═══ Section 1: 比赛 + 全赔率源 (输入数据) ═══════════ */}
        <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
          {/* 比赛实时 */}
          <Stack gap={4}>
            <Text size="xs" fw={600} c="dimmed">⚽ 比赛实时</Text>
            <Group gap={6} wrap="nowrap" align="baseline">
              <Text size="lg" ff="monospace" fw={500}>
                {lg?.home_score ?? '—'} : {lg?.away_score ?? '—'}
              </Text>
              <StatusPill tone={gameMeta.tone} size="xs">{gameMeta.text}</StatusPill>
              {isStaleSnapshot ? (
                <Tooltip
                  label={`Goalserve 已停止推送本事件 ${Math.floor((observedAgeS ?? 0) / 60)} 分钟·缓存数据冻结·真实状态可能已结束`}
                  withArrow
                  multiline
                  w={280}
                >
                  <span><StatusPill tone="warning" size="xs">数据陈旧</StatusPill></span>
                </Tooltip>
              ) : null}
              {lg?.period && lg.period !== lg.raw_status ? (
                <Text size="xs" c="dimmed">{lg.period}</Text>
              ) : null}
              {lg?.seconds_remaining != null ? (
                <Text size="xs" c="dimmed">{lg.seconds_remaining}s 剩</Text>
              ) : null}
            </Group>
            <Text size="xs" c="dimmed">
              {lg ? `${lg.home_name ?? ''} vs ${lg.away_name ?? ''}` : '—'}
            </Text>
            <Group gap={6}>
              <Text size="xs" c="dimmed">{lg?.league ?? '—'}</Text>
              <Text size="xs" c="dimmed">·</Text>
              <Text size="xs" c="dimmed">{lg?.sport ?? '—'}</Text>
              <Text size="xs" c="dimmed">·</Text>
              <Text size="xs" c="dimmed">{lg?.kind ?? '—'}</Text>
            </Group>
            <SportStateLine live_game={lg as Record<string, unknown> | undefined} />
            <Group gap={8}>
              <Text size="xs" c="dimmed">raw: {lg?.raw_status ?? '—'}</Text>
              <Text size="xs" c="dimmed">观测: {ageStr(lg?.observed_at)}</Text>
              {lg?.source ? <Text size="xs" c="dimmed">源: {lg.source}</Text> : null}
              {lg?.source_event_id ? <Text size="xs" c="dimmed">id: {lg.source_event_id}</Text> : null}
            </Group>
            {lg?.contributing_sources?.length ? (
              <Text size="xs" c="dimmed">+{lg.contributing_sources.join(', ')}</Text>
            ) : null}
            {lg?.source_conflicts?.length ? (
              <Text size="xs" c="red">冲突: {lg.source_conflicts.join(', ')}</Text>
            ) : null}
            <Group gap={4}>
              <Text size="xs" c="dimmed">signal:</Text>
              <StatusPill
                tone={signalAllowed === true ? 'success' : signalAllowed === false ? 'warning' : 'neutral'}
                size="xs"
              >
                {signalAllowed === true ? '允许' : signalAllowed === false ? '跳过' : '—'}
              </StatusPill>
              {signalReason ? <Text size="xs" c="dimmed">{String(signalReason)}</Text> : null}
            </Group>
          </Stack>

          {/* Goalserve 全 4 类赔率 */}
          <Stack gap={6}>
            <Text size="xs" fw={600} c="dimmed">📊 Goalserve 赔率源(quant_decider 输入)</Text>
            <OddsBadgePair
              market="ML"
              homeLabel="主"
              awayLabel="客"
              homeProb={gml?.home_implied_prob}
              awayProb={gml?.away_implied_prob}
              suspended={gml?.suspended}
              homeSuspended={gml?.home_suspended}
              awaySuspended={gml?.away_suspended}
              extras={gml?.home_eu != null ? `欧赔 ${gml.home_eu}/${gml.away_eu}` : undefined}
            />
            <OddsBadgePair
              market="Spread"
              homeLabel={`主${gsp?.home_handicap != null ? `(${gsp.home_handicap >= 0 ? '+' : ''}${gsp.home_handicap})` : ''}`}
              awayLabel={`客${gsp?.away_handicap != null ? `(${gsp.away_handicap >= 0 ? '+' : ''}${gsp.away_handicap})` : ''}`}
              homeProb={gsp?.home_implied_prob}
              awayProb={gsp?.away_implied_prob}
              suspended={gsp?.suspended}
              homeSuspended={gsp?.home_suspended}
              awaySuspended={gsp?.away_suspended}
            />
            <OddsBadgePair
              market="Totals"
              homeLabel="大"
              awayLabel="小"
              homeProb={gtot?.over_implied_prob}
              awayProb={gtot?.under_implied_prob}
              suspended={gtot?.suspended}
              homeSuspended={gtot?.over_suspended}
              awaySuspended={gtot?.under_suspended}
              extras={gtot?.total_line != null ? `O/U ${gtot.total_line}` : undefined}
            />
            <OddsBadgePair
              market="Halftime"
              homeLabel="主"
              awayLabel="客"
              homeProb={ght?.home_implied_prob}
              awayProb={ght?.away_implied_prob}
              suspended={ght?.suspended}
            />
          </Stack>
        </SimpleGrid>

        <Divider variant="dashed" />

        {/* ═══ Section 2: Polymarket 盘口可观测 ═══════════════ */}
        <Stack gap={4}>
          <Group gap={6} align="baseline">
            <Text size="xs" fw={600} c="dimmed">📈 Polymarket 盘口</Text>
            {ob?.snapshot_age_ms != null ? (
              <Text size="xs" c="dimmed">
                snapshot {ob.snapshot_age_ms < 1000 ? `${ob.snapshot_age_ms}ms` : `${(ob.snapshot_age_ms / 1000).toFixed(1)}s`} 前
              </Text>
            ) : null}
            {ob?.last_trade_price != null ? (
              <Text size="xs" c="dimmed">last trade {fmtNum(ob.last_trade_price, 3)}</Text>
            ) : null}
            {ob?.tick_size != null ? (
              <Text size="xs" c="dimmed">tick {fmtNum(ob.tick_size, 3)}</Text>
            ) : null}
          </Group>

          <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
            <Stack gap={0}>
              <Text size="xs" c="dimmed">best bid · ask</Text>
              <Text size="sm" ff="monospace">
                {fmtNum(ob?.best_bid, 3)} × {fmtNum(ob?.best_bid_size, 0)}
              </Text>
              <Text size="sm" ff="monospace">
                {fmtNum(ob?.best_ask, 3)} × {fmtNum(ob?.best_ask_size, 0)}
              </Text>
            </Stack>
            <Stack gap={0}>
              <Text size="xs" c="dimmed">spread / eff bps</Text>
              <Text size="sm" ff="monospace">
                {fmtNum(ob?.spread, 3)} / {liq?.effective_spread_bps != null ? fmtNum(liq.effective_spread_bps, 0) : '—'} bps
              </Text>
              <Text size="xs" c="dimmed">
                microprice {fmtNum(ob?.microprice, 4)}
              </Text>
            </Stack>
            <Stack gap={0}>
              <Text size="xs" c="dimmed">VWAP</Text>
              <Text size="xs" ff="monospace">mid {fmtNum(liq?.vwap_mid, 4)}</Text>
              <Text size="xs" ff="monospace">bid {fmtNum(liq?.vwap_bid, 4)} / ask {fmtNum(liq?.vwap_ask, 4)}</Text>
            </Stack>
            <Stack gap={0}>
              <Tooltip
                label={`total bid ${liq?.total_bid_size ?? '—'} / ask ${liq?.total_ask_size ?? '—'}`}
                withArrow
              >
                <Text size="xs" c="dimmed">imbalance</Text>
              </Tooltip>
              <Text
                size="sm"
                ff="monospace"
                style={{
                  color:
                    bidAskImbalance == null
                      ? undefined
                      : bidAskImbalance > 1.2
                        ? '#5cd9c5'
                        : bidAskImbalance < 0.83
                          ? '#ff9f5b'
                          : undefined,
                }}
              >
                {bidAskImbalance != null ? `${bidAskImbalance.toFixed(2)}× (买/卖)` : '—'}
              </Text>
              <Group gap={4} mt={2}>
                <Tooltip label={`bid: ${ob?.bid_liquidity_state ?? '—'}`} withArrow>
                  <span><StatusPill tone={ob?.buy_actionable ? 'success' : 'neutral'} size="xs">BUY {ob?.buy_actionable ? '✓' : '×'}</StatusPill></span>
                </Tooltip>
                <Tooltip label={`ask: ${ob?.ask_liquidity_state ?? '—'}`} withArrow>
                  <span><StatusPill tone={ob?.sell_actionable ? 'success' : 'neutral'} size="xs">SELL {ob?.sell_actionable ? '✓' : '×'}</StatusPill></span>
                </Tooltip>
              </Group>
            </Stack>
          </SimpleGrid>

          {/* depth table (使用 liquidity.bid_depth / ask_depth, 有 cumulative_usdc) */}
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md" mt={4}>
            <Stack gap={2}>
              <Text size="xs" c="dimmed">bid 深度 (累计 USDC)</Text>
              {(liq?.bid_depth ?? []).slice(0, 5).map((t, i) => (
                <Text key={i} size="xs" ff="monospace" style={{ color: '#5cd9c5' }}>
                  {fmtNum(t.price, 3)} × {fmtNum(t.size, 0)} → ${fmtNum(t.cumulative_usdc, 0)}
                </Text>
              ))}
              {(!liq?.bid_depth || liq.bid_depth.length === 0) && <Text size="xs" c="dimmed">—</Text>}
            </Stack>
            <Stack gap={2}>
              <Text size="xs" c="dimmed">ask 深度 (累计 USDC)</Text>
              {(liq?.ask_depth ?? []).slice(0, 5).map((t, i) => (
                <Text key={i} size="xs" ff="monospace" style={{ color: '#ff9f5b' }}>
                  {fmtNum(t.price, 3)} × {fmtNum(t.size, 0)} → ${fmtNum(t.cumulative_usdc, 0)}
                </Text>
              ))}
              {(!liq?.ask_depth || liq.ask_depth.length === 0) && <Text size="xs" c="dimmed">—</Text>}
            </Stack>
          </SimpleGrid>

          {/* sparkline */}
          {sparkPoints.length > 1 ? (
            <Box mt={4}>
              <Group gap={6} mb={2}>
                <Text size="xs" c="dimmed">价格 1h</Text>
                <Text size="xs" ff="monospace">
                  {fmtNum(sparkPoints[0], 3)} → {fmtNum(sparkPoints[sparkPoints.length - 1], 3)}
                </Text>
                <Text size="xs" c="dimmed">
                  min {fmtNum(Math.min(...sparkPoints), 3)} / max {fmtNum(Math.max(...sparkPoints), 3)}
                </Text>
              </Group>
              <Sparkline points={sparkPoints} />
            </Box>
          ) : null}
        </Stack>

        <Divider variant="dashed" />

        {/* ═══ Section 3: 持仓 + 量化决策器输出 ═══════════════ */}
        <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
          <Stack gap={4}>
            <Text size="xs" fw={600} c="dimmed">💰 持仓</Text>
            <Group gap={16}>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">shares</Text>
                <Text ff="monospace" fw={500}>{fmtNum(position.shares, 2)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">cost</Text>
                <Text ff="monospace">{fmtUsdc(position.cost_usdc)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">cur</Text>
                <Text ff="monospace">{fmtNum(position.cur_price, 4)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">mtm</Text>
                <Text ff="monospace" fw={500}>{fmtUsdc(position.position_usdc)}</Text>
              </Stack>
            </Group>
            <Group gap={16} mt={4}>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">cash pnl</Text>
                <Text ff="monospace" fw={600} style={{ color: pnlColor(cashPnlTone) }}>
                  {fmtUsdc(cashPnl)}
                </Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">%</Text>
                <Text ff="monospace" style={{ color: pnlColor(cashPnlTone) }}>
                  {fmtPp(position.percent_pnl != null ? Number(position.percent_pnl) / 100 : null)}
                </Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">realized</Text>
                <Text ff="monospace">{fmtUsdc(position.realized_pnl)}</Text>
              </Stack>
            </Group>
            <CopyableId value={position.token_id} dense label="tok" />
          </Stack>

          <Stack gap={4}>
            <Group gap={6}>
              <Text size="xs" fw={600} c="dimmed">🧠 最新决策器输出</Text>
              {decisionAgeS != null ? <Text size="xs" c="dimmed">{decisionAgeS}s 前</Text> : null}
            </Group>
            {latestDecision ? (
              <>
                <Group gap={6}>
                  <StatusPill tone={latestDecision.accepted ? 'success' : 'neutral'} size="xs">
                    {latestDecision.accepted ? (latestDecision.decision_output?.action ?? 'accepted') : 'skip'}
                  </StatusPill>
                  <Text size="xs" ff="monospace" c="dimmed">{latestDecision.reason ?? '—'}</Text>
                </Group>
                {kelly && kelly.prob_p != null ? (
                  <>
                    <Group gap={12} mt={4}>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">prob_p</Text>
                        <Text size="sm" ff="monospace">{fmtPct(kelly.prob_p, 2)}</Text>
                      </Stack>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">price_c</Text>
                        <Text size="sm" ff="monospace">{fmtNum(kelly.price_c, 3)}</Text>
                      </Stack>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">edge_net</Text>
                        <Tooltip
                          label={`gross=${fmtPp(kelly.edge_gross)} · fee=${kelly.fee_per_share_usdc ?? '—'}`}
                          withArrow
                        >
                          <span><StatusPill tone={edgeTone(kelly.edge_net)} size="xs">{fmtPp(kelly.edge_net)}</StatusPill></span>
                        </Tooltip>
                      </Stack>
                    </Group>
                    <Group gap={12}>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">f_star</Text>
                        <Text size="sm" ff="monospace">{fmtPct(kelly.f_star, 1)}</Text>
                      </Stack>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">eff frac</Text>
                        <Text size="sm" ff="monospace">{fmtPct(kelly.effective_kelly_fraction, 1)}</Text>
                      </Stack>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">budget</Text>
                        <Text size="sm" ff="monospace">{fmtUsdc(kelly.buy_budget_usdc)}</Text>
                      </Stack>
                    </Group>
                    {kelly.capped_by || kelly.is_round_up_overbet ? (
                      <Text size="xs" c="dimmed">
                        {kelly.capped_by ? `capped: ${kelly.capped_by}` : ''}
                        {kelly.is_round_up_overbet ? ' · round-up overbet' : ''}
                      </Text>
                    ) : null}
                  </>
                ) : (
                  <Text size="xs" c="dimmed">本次决策无 Kelly 内核(无真概率信号)</Text>
                )}
              </>
            ) : (
              <Text size="xs" c="dimmed">无决策记录</Text>
            )}
            <Group gap={4} mt="auto">
              <InlineActionButton
                variant="link"
                onClick={() =>
                  navigate(
                    `/investigate/decisions?condition_id=${encodeURIComponent(position.condition_id)}${tokenId ? `&token_id=${encodeURIComponent(tokenId)}` : ''}`,
                  )
                }
              >
                时序 →
              </InlineActionButton>
              {!position.redeemable && (
                <InlineActionButton variant="danger" onClick={onForceExit}>
                  force exit
                </InlineActionButton>
              )}
            </Group>
          </Stack>
        </SimpleGrid>
      </Stack>
    </SectionCard>
  )
}
