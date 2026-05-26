import { useQuery } from '@tanstack/react-query'
import { Anchor, Group, SimpleGrid, Stack, Text } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import { qk } from '@core/api/keys'
import { analyticsApi, healthApi } from '@core/api/resources'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { formatPercent } from '@shared/format'

// 操盘指挥中心——一站汇总当前 quant 状态.
// 数据源:
// - /analytics/quant-summary: 决策计数 + Kelly 内核 + 执行 + portfolio + 信号源覆盖
// - /health: 4 维度状态
// 详情下钻走 nav 各专门页面.

const REFRESH_INTERVAL_MS = 5000

function fmtPct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function fmtNum(value: number | null | undefined, digits = 4): string {
  if (value == null) return '—'
  return value.toFixed(digits)
}

function fmtUsdc(value: string | undefined): string {
  if (!value) return '$—'
  const n = Number(value)
  if (!Number.isFinite(n)) return value
  return `$${n.toFixed(2)}`
}

export function LiveOverviewPage() {
  const navigate = useNavigate()
  const health = useQuery({
    queryKey: qk.health(),
    queryFn: ({ signal }) => healthApi.health(signal),
    refetchInterval: REFRESH_INTERVAL_MS,
  })
  const summary = useQuery({
    queryKey: ['analytics', 'quant-summary', 86_400_000],
    queryFn: ({ signal }) => analyticsApi.quantSummary({ window_ms: 86_400_000 }, signal),
    refetchInterval: REFRESH_INTERVAL_MS,
  })

  const s = summary.data
  const h = health.data

  return (
    <>
      <PageHeader title="操盘指挥中心" subtitle="量化决策器全景 · 实时刷新 · 详情下钻" />

      {summary.error ? <QueryErrorNotice error={summary.error} /> : null}

      <Stack gap="md">
        {/* 健康 + 持仓汇总 */}
        <SectionCard title={`系统 / 持仓 · ${h?.status ?? '—'}`}>
          <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
            <Stat label="健康" value={h?.status ?? '—'} tone={healthTone(h?.status)} />
            <Stat label="balance" value={fmtUsdc(s?.portfolio.balance_usdc)} />
            <Stat label="net value" value={fmtUsdc(s?.portfolio.net_value_usdc)} />
            <Stat
              label="cash pnl"
              value={fmtUsdc(s?.portfolio.cash_pnl_usdc)}
              tone={pnlTone(s?.portfolio.cash_pnl_usdc)}
            />
            <Stat label="持仓" value={String(s?.portfolio.position_count ?? '—')} />
            <Stat label="挂单" value={String(s?.portfolio.open_order_count ?? '—')} />
            <Stat label="paused" value={String(s?.portfolio.paused_market_count ?? 0)} />
            <Stat label="cost" value={fmtUsdc(s?.portfolio.cost_usdc)} />
          </SimpleGrid>
          <Group justify="flex-end" mt="sm">
            <Anchor size="xs" onClick={() => navigate('/positions')}>持仓详情 →</Anchor>
            <Anchor size="xs" onClick={() => navigate('/portfolio')}>portfolio →</Anchor>
            <Anchor size="xs" onClick={() => navigate('/system/health')}>health →</Anchor>
          </Group>
        </SectionCard>

        {/* 决策计数 + 拒绝 top */}
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
          <SectionCard title="决策计数 (24h)">
            <SimpleGrid cols={3} spacing="md">
              <Stat label="总评估" value={String(s?.decision_counts.total ?? '—')} />
              <Stat
                label="accepted"
                value={String(s?.decision_counts.accepted ?? '—')}
                tone="pos"
              />
              <Stat label="rejected" value={String(s?.decision_counts.rejected ?? '—')} />
            </SimpleGrid>
            <Text size="xs" c="dimmed" mt="xs">
              接受率: {s ? formatPercent(s.decision_counts.accept_rate_pct / 100) : '—'}
            </Text>
            <Group justify="flex-end" mt="xs">
              <Anchor size="xs" onClick={() => navigate('/investigate/decisions')}>决策记录 →</Anchor>
              <Anchor size="xs" onClick={() => navigate('/analytics/funnel')}>漏斗 →</Anchor>
            </Group>
          </SectionCard>

          <SectionCard title="拒绝原因 top 5">
            <Stack gap={6}>
              {!s?.rejection_top.length ? (
                <Text size="xs" c="dimmed">无拒绝</Text>
              ) : (
                s.rejection_top.map((r) => (
                  <Group key={r.reason} justify="space-between">
                    <Text size="sm">{r.reason}</Text>
                    <Group gap={6}>
                      <Text size="sm" fw={600}>{r.count}</Text>
                      <Text size="xs" c="dimmed">{r.pct.toFixed(1)}%</Text>
                    </Group>
                  </Group>
                ))
              )}
            </Stack>
            <Group justify="flex-end" mt="sm">
              <Anchor size="xs" onClick={() => navigate('/analytics/rejections')}>拒绝分析 →</Anchor>
            </Group>
          </SectionCard>
        </SimpleGrid>

        {/* Kelly 内核 + 执行 */}
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
          <SectionCard title={`Kelly 内核 (n=${s?.kelly_stats.sample_count ?? 0})`}>
            <SimpleGrid cols={2} spacing="xs">
              <Stat label="avg prob_p" value={fmtPct(s?.kelly_stats.avg_prob_p)} />
              <Stat label="avg price_c" value={fmtPct(s?.kelly_stats.avg_price_c)} />
              <Stat label="avg edge_net" value={fmtPct(s?.kelly_stats.avg_edge_net)} />
              <Stat label="avg f_star" value={fmtNum(s?.kelly_stats.avg_f_star, 4)} />
              <Stat label="avg budget" value={fmtUsdc(s?.kelly_stats.avg_buy_budget_usdc?.toFixed(2))} />
              <Stat
                label="round-up overbet"
                value={`${s?.kelly_stats.rounded_up_count ?? 0} (${(s?.kelly_stats.rounded_up_pct ?? 0).toFixed(1)}%)`}
              />
            </SimpleGrid>
          </SectionCard>

          <SectionCard title="执行 (24h)">
            <SimpleGrid cols={2} spacing="xs">
              <Stat label="submitted" value={String(s?.execution.orders_submitted ?? '—')} />
              <Stat label="filled" value={String(s?.execution.orders_filled ?? '—')} />
              <Stat label="rejected" value={String(s?.execution.orders_rejected ?? '—')} />
              <Stat label="fill rate" value={fmtPct((s?.execution.fill_rate_pct ?? 0) / 100)} />
            </SimpleGrid>
            <Group justify="flex-end" mt="sm">
              <Anchor size="xs" onClick={() => navigate('/orders')}>订单 →</Anchor>
              <Anchor size="xs" onClick={() => navigate('/fills')}>成交 →</Anchor>
              <Anchor size="xs" onClick={() => navigate('/analytics/execution-quality')}>延迟 →</Anchor>
            </Group>
          </SectionCard>
        </SimpleGrid>

        {/* 信号源覆盖 + 健康 */}
        <SectionCard title="信号源覆盖 + WS">
          <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
            <Stat
              label="registry markets"
              value={String(s?.market_coverage.registry_total ?? '—')}
            />
            <Stat
              label="有 live state"
              value={String(s?.market_coverage.with_live_state ?? '—')}
              tone={s?.market_coverage.with_live_state ? 'pos' : 'neutral'}
            />
            <Stat
              label="signal allowed"
              value={String(s?.market_coverage.signal_allowed ?? '—')}
              tone={s?.market_coverage.signal_allowed ? 'pos' : 'neutral'}
            />
            <Stat
              label="WS 订阅"
              value={String(s?.signal_health.ws_subscribed_tokens ?? '—')}
            />
          </SimpleGrid>
          <Group justify="flex-end" mt="sm">
            <Anchor size="xs" onClick={() => navigate('/live/goalserve')}>直播看板 →</Anchor>
            <Anchor size="xs" onClick={() => navigate('/live/candidates')}>候选列表 →</Anchor>
            <Anchor size="xs" onClick={() => navigate('/investigate/sports-events')}>事件流 →</Anchor>
          </Group>
        </SectionCard>

        <Text size="xs" c="dimmed" ta="center">
          所有数据来自 <code>/analytics/quant-summary</code> + <code>/health</code> · 5s 刷新 ·
          最后更新 {summary.dataUpdatedAt ? new Date(summary.dataUpdatedAt).toLocaleTimeString() : '—'}
        </Text>
      </Stack>
    </>
  )
}

function healthTone(status: string | undefined): 'pos' | 'neg' | 'neutral' {
  if (status === 'healthy') return 'pos'
  if (status === 'unhealthy') return 'neg'
  return 'neutral'
}

function pnlTone(value: string | undefined): 'pos' | 'neg' | 'neutral' {
  if (!value) return 'neutral'
  const n = Number(value)
  if (!Number.isFinite(n) || n === 0) return 'neutral'
  return n > 0 ? 'pos' : 'neg'
}

function Stat({
  label,
  value,
  tone = 'neutral',
}: {
  label: string
  value: string
  tone?: 'pos' | 'neg' | 'neutral'
}) {
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed">{label}</Text>
      <Group gap={4} align="baseline">
        {tone === 'neutral' ? (
          <Text size="md" fw={600}>{value}</Text>
        ) : (
          <StatusPill tone={tone === 'pos' ? 'success' : 'danger'} size="sm">{value}</StatusPill>
        )}
      </Group>
    </Stack>
  )
}
