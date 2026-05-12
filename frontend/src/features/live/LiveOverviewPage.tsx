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
              <Row label="ready" tone={ready.data?.ready ? 'success' : 'danger'} value={String(ready.data?.ready ?? '—')} />
              <Row
                label="market_ws"
                tone={ready.data?.market_ws_connected ? 'success' : 'warning'}
                value={String(ready.data?.market_ws_connected ?? '—')}
              />
              <Row
                label="user_ws"
                tone={ready.data?.user_ws_connected ? 'success' : 'warning'}
                value={String(ready.data?.user_ws_connected ?? '—')}
              />
              <Row
                label="trading_client"
                tone={ready.data?.trading_client_ready ? 'success' : 'warning'}
                value={String(ready.data?.trading_client_ready ?? '—')}
              />
              {ready.data?.blocked_on ? (
                <Text size="xs" c="dimmed">
                  blocked_on: <code>{ready.data.blocked_on}</code>
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
                v={formatUsdc(portfolio.data?.net_value_usdc ?? portfolio.data?.equity_usdc)}
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

        <SectionCard title="决策计数">
          {metrics.error ? (
            <QueryErrorNotice error={metrics.error} compact />
          ) : (
            <Stack gap={6}>
              <KV k="decisions_total" v={String(metrics.data?.decisions_total ?? '—')} />
              <KV k="accepted" v={String(metrics.data?.decisions_accepted ?? '—')} tone="pos" />
              <KV k="rejected" v={String(metrics.data?.decisions_rejected ?? '—')} tone="neutral" />
              <KV k="orders_acked" v={String(metrics.data?.orders_acked ?? '—')} />
              <KV k="fills_total" v={String(metrics.data?.fills_total ?? '—')} />
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
          <Stack gap={4}>
            <KV k="active subscribers" v={String(metrics.data?.sse_active_subscribers ?? '—')} />
            <KV k="dropped events" v={String(metrics.data?.sse_dropped_events_total ?? '—')} />
            <KV k="subscriber cap" v={String(metrics.data?.sse_subscriber_cap ?? '—')} />
          </Stack>
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
