import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, NumberInput, SimpleGrid, Stack, Text } from '@mantine/core'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { qk } from '@core/api/keys'
import { portfolioApi } from '@core/api/resources'
import type { EquityPoint } from '@core/api/types'
import { chartTooltipStyle } from '@shared/charts'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { formatIso, formatUsdc, pnlTone, toDecimal } from '@shared/format'

// 默认窗口与后端 portfolio_history_service 保持一致（24h × 1h 桶）。
// annualization_factor 默认按"1h 桶"折成 ≈ 8760，给 Sharpe-like 年化。
const DEFAULT_WINDOW_MS = 24 * 60 * 60 * 1000
const DEFAULT_INTERVAL_MS = 60 * 60 * 1000

export function PortfolioPage() {
  const [windowMs, setWindowMs] = useState<number>(DEFAULT_WINDOW_MS)
  const [intervalMs, setIntervalMs] = useState<number>(DEFAULT_INTERVAL_MS)

  // 每页打开同时拉三份：snapshot 即时，equity-curve 时序，risk-metrics 标量。
  // 不绑定 SSE 自动失效（避免分析师查看时频繁重画）；用户可点刷新按钮。
  const snapshot = useQuery({
    queryKey: qk.portfolio.snapshot(),
    queryFn: ({ signal }) => portfolioApi.snapshot(signal),
  })
  const curve = useQuery({
    queryKey: qk.portfolio.equity({ window_ms: windowMs, interval_ms: intervalMs }),
    queryFn: ({ signal }) => portfolioApi.equityCurve({ window_ms: windowMs, interval_ms: intervalMs }, signal),
  })

  // annualization_factor: 一年的桶数 = (365×24×3600×1000) / interval_ms。
  const annualizationFactor = intervalMs > 0 ? (365 * 24 * 60 * 60 * 1000) / intervalMs : undefined
  const risk = useQuery({
    queryKey: qk.portfolio.riskMetrics({ window_ms: windowMs, interval_ms: intervalMs, annualization_factor: annualizationFactor }),
    queryFn: ({ signal }) =>
      portfolioApi.riskMetrics(
        {
          window_ms: windowMs,
          interval_ms: intervalMs,
          annualization_factor: annualizationFactor,
        },
        signal,
      ),
  })

  return (
    <>
      <PageHeader
        title="Portfolio 概览"
        subtitle="snapshot · equity curve · 风险指标"
        actions={
          <Group gap="xs" align="flex-end">
            <NumberInput
              size="xs"
              label="window (h)"
              value={windowMs / 3_600_000}
              onChange={(v) => setWindowMs((typeof v === 'number' ? v : 24) * 3_600_000)}
              min={1}
              max={24 * 30}
              step={1}
              w={110}
            />
            <NumberInput
              size="xs"
              label="interval (m)"
              value={intervalMs / 60_000}
              onChange={(v) => setIntervalMs((typeof v === 'number' ? v : 60) * 60_000)}
              min={1}
              max={24 * 60}
              step={5}
              w={110}
            />
          </Group>
        }
      />

      <Stack gap="md">
        <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
          <Stat label="净值 net_value" value={formatUsdc(snapshot.data?.net_value_usdc)} />
          <Stat
            label="实现 PnL"
            value={formatUsdc(snapshot.data?.realized_pnl_usdc)}
            tone={pnlTone(snapshot.data?.realized_pnl_usdc)}
          />
          <Stat
            label="未实现 PnL"
            value={formatUsdc(snapshot.data?.cash_pnl_usdc)}
            tone={pnlTone(snapshot.data?.cash_pnl_usdc)}
          />
          <Stat label="名义敞口" value={formatUsdc(snapshot.data?.notional_usdc)} />
        </SimpleGrid>

        <SectionCard
          title="净值曲线 Equity curve"
          description={`window=${windowMs / 3_600_000}h · interval=${intervalMs / 60_000}m`}
        >
          {curve.error ? (
            <QueryErrorNotice error={curve.error} compact />
          ) : (
            <EquityChart points={curve.data?.points ?? []} />
          )}
        </SectionCard>

        <SectionCard
          title="风险指标 Risk metrics"
          description={
            annualizationFactor
              ? `Sharpe-like 按当前 interval 年化（factor=${annualizationFactor.toFixed(0)}）`
              : undefined
          }
        >
          {risk.error ? (
            <QueryErrorNotice error={risk.error} compact />
          ) : (
            <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
              <Stat
                label="total_return"
                value={fmtPct(risk.data?.metrics?.total_return)}
                tone={returnTone(risk.data?.metrics?.total_return)}
                hint={`样本 ${risk.data?.metrics?.sample_count ?? 0} 个`}
              />
              <Stat
                label="max drawdown"
                value={formatUsdc(risk.data?.metrics?.max_drawdown_pct)}
                tone="neg"
                hint={
                  risk.data?.metrics?.peak_at && risk.data?.metrics?.trough_at
                    ? `${formatIso(risk.data.metrics.peak_at, 'MM-DD HH:mm')} → ${formatIso(risk.data.metrics.trough_at, 'MM-DD HH:mm')}`
                    : '—'
                }
              />
              <Stat
                label="time underwater"
                value={
                  risk.data?.metrics?.time_underwater_seconds !== undefined
                    ? formatSeconds(risk.data.metrics.time_underwater_seconds)
                    : '—'
                }
                hint={
                  risk.data?.metrics?.time_underwater_ratio
                    ? `占比 ${(Number(risk.data.metrics.time_underwater_ratio) * 100).toFixed(1)}%`
                    : undefined
                }
              />
              <Stat
                label="Sharpe-like"
                value={fmtNum(risk.data?.metrics?.sharpe_like, 3)}
                tone={returnTone(risk.data?.metrics?.sharpe_like)}
                hint="mean / stddev × √(annualization)"
              />
              <Stat
                label="mean return / period"
                value={fmtNum(risk.data?.metrics?.mean_return_per_period, 6)}
              />
              <Stat
                label="stddev return / period"
                value={fmtNum(risk.data?.metrics?.stddev_return_per_period, 6)}
                hint="波动率"
              />
              <Stat
                label="start / end net value"
                value={`${formatUsdc(risk.data?.metrics?.start_net_value_usdc)} → ${formatUsdc(risk.data?.metrics?.end_net_value_usdc)}`}
              />
              <Stat
                label="first → last recorded"
                value={
                  risk.data?.metrics?.first_recorded_at && risk.data?.metrics?.last_recorded_at
                    ? `${formatIso(risk.data.metrics.first_recorded_at, 'MM-DD HH:mm')} → ${formatIso(risk.data.metrics.last_recorded_at, 'MM-DD HH:mm')}`
                    : '—'
                }
              />
            </SimpleGrid>
          )}
        </SectionCard>
      </Stack>
    </>
  )
}

