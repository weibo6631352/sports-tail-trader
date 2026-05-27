import { useQuery } from '@tanstack/react-query'
import { Anchor, Box, Group, SimpleGrid, Stack, Text, Tooltip } from '@mantine/core'
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

// 单仓位实时分析卡——比赛/持仓/订单簿/量化 一站可视化, 5s 自动刷新.
// 操盘人验证量化决策正确性的"凭证页": 看到所有输入(盘口/赔率/比分),
// 看到所有输出(Kelly 内核 / 最新决策 reason / accepted), 判断决策器
// 行为是否符合预期.

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

// 轻量 SVG sparkline,无外部依赖.
function Sparkline({ points, width = 200, height = 32 }: { points: number[]; width?: number; height?: number }) {
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
  // 末值 vs 首值, 决定颜色: 涨绿 / 跌橙 / 平灰
  const last = points[points.length - 1]
  const first = points[0]
  const color = last > first ? '#5cd9c5' : last < first ? '#ff9f5b' : '#97a6c2'
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
      <path d={path} stroke={color} strokeWidth={1.2} fill="none" />
    </svg>
  )
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

type Props = {
  position: PositionRow
  onForceExit: () => void
}

export function PositionAnalysisCard({ position, onForceExit }: Props) {
  const navigate = useNavigate()
  const tokenId = position.token_id

  // 实时拉 5 类数据, 5s 刷新.
  // orderbook/liquidity 是同一 store 派生, 但 liquidity 含 VWAP+depth_cumulative
  // 分析字段, orderbook 含 top_5 价格. 两者互补, 都拉.
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

  // 1h 价格历史 sparkline. interval=1h 自动取最近一小时的 minute-level 数据.
  const priceHistoryQuery = useQuery({
    queryKey: ['markets', 'prices-history', tokenId, '1h'],
    queryFn: ({ signal }) =>
      marketsApi.pricesHistory({ token_id: tokenId, interval: '1h' as const }, signal),
    enabled: !!tokenId && !position.redeemable,
    refetchInterval: 30_000,  // 历史变化慢, 30s 一次足够
    staleTime: 25_000,
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

  // /admin/decisions/dump 无 token_id 过滤,按 condition_id 拉 10 条客户端筛.
  const decisionQuery = useQuery({
    queryKey: qk.decisions.list({
      condition_id: position.condition_id,
      limit: 10,
    }),
    queryFn: ({ signal }) =>
      decisionsApi.list(
        { condition_id: position.condition_id, limit: 10 },
        signal,
      ),
    enabled: !!position.condition_id,
    refetchInterval: 5000,
  })

  const ob: OrderbookSnapshot | undefined = orderbookQuery.data?.orderbook
  const liq: MarketLiquidity | undefined = liquidityQuery.data as MarketLiquidity | undefined
  const ph: PricesHistory | undefined = priceHistoryQuery.data
  // bid/ask size 不平衡: > 1 表示买压 > 卖压
  const bidAskImbalance =
    liq?.total_bid_size != null && liq?.total_ask_size != null && Number(liq.total_ask_size) > 0
      ? Number(liq.total_bid_size) / Number(liq.total_ask_size)
      : null
  const lg = (liveState?.metadata as Record<string, unknown> | undefined)?.live_game as
    | { home_name?: string; away_name?: string; home_score?: number; away_score?: number; status?: string; period?: string; league?: string }
    | undefined
  const gml = (liveState?.metadata as Record<string, unknown> | undefined)?.goalserve_moneyline as
    | { home_implied_prob?: number; away_implied_prob?: number; suspended?: boolean; home_suspended?: boolean; away_suspended?: boolean }
    | undefined
  const signalAllowed = (liveState as { live_state_signal_allowed?: boolean | null })?.live_state_signal_allowed

  // backend 按 created_at DESC 排, 取第 1 个 token_id 匹配的(无则取最新一条).
  const latestDecision =
    decisionQuery.data?.items?.find((d) => d.token_id === tokenId) ??
    decisionQuery.data?.items?.[0]
  const kelly: KellyInternals | undefined = latestDecision?.decision_output?.metadata?.kelly
  const decisionAgeS = latestDecision?.created_at
    ? Math.floor((Date.now() - new Date(latestDecision.created_at).getTime()) / 1000)
    : null

  const cashPnl = position.cash_pnl
  const cashPnlTone = pnlTone(cashPnl)
  const gameMeta = gameStatusLabel(lg?.status)

  // 状态推断
  let statusLabel: { tone: 'success' | 'warning' | 'neutral' | 'danger'; text: string }
  if (position.settled_zero_value) {
    statusLabel = { tone: 'neutral', text: '归零' }
  } else if (position.redeemable) {
    statusLabel = { tone: 'warning', text: '待赎回 · 决策器停止评估' }
  } else if (position.is_paused) {
    statusLabel = { tone: 'warning', text: '人工暂停' }
  } else {
    statusLabel = { tone: 'success', text: '决策器接管中' }
  }
  const isAnalyzing = !position.redeemable && !position.settled_zero_value

  return (
    <SectionCard
      title={
        <Group gap={8} wrap="nowrap" align="center">
          {isAnalyzing ? (
            <Tooltip label="WS 订阅活跃 · market_tick 实时驱动 quant_decide" withArrow>
              <Box
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  background: '#5cd9c5',
                  boxShadow: '0 0 8px #5cd9c5',
                }}
              />
            </Tooltip>
          ) : null}
          <Text fw={600}>{position.outcome ?? '—'}</Text>
          <Text size="xs" c="dimmed">@</Text>
          <Anchor
            size="xs"
            onClick={() =>
              navigate(`/investigate/timeline?condition_id=${encodeURIComponent(position.condition_id)}`)
            }
            style={{ cursor: 'pointer' }}
          >
            {position.market_slug ?? position.condition_id}
          </Anchor>
          <StatusPill tone={statusLabel.tone} size="xs">
            {statusLabel.text}
          </StatusPill>
        </Group>
      }
    >
      <Stack gap="sm">
        <Group gap={4}>
          <Text size="xs" c="dimmed">{position.event_title ?? '—'}</Text>
        </Group>

        <SimpleGrid cols={{ base: 1, md: 4 }} spacing="md">
          {/* === 比赛 ============================================ */}
          <Stack gap={4}>
            <Text size="xs" fw={600} c="dimmed">比赛</Text>
            <Group gap={6} wrap="nowrap">
              <Text size="md" ff="monospace" fw={500}>
                {lg?.home_score ?? '—'} : {lg?.away_score ?? '—'}
              </Text>
              <StatusPill tone={gameMeta.tone} size="xs">{gameMeta.text}</StatusPill>
            </Group>
            <Text size="xs" c="dimmed">
              {lg ? `${lg.home_name ?? ''} vs ${lg.away_name ?? ''}` : '—'}
            </Text>
            <Text size="xs" c="dimmed">联赛: {lg?.league ?? '—'}</Text>
            <Group gap={8} mt={4}>
              <Text size="xs" c="dimmed">Goalserve 赔率:</Text>
              {gml?.suspended ? (
                <StatusPill tone="warning" size="xs">锁盘</StatusPill>
              ) : (
                <Text size="xs" ff="monospace">
                  主 {fmtPct(gml?.home_implied_prob)} / 客 {fmtPct(gml?.away_implied_prob)}
                </Text>
              )}
            </Group>
            <Group gap={4}>
              <Text size="xs" c="dimmed">信号:</Text>
              <StatusPill
                tone={signalAllowed === true ? 'success' : signalAllowed === false ? 'warning' : 'neutral'}
                size="xs"
              >
                {signalAllowed === true ? '允许' : signalAllowed === false ? '跳过' : '—'}
              </StatusPill>
              {liveState?.live_state_signal_reason ? (
                <Text size="xs" c="dimmed">{String(liveState.live_state_signal_reason)}</Text>
              ) : null}
            </Group>
          </Stack>

          {/* === 持仓 ============================================ */}
          <Stack gap={4}>
            <Text size="xs" fw={600} c="dimmed">持仓</Text>
            <Group gap={12}>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">shares</Text>
                <Text ff="monospace">{fmtNum(position.shares, 2)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">cost</Text>
                <Text ff="monospace">{fmtUsdc(position.cost_usdc)}</Text>
              </Stack>
            </Group>
            <Group gap={12}>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">cur price</Text>
                <Text ff="monospace">{fmtNum(position.cur_price, 4)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">mtm value</Text>
                <Text ff="monospace">{fmtUsdc(position.position_usdc)}</Text>
              </Stack>
            </Group>
            <Group gap={12} mt={4}>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">cash pnl</Text>
                <Text ff="monospace" style={{ color: pnlColor(cashPnlTone), fontWeight: 600 }}>
                  {fmtUsdc(cashPnl)}
                </Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">%</Text>
                <Text ff="monospace" style={{ color: pnlColor(cashPnlTone) }}>
                  {fmtPp(position.percent_pnl != null ? Number(position.percent_pnl) / 100 : null)}
                </Text>
              </Stack>
            </Group>
            <Text size="xs" c="dimmed" mt={4}>
              <CopyableId value={position.token_id} dense label="tok" />
            </Text>
          </Stack>

          {/* === 订单簿深度 + 流动性 ============================== */}
          <Stack gap={4}>
            <Group gap={6} align="baseline">
              <Text size="xs" fw={600} c="dimmed">订单簿</Text>
              {ob?.snapshot_age_ms != null ? (
                <Text size="xs" c="dimmed">
                  {ob.snapshot_age_ms < 1000 ? `${ob.snapshot_age_ms}ms` : `${(ob.snapshot_age_ms / 1000).toFixed(1)}s`} 前
                </Text>
              ) : null}
            </Group>
            {/* 行 1: microprice / spread / effective_spread_bps */}
            <Group gap={10}>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">microprice</Text>
                <Text size="xs" ff="monospace">{fmtNum(ob?.microprice, 4)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">spread</Text>
                <Text size="xs" ff="monospace">{fmtNum(ob?.spread, 3)}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">eff bps</Text>
                <Text size="xs" ff="monospace">{liq?.effective_spread_bps ? fmtNum(liq.effective_spread_bps, 0) : '—'}</Text>
              </Stack>
            </Group>
            {/* 行 2: VWAP mid / 不平衡 / 可成交 */}
            <Group gap={10} align="baseline">
              <Stack gap={0}>
                <Text size="xs" c="dimmed">VWAP mid</Text>
                <Text size="xs" ff="monospace">{fmtNum(liq?.vwap_mid, 4)}</Text>
              </Stack>
              <Tooltip
                label={`total bids ${liq?.total_bid_size ?? '—'} / asks ${liq?.total_ask_size ?? '—'}`}
                withArrow
                disabled={bidAskImbalance == null}
              >
                <Stack gap={0}>
                  <Text size="xs" c="dimmed">imbalance</Text>
                  <Text
                    size="xs"
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
                    {bidAskImbalance != null ? `${bidAskImbalance.toFixed(2)}×` : '—'}
                  </Text>
                </Stack>
              </Tooltip>
              <Group gap={4}>
                <Tooltip label={`bid: ${ob?.bid_liquidity_state ?? '—'}`} withArrow>
                  <span>
                    <StatusPill tone={ob?.buy_actionable ? 'success' : 'neutral'} size="xs">
                      BUY {ob?.buy_actionable ? '✓' : '×'}
                    </StatusPill>
                  </span>
                </Tooltip>
                <Tooltip label={`ask: ${ob?.ask_liquidity_state ?? '—'}`} withArrow>
                  <span>
                    <StatusPill tone={ob?.sell_actionable ? 'success' : 'neutral'} size="xs">
                      SELL {ob?.sell_actionable ? '✓' : '×'}
                    </StatusPill>
                  </span>
                </Tooltip>
              </Group>
            </Group>
            {/* 行 3: top 5 bids/asks 价×size */}
            <SimpleGrid cols={2} spacing={4}>
              <Stack gap={2}>
                <Text size="xs" c="dimmed">bids (top 5)</Text>
                {(ob?.top_5_bids ?? []).slice(0, 5).map((b, i) => (
                  <Text key={i} size="xs" ff="monospace" style={{ color: '#5cd9c5' }}>
                    {fmtNum(b.price, 3)} × {fmtNum(b.size, 0)}
                  </Text>
                ))}
                {(!ob?.top_5_bids || ob.top_5_bids.length === 0) && (
                  <Text size="xs" c="dimmed">—</Text>
                )}
              </Stack>
              <Stack gap={2}>
                <Text size="xs" c="dimmed">asks (top 5)</Text>
                {(ob?.top_5_asks ?? []).slice(0, 5).map((a, i) => (
                  <Text key={i} size="xs" ff="monospace" style={{ color: '#ff9f5b' }}>
                    {fmtNum(a.price, 3)} × {fmtNum(a.size, 0)}
                  </Text>
                ))}
                {(!ob?.top_5_asks || ob.top_5_asks.length === 0) && (
                  <Text size="xs" c="dimmed">—</Text>
                )}
              </Stack>
            </SimpleGrid>
            {/* 行 4: 价格 sparkline (1h history) */}
            {ph?.history && ph.history.length > 1 ? (
              <Box mt={4}>
                <Group gap={4} mb={2}>
                  <Text size="xs" c="dimmed">价格 1h</Text>
                  <Text size="xs" ff="monospace">
                    {fmtNum(ph.history[0].price, 3)} → {fmtNum(ph.history[ph.history.length - 1].price, 3)}
                  </Text>
                </Group>
                <Sparkline points={ph.history.map((h) => Number(h.price)).filter((n) => Number.isFinite(n))} />
              </Box>
            ) : null}
          </Stack>

          {/* === 量化决策 ======================================== */}
          <Stack gap={4}>
            <Group gap={6}>
              <Text size="xs" fw={600} c="dimmed">最新决策器输出</Text>
              {decisionAgeS != null ? (
                <Text size="xs" c="dimmed">{decisionAgeS}s 前</Text>
              ) : null}
            </Group>
            {latestDecision ? (
              <>
                <Group gap={6}>
                  <StatusPill
                    tone={latestDecision.accepted ? 'success' : 'neutral'}
                    size="xs"
                  >
                    {latestDecision.accepted
                      ? (latestDecision.decision_output?.action ?? 'accepted')
                      : 'skip'}
                  </StatusPill>
                  <Text size="xs" ff="monospace" c="dimmed">
                    {latestDecision.reason ?? '—'}
                  </Text>
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
                    </Group>
                    <Group gap={12}>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">edge_net</Text>
                        <Tooltip label={`gross=${fmtPp(kelly.edge_gross)} · fee=${kelly.fee_per_share_usdc ?? '—'}`} withArrow>
                          <span>
                            <StatusPill tone={edgeTone(kelly.edge_net)} size="xs">
                              {fmtPp(kelly.edge_net)}
                            </StatusPill>
                          </span>
                        </Tooltip>
                      </Stack>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">f_star</Text>
                        <Text size="sm" ff="monospace">{fmtPct(kelly.f_star, 1)}</Text>
                      </Stack>
                      <Stack gap={0}>
                        <Text size="xs" c="dimmed">budget</Text>
                        <Text size="sm" ff="monospace">{fmtUsdc(kelly.buy_budget_usdc)}</Text>
                      </Stack>
                    </Group>
                    {kelly.capped_by ? (
                      <Text size="xs" c="dimmed">capped: {kelly.capped_by}</Text>
                    ) : null}
                  </>
                ) : (
                  <Text size="xs" c="dimmed">无 Kelly 内核（无概率信号）</Text>
                )}
              </>
            ) : (
              <Text size="xs" c="dimmed">无决策记录</Text>
            )}
            <Group gap={4} mt={4}>
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
