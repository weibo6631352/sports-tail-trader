import { useQuery } from '@tanstack/react-query'
import {
  SimpleGrid,
  Stack,
  Group,
  Text,
  Anchor,
  Alert,
  Badge,
  Progress,
  Box,
  Divider,
} from '@mantine/core'
import { IconAlertTriangle, IconCircleCheck } from '@tabler/icons-react'
import { useNavigate } from 'react-router-dom'
import { qk } from '@core/api/keys'
import {
  healthApi,
  portfolioApi,
  marketsApi,
  candidatesApi,
  analyticsApi,
} from '@core/api/resources'
import { useRuntimeIdentity } from '@core/identity/useRuntimeIdentity'
import { resolveStrategyBundle } from '@strategy/registry'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import {
  formatUsdc,
  pnlTone,
  formatDecimal,
  formatPercent,
  toDecimal,
} from '@shared/format'
import type { PortfolioExposureItem } from '@core/api/types'

export function LiveOverviewPage() {
  const navigate = useNavigate()

  const ready = useQuery({
    queryKey: qk.ready(),
    queryFn: ({ signal }) => healthApi.ready(signal),
  })
  const runtime = useQuery({
    queryKey: qk.runtime(),
    queryFn: ({ signal }) => healthApi.runtime(signal),
  })
  const workers = useQuery({
    queryKey: qk.workers(),
    queryFn: ({ signal }) => healthApi.workers(signal),
  })
  const metrics = useQuery({
    queryKey: qk.metrics(),
    queryFn: ({ signal }) => healthApi.metrics(signal),
  })
  const portfolio = useQuery({
    queryKey: qk.portfolio.snapshot(),
    queryFn: ({ signal }) => portfolioApi.snapshot(signal),
  })
  const exposure = useQuery({
    queryKey: qk.portfolio.exposure(),
    queryFn: ({ signal }) => portfolioApi.exposure(signal),
  })
  const latency = useQuery({
    queryKey: qk.latency({ window_ms: null, sample_limit: 500 }),
    queryFn: ({ signal }) => healthApi.latencyPercentiles({ sample_limit: 500 }, signal),
  })
  const pausedMarkets = useQuery({
    queryKey: qk.markets.list({ trading_status: 'paused', limit: 15 }),
    queryFn: ({ signal }) => marketsApi.list({ trading_status: 'paused', limit: 15 }, signal),
  })
  const dataFreshness = useQuery({
    queryKey: qk.candidates.dataFreshness(),
    queryFn: ({ signal }) => candidatesApi.dataFreshness(signal),
  })
  const candidates = useQuery({
    queryKey: qk.candidates.list({ limit: 1 }),
    queryFn: ({ signal }) => candidatesApi.list({ limit: 1 }, signal),
  })
  const execQuality = useQuery({
    queryKey: qk.analytics.executionQuality({ window_ms: 3_600_000 }),
    queryFn: ({ signal }) => analyticsApi.executionQuality({ window_ms: 3_600_000 }, signal),
  })

  const { extensionModule } = useRuntimeIdentity()
  const strategyBundle = resolveStrategyBundle(extensionModule)
  const widgets = strategyBundle.dashboardWidgets ?? []

  const blockingReasons: string[] = ready.data?.blocking_reasons ?? []
  const warnings: unknown[] = ready.data?.warnings ?? []
  const isBlocked = blockingReasons.length > 0
  const hasWarnings = warnings.length > 0 && !isBlocked
  const readyToTrade = ready.data?.ready_to_trade ?? false

  return (
    <>
      <PageHeader
        title="盯盘总览"
        subtitle="连接状态 · portfolio · 持仓敞口 · 候选 pipeline · 执行质量"
      />

      {/* 告警 banner */}
      {isBlocked && (
        <Alert
          icon={<IconAlertTriangle size={16} />}
          color="red"
          mb="md"
          title={`交易已阻塞（${blockingReasons.length} 个原因）`}
        >
          <Stack gap={2}>
            {blockingReasons.map((r, i) => (
              <Text key={i} size="xs" ff="var(--font-mono)">
                {r}
              </Text>
            ))}
          </Stack>
        </Alert>
      )}
      {hasWarnings && (
        <Alert
          icon={<IconAlertTriangle size={16} />}
          color="yellow"
          mb="md"
          title={`${warnings.length} 条警告`}
        >
          <Stack gap={2}>
            {warnings.slice(0, 5).map((w, i) => (
              <Text key={i} size="xs" ff="var(--font-mono)">
                {String(w)}
              </Text>
            ))}
          </Stack>
        </Alert>
      )}

      <SimpleGrid cols={{ base: 1, md: 2, lg: 3 }} spacing="md">

        {/* 连接 / 就绪 */}
        <SectionCard title="连接 / 就绪">
          {ready.error ? (
            <QueryErrorNotice error={ready.error} compact />
          ) : (
            <Stack gap={6}>
              <Row
                label="ready_to_trade"
                tone={readyToTrade ? 'success' : 'danger'}
                value={String(ready.data?.ready_to_trade ?? '—')}
              />
              <Row
                label="market_ws"
                tone={runtime.data?.readiness?.market_ws_connected ? 'success' : 'warning'}
                value={String(runtime.data?.readiness?.market_ws_connected ?? '—')}
              />
              <Row
                label="user_ws"
                tone={
                  (runtime.data?.readiness?.user_ws_connected ??
                    ready.data?.runtime?.user_ws_connected)
                    ? 'success'
                    : 'warning'
                }
                value={String(
                  runtime.data?.readiness?.user_ws_connected ??
                    ready.data?.runtime?.user_ws_connected ??
                    '—',
                )}
              />
              <Row
                label="trading_client"
                tone={runtime.data?.readiness?.trading_client_ready ? 'success' : 'warning'}
                value={String(runtime.data?.readiness?.trading_client_ready ?? '—')}
              />
              <Divider mt={4} mb={4} />
              <Group justify="space-between" gap="xs">
                <Text size="xs" c="dimmed">
                  快速操作
                </Text>
                <Anchor size="xs" onClick={() => navigate('/live/operations')}>
                  → 操作面板
                </Anchor>
              </Group>
            </Stack>
          )}
        </SectionCard>

        {/* Portfolio */}
        <SectionCard title="Portfolio">
          {portfolio.error ? (
            <QueryErrorNotice error={portfolio.error} compact />
          ) : (
            <Stack gap={6}>
              <KV k="净值 net_value" v={formatUsdc(portfolio.data?.net_value_usdc)} />
              <KV
                k="名义敞口 notional"
                v={formatUsdc(portfolio.data?.notional_usdc)}
              />
              <KV
                k="实现 PnL"
                v={formatUsdc(portfolio.data?.realized_pnl_usdc)}
                tone={pnlTone(portfolio.data?.realized_pnl_usdc)}
              />
              <KV
                k="未实现 PnL"
                v={formatUsdc(portfolio.data?.cash_pnl_usdc)}
                tone={pnlTone(portfolio.data?.cash_pnl_usdc)}
              />
              <Divider mt={2} mb={2} />
              <Group justify="space-between" gap="xs">
                <Text size="xs" c="dimmed">
                  持仓 / 开口
                </Text>
                <Anchor
                  size="sm"
                  fw={500}
                  onClick={() => navigate('/live/positions')}
                  style={{ cursor: 'pointer' }}
                >
                  {portfolio.data?.position_count ?? '—'} /{' '}
                  {portfolio.data?.open_position_count ?? '—'}
                </Anchor>
              </Group>
              <Group justify="space-between" gap="xs">
                <Text size="xs" c="dimmed">
                  挂单 open_orders
                </Text>
                <Anchor
                  size="sm"
                  fw={500}
                  onClick={() => navigate('/live/orders')}
                  style={{ cursor: 'pointer' }}
                >
                  {portfolio.data?.open_order_count ?? '—'}
                </Anchor>
              </Group>
            </Stack>
          )}
        </SectionCard>

        {/* 资金预算 */}
        <SectionCard title="资金预算">
          {exposure.error ? (
            <QueryErrorNotice error={exposure.error} compact />
          ) : (
            <Stack gap={6}>
              <KV k="余额 balance" v={formatUsdc(exposure.data?.balance_usdc)} />
              <KV k="可用 available" v={formatUsdc(exposure.data?.available_usdc)} />
              <KV k="权益 equity" v={formatUsdc(exposure.data?.equity_usdc)} />
              <KV
                k="已占名义 notional"
                v={formatUsdc(exposure.data?.total_notional_usdc)}
              />
              <KV
                k="BUY 预留"
                v={formatUsdc(exposure.data?.total_open_buy_reserved_usdc)}
              />
              {(() => {
                const balance = toDecimal(exposure.data?.balance_usdc)
                const available = toDecimal(exposure.data?.available_usdc)
                if (!balance || !available || balance.isZero()) return null
                const usedPct = balance.minus(available).div(balance).mul(100).toNumber()
                return (
                  <Box mt={4}>
                    <Group justify="space-between" mb={4}>
                      <Text size="xs" c="dimmed">
                        已用 / 总余额
                      </Text>
                      <Text size="xs">{usedPct.toFixed(1)}%</Text>
                    </Group>
                    <Progress
                      value={Math.min(usedPct, 100)}
                      color={usedPct > 80 ? 'red' : usedPct > 60 ? 'yellow' : 'teal'}
                      size="xs"
                    />
                  </Box>
                )
              })()}
            </Stack>
          )}
        </SectionCard>

        {/* 候选 Pipeline */}
        <SectionCard title="候选 Pipeline">
          {dataFreshness.error ? (
            <QueryErrorNotice error={dataFreshness.error} compact />
          ) : (
            <Stack gap={6}>
              <KV
                k="追踪市场数"
                v={String(dataFreshness.data?.item_count ?? '—')}
              />
              {(() => {
                const items = dataFreshness.data?.items ?? []
                const signalOn = items.filter((i) => i.signal_allowed === true).length
                const stale = items.filter(
                  (i) => i.staleness_ms !== null && i.staleness_ms > 30_000,
                ).length
                const total = items.length
                return (
                  <>
                    <KV
                      k="signal_allowed"
                      v={total > 0 ? `${signalOn} / ${total}` : '—'}
                      tone={signalOn > 0 ? 'pos' : 'neutral'}
                    />
                    <KV
                      k="数据过期 (>30s)"
                      v={total > 0 ? String(stale) : '—'}
                      tone={stale > 0 ? 'neg' : 'pos'}
                    />
                  </>
                )
              })()}
              <KV
                k="候选总数"
                v={String(candidates.data?.total ?? candidates.data?.items?.length ?? '—')}
              />
              <Group justify="flex-end">
                <Anchor size="xs" onClick={() => navigate('/live/candidates')}>
                  → 候选列表
                </Anchor>
              </Group>
            </Stack>
          )}
        </SectionCard>

        {/* 执行质量（最近 1h）*/}
        <SectionCard
          title="执行质量"
          description={
            execQuality.data?.sample_size != null
              ? `样本 ${execQuality.data.sample_size} 笔`
              : undefined
          }
        >
          {execQuality.error ? (
            <QueryErrorNotice error={execQuality.error} compact />
          ) : (
            <Stack gap={6}>
              <Text size="xs" c="dimmed" fw={500}>
                提交延迟 submit_latency
              </Text>
              <Group justify="space-between" gap="xs" pl={8}>
                <Text size="xs" c="dimmed">
                  p50
                </Text>
                <Text size="xs">
                  {execQuality.data?.submit_latency_ms?.p50 != null
                    ? `${execQuality.data.submit_latency_ms.p50.toFixed(0)} ms`
                    : '—'}
                </Text>
              </Group>
              <Group justify="space-between" gap="xs" pl={8}>
                <Text size="xs" c="dimmed">
                  p95
                </Text>
                <Text size="xs">
                  {execQuality.data?.submit_latency_ms?.p95 != null
                    ? `${execQuality.data.submit_latency_ms.p95.toFixed(0)} ms`
                    : '—'}
                </Text>
              </Group>
              <Text size="xs" c="dimmed" fw={500} mt={4}>
                Slippage
              </Text>
              <Group justify="space-between" gap="xs" pl={8}>
                <Text size="xs" c="dimmed">
                  mean
                </Text>
                <Text
                  size="xs"
                  c={
                    execQuality.data?.slippage_bps?.mean != null &&
                    execQuality.data.slippage_bps.mean > 50
                      ? 'var(--color-danger)'
                      : undefined
                  }
                >
                  {execQuality.data?.slippage_bps?.mean != null
                    ? `${execQuality.data.slippage_bps.mean.toFixed(1)} bps`
                    : '—'}
                </Text>
              </Group>
              <Group justify="space-between" gap="xs" pl={8}>
                <Text size="xs" c="dimmed">
                  p95
                </Text>
                <Text size="xs">
                  {execQuality.data?.slippage_bps?.p95 != null
                    ? `${execQuality.data.slippage_bps.p95.toFixed(1)} bps`
                    : '—'}
                </Text>
              </Group>
              <Group justify="flex-end">
                <Anchor size="xs" onClick={() => navigate('/analytics/execution-quality')}>
                  → 详细分析
                </Anchor>
              </Group>
            </Stack>
          )}
        </SectionCard>

        {/* 暂停市场快览 */}
        <SectionCard
          title="暂停市场"
          description={
            pausedMarkets.data?.items?.length
              ? `${pausedMarkets.data.items.length} 个暂停`
              : undefined
          }
        >
          {pausedMarkets.error ? (
            <QueryErrorNotice error={pausedMarkets.error} compact />
          ) : (
            <Stack gap={4}>
              {(pausedMarkets.data?.items ?? []).length === 0 ? (
                <Group gap={6}>
                  <IconCircleCheck size={14} color="var(--color-success)" />
                  <Text size="xs" c="dimmed">
                    无暂停市场
                  </Text>
                </Group>
              ) : (
                (pausedMarkets.data?.items ?? []).slice(0, 8).map((m) => (
                  <Box key={m.condition_id}>
                    <Group justify="space-between" gap="xs" wrap="nowrap">
                      <Text size="xs" ff="var(--font-mono)" style={{ flex: 1 }} truncate>
                        {m.market_slug ?? m.condition_id.slice(0, 12)}
                      </Text>
                      <Badge color="red" size="xs" variant="light">
                        暂停
                      </Badge>
                    </Group>
                    {m.pause_reason && (
                      <Text size="xs" c="dimmed" pl={0} truncate>
                        {m.pause_reason}
                      </Text>
                    )}
                  </Box>
                ))
              )}
              {(pausedMarkets.data?.has_more || (pausedMarkets.data?.total ?? 0) > 8) && (
                <Anchor size="xs" onClick={() => navigate('/markets')}>
                  → 查看全部
                </Anchor>
              )}
            </Stack>
          )}
        </SectionCard>

        {/* Workers / 队列 */}
        <SectionCard
          title="Workers / 队列"
          description={
            runtime.data?.readiness?.phase ?? runtime.data?.phase
              ? `phase: ${runtime.data?.readiness?.phase ?? runtime.data?.phase}`
              : undefined
          }
        >
          {workers.error ? (
            <QueryErrorNotice error={workers.error} compact />
          ) : (
            <Stack gap={4}>
              {(workers.data?.workers ?? []).slice(0, 8).map((w) => (
                <Group key={w.name} justify="space-between" gap="xs">
                  <Text size="xs" ff="var(--font-mono)">
                    {w.name}
                  </Text>
                  <Group gap={6}>
                    <StatusPill
                      tone={
                        w.healthy
                          ? 'success'
                          : w.state === 'paused'
                            ? 'warning'
                            : 'danger'
                      }
                      size="xs"
                    >
                      {w.state ?? (w.healthy ? 'ok' : 'down')}
                    </StatusPill>
                    {typeof w.queue_depth === 'number' ? (
                      <Text
                        size="xs"
                        c={w.queue_depth > 50 ? 'var(--color-danger)' : 'dimmed'}
                      >
                        q={w.queue_depth}
                      </Text>
                    ) : null}
                  </Group>
                </Group>
              ))}
              {!(workers.data?.workers ?? []).length && (
                <Text c="dimmed" size="xs">
                  无 worker 数据
                </Text>
              )}
            </Stack>
          )}
        </SectionCard>

        {/* 执行延迟 p95（ms）*/}
        <SectionCard title="执行延迟 p95（ms）">
          {latency.error ? (
            <QueryErrorNotice error={latency.error} compact />
          ) : (
            <Stack gap={4}>
              {(latency.data?.stages ?? []).map((stage) => (
                <Group key={stage.stage} justify="space-between" gap="xs">
                  <Text size="xs" ff="var(--font-mono)">
                    {stage.stage}
                  </Text>
                  <Text size="xs">
                    p50={formatDecimal(stage.p50_ms, { dp: 0 })} · p95=
                    {formatDecimal(stage.p95_ms, { dp: 0 })} · p99=
                    {formatDecimal(stage.p99_ms, { dp: 0 })}
                  </Text>
                </Group>
              ))}
              {!(latency.data?.stages ?? []).length && (
                <Text c="dimmed" size="xs">
                  暂无样本
                </Text>
              )}
            </Stack>
          )}
        </SectionCard>

        {/* SSE / 订阅 */}
        <SectionCard title="SSE / 订阅">
          {metrics.error ? (
            <QueryErrorNotice error={metrics.error} compact />
          ) : (
            <Stack gap={4}>
              <KV
                k="active subscribers"
                v={String(metrics.data?.sse_active_subscribers ?? '—')}
              />
              <KV
                k="dropped events"
                v={String(metrics.data?.sse_dropped_events_total ?? '—')}
                tone={
                  (metrics.data?.sse_dropped_events_total ?? 0) > 0 ? 'neg' : 'neutral'
                }
              />
              <KV
                k="追踪市场"
                v={String(portfolio.data?.markets_tracked ?? '—')}
              />
              <KV
                k="暂停计数"
                v={String(portfolio.data?.pause_count ?? '—')}
                tone={(portfolio.data?.pause_count ?? 0) > 0 ? 'neg' : 'neutral'}
              />
            </Stack>
          )}
        </SectionCard>

        {/* 持仓敞口（全宽）*/}
        <div style={{ gridColumn: '1 / -1' }}>
          <SectionCard
            title="持仓敞口"
            description={
              exposure.data?.position_count != null
                ? `${exposure.data.position_count} 个持仓 · 总名义 ${formatUsdc(exposure.data.total_notional_usdc)} · 未实现 PnL ${formatUsdc(exposure.data.total_cash_pnl)}`
                : undefined
            }
            actions={
              <Anchor size="xs" onClick={() => navigate('/live/positions')}>
                → 持仓管理
              </Anchor>
            }
          >
            {exposure.error ? (
              <QueryErrorNotice error={exposure.error} compact />
            ) : (exposure.data?.items ?? []).length === 0 ? (
              <Text c="dimmed" size="xs">
                无开口持仓
              </Text>
            ) : (
              <Stack gap={0}>
                {/* 表头 */}
                <Group
                  justify="space-between"
                  gap="xs"
                  px={4}
                  py={4}
                  style={{ borderBottom: '1px solid var(--mantine-color-default-border)' }}
                >
                  {['市场', '仓位(shares)', '均价', '现价', '未实现PnL', '%', '状态'].map(
                    (h) => (
                      <Text key={h} size="xs" c="dimmed" fw={500} style={{ minWidth: 60 }}>
                        {h}
                      </Text>
                    ),
                  )}
                </Group>
                {(exposure.data?.items ?? [])
                  .sort((a, b) => {
                    const na = toDecimal(a.notional_usdc)?.toNumber() ?? 0
                    const nb = toDecimal(b.notional_usdc)?.toNumber() ?? 0
                    return nb - na
                  })
                  .map((item: PortfolioExposureItem) => (
                    <PositionRow key={item.token_id} item={item} />
                  ))}
              </Stack>
            )}
          </SectionCard>
        </div>

        {/* 策略私有 widgets */}
        {widgets.map((widget) => (
          <div key={widget.id} style={{ gridColumn: '1 / -1' }}>
            {widget.render()}
          </div>
        ))}
      </SimpleGrid>
    </>
  )
}

