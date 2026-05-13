import { useQuery } from '@tanstack/react-query'
import { SimpleGrid, Stack, Group, Text } from '@mantine/core'
import { qk } from '@core/api/keys'
import { healthApi, portfolioApi } from '@core/api/resources'
import { useRuntimeIdentity } from '@core/identity/useRuntimeIdentity'
import { resolveStrategyBundle } from '@strategy/registry'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { formatUsdc, pnlTone, formatDecimal } from '@shared/format'

export function LiveOverviewPage() {
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
  const latency = useQuery({
    queryKey: qk.latency({ window_ms: null, sample_limit: 500 }),
    queryFn: ({ signal }) => healthApi.latencyPercentiles({ sample_limit: 500 }, signal),
  })
  // 当前 extension_module 注入的 dashboard widgets——通用壳不知道策略想展示什么。
  const { extensionModule } = useRuntimeIdentity()
  const strategyBundle = resolveStrategyBundle(extensionModule)
  const widgets = strategyBundle.dashboardWidgets ?? []

  return (
    <>
      <PageHeader
        title="盯盘总览"
        subtitle="连接状态 · portfolio · 最近决策 · 关键 latency"
      />

      <SimpleGrid cols={{ base: 1, md: 2, lg: 3 }} spacing="md">
        <SectionCard title="连接 / 就绪">
          {ready.error ? (
            <QueryErrorNotice error={ready.error} compact />
          ) : (
            <Stack gap={6}>
              <Row label="ready" tone={ready.data?.ready_to_trade ? 'success' : 'danger'} value={String(ready.data?.ready_to_trade ?? '—')} />
              <Row
                label="market_ws"
                tone={runtime.data?.readiness?.market_ws_connected ? 'success' : 'warning'}
                value={String(runtime.data?.readiness?.market_ws_connected ?? '—')}
              />
              <Row
                label="user_ws"
                tone={(runtime.data?.readiness?.user_ws_connected ?? ready.data?.runtime?.user_ws_connected) ? 'success' : 'warning'}
                value={String(runtime.data?.readiness?.user_ws_connected ?? ready.data?.runtime?.user_ws_connected ?? '—')}
              />
              <Row
                label="trading_client"
                tone={runtime.data?.readiness?.trading_client_ready ? 'success' : 'warning'}
                value={String(runtime.data?.readiness?.trading_client_ready ?? '—')}
              />
              {(ready.data?.blocking_reasons ?? []).length > 0 ? (
                <Text size="xs" c="dimmed">
                  blocked: <code>{(ready.data!.blocking_reasons as string[]).join(', ')}</code>
                </Text>
              ) : null}
            </Stack>
          )}
        </SectionCard>

        <SectionCard title="Portfolio">
          {portfolio.error ? (
            <QueryErrorNotice error={portfolio.error} compact />
          ) : (
            <Stack gap={6}>
              <KV
                k="净值 net_value"
                v={formatUsdc(portfolio.data?.net_value_usdc)}
              />
              <KV k="名义敞口 notional" v={formatUsdc(portfolio.data?.notional_usdc)} />
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
              <KV k="持仓数 / 开口数" v={`${portfolio.data?.position_count ?? '—'} / ${portfolio.data?.open_position_count ?? '—'}`} />
            </Stack>
          )}
        </SectionCard>

        <SectionCard title="账户计数">
          {portfolio.error ? (
            <QueryErrorNotice error={portfolio.error} compact />
          ) : (
            <Stack gap={6}>
              <KV k="成交 fill_count" v={String(portfolio.data?.fill_count ?? '—')} />
              <KV k="持仓 position_count" v={String(portfolio.data?.position_count ?? '—')} />
              <KV k="挂单 open_orders" v={String(portfolio.data?.open_order_count ?? '—')} />
              <KV k="追踪市场 markets" v={String(portfolio.data?.markets_tracked ?? '—')} />
              <KV k="暂停市场 pauses" v={String(portfolio.data?.pause_count ?? '—')} />
            </Stack>
          )}
        </SectionCard>

        <SectionCard title="Workers / 队列" description={runtime.data?.phase ? `phase: ${runtime.data.phase}` : undefined}>
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
                    <StatusPill tone={w.healthy ? 'success' : w.state === 'paused' ? 'warning' : 'danger'} size="xs">
                      {w.state ?? (w.healthy ? 'ok' : 'down')}
                    </StatusPill>
                    {typeof w.queue_depth === 'number' ? (
                      <Text size="xs" c="dimmed">
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
                    p50={formatDecimal(stage.p50_ms, { dp: 0 })} · p95={formatDecimal(stage.p95_ms, { dp: 0 })} · p99=
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

        <SectionCard title="SSE / 订阅">
          {metrics.error ? (
            <QueryErrorNotice error={metrics.error} compact />
          ) : (
            <Stack gap={4}>
              <KV k="active subscribers" v={String(metrics.data?.sse_active_subscribers ?? '—')} />
              <KV k="dropped events" v={String(metrics.data?.sse_dropped_events_total ?? '—')} />
            </Stack>
          )}
        </SectionCard>

        {/* 策略私有 widgets——通用壳让 strategies.current 自己说话。 */}
        {widgets.map((widget) => (
          <div key={widget.id} style={{ gridColumn: '1 / -1' }}>
            {widget.render()}
          </div>
        ))}
      </SimpleGrid>
    </>
  )
}

function Row({ label, value, tone }: { label: string; value: string; tone: 'success' | 'warning' | 'danger' | 'neutral' }) {
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

function KV({ k, v, tone }: { k: string; v: string; tone?: 'pos' | 'neg' | 'neutral' }) {
  const color = tone === 'pos' ? 'var(--color-success)' : tone === 'neg' ? 'var(--color-danger)' : undefined
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