function EquityChart({ points }: { points: EquityPoint[] }) {
  if (points.length === 0) {
    return (
      <Text c="dimmed" size="sm">
        当前窗口无数据（可能 portfolio_history_service 还没有 account_snapshots 落库）。
      </Text>
    )
  }
  const data = points.map((p) => ({
    t: p.ts,
    label: formatIso(p.ts, 'MM-DD HH:mm'),
    net_value: toDecimal(p.net_usdc)?.toNumber() ?? 0,
  }))
  return (
    <div style={{ width: '100%', height: 280 }}>
      <ResponsiveContainer>
        <AreaChart data={data}>
          <defs>
            <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#5cd9c5" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#5cd9c5" stopOpacity={0.04} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
          <XAxis dataKey="label" stroke="#97a6c2" tick={{ fontSize: 11 }} minTickGap={40} />
          <YAxis stroke="#97a6c2" tick={{ fontSize: 11 }} domain={['auto', 'auto']} />
          <Tooltip
            contentStyle={chartTooltipStyle}
            formatter={(value) =>
              typeof value === 'number' ? value.toFixed(2) : String(value)
            }
          />
          <Area type="monotone" dataKey="net_value" stroke="#5cd9c5" strokeWidth={2} fill="url(#equityGradient)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'pos' | 'neg' | 'neutral'
}) {
  const color = tone === 'pos' ? 'var(--color-success)' : tone === 'neg' ? 'var(--color-danger)' : undefined
  return (
    <SectionCard>
      <Stack gap={2}>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        <Text size="lg" fw={700} c={color}>
          {value}
        </Text>
        {hint ? (
          <Text size="xs" c="dimmed">
            {hint}
          </Text>
        ) : null}
      </Stack>
    </SectionCard>
  )
}

function fmtPct(v: string | null | undefined): string {
  if (!v) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${(n * 100).toFixed(2)}%`
}

function fmtNum(v: string | null | undefined, dp = 4): string {
  if (!v) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toFixed(dp)
}

function returnTone(v: string | null | undefined): 'pos' | 'neg' | 'neutral' | undefined {
  if (!v) return undefined
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return undefined
  return n > 0 ? 'pos' : 'neg'
}

function formatSeconds(seconds: number): string {
  if (seconds <= 0) return '0s'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}