function PositionRow({ item }: { item: PortfolioExposureItem }) {
  const cashPnlTone = pnlTone(item.cash_pnl)
  const cashPnlColor =
    cashPnlTone === 'pos'
      ? 'var(--color-success)'
      : cashPnlTone === 'neg'
        ? 'var(--color-danger)'
        : undefined
  const pctPnlTone = pnlTone(item.percent_pnl)
  const pctColor =
    pctPnlTone === 'pos'
      ? 'var(--color-success)'
      : pctPnlTone === 'neg'
        ? 'var(--color-danger)'
        : undefined
  const statusBadge = item.paused ? (
    <Badge color="red" size="xs" variant="light">
      暂停
    </Badge>
  ) : item.redeemable ? (
    <Badge color="teal" size="xs" variant="light">
      可赎回
    </Badge>
  ) : item.settled_zero_value ? (
    <Badge color="gray" size="xs" variant="light">
      归零
    </Badge>
  ) : (
    <Badge color="blue" size="xs" variant="light">
      持有
    </Badge>
  )

  return (
    <Group
      justify="space-between"
      gap="xs"
      px={4}
      py={6}
      style={{ borderBottom: '1px solid var(--mantine-color-default-border)' }}
      wrap="nowrap"
    >
      <Text size="xs" ff="var(--font-mono)" style={{ minWidth: 60 }} truncate>
        {item.market_slug ?? item.condition_id.slice(0, 10)}
      </Text>
      <Text size="xs" ff="var(--font-mono)" style={{ minWidth: 60 }}>
        {formatDecimal(item.shares, { dp: 2 })}
      </Text>
      <Text size="xs" ff="var(--font-mono)" style={{ minWidth: 60 }}>
        {formatDecimal(item.avg_price, { dp: 3 })}
      </Text>
      <Text size="xs" ff="var(--font-mono)" style={{ minWidth: 60 }}>
        {formatDecimal(item.cur_price, { dp: 3 })}
      </Text>
      <Text size="xs" fw={500} c={cashPnlColor} style={{ minWidth: 60 }}>
        {formatUsdc(item.cash_pnl)}
      </Text>
      <Text size="xs" c={pctColor} style={{ minWidth: 60 }}>
        {formatPercent(item.percent_pnl, { signed: true })}
      </Text>
      <Box style={{ minWidth: 60 }}>{statusBadge}</Box>
    </Group>
  )
}

function Row({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone: 'success' | 'warning' | 'danger' | 'neutral'
}) {
  return (
    <Group justify="space-between">
      <Text size="xs" ff="var(--font-mono)">
        {label}
      </Text>
      <StatusPill tone={tone} size="xs">
        {value}
      </StatusPill>
    </Group>
  )
}

function KV({
  k,
  v,
  tone,
}: {
  k: string
  v: string
  tone?: 'pos' | 'neg' | 'neutral'
}) {
  const color =
    tone === 'pos'
      ? 'var(--color-success)'
      : tone === 'neg'
        ? 'var(--color-danger)'
        : undefined
  return (
    <Group justify="space-between" gap="xs">
      <Text size="xs" c="dimmed">
        {k}
      </Text>
      <Text size="sm" fw={500} c={color}>
        {v}
      </Text>
    </Group>
  )
}
