import { useQuery } from '@tanstack/react-query'
import { Button, SimpleGrid, Stack, Text, Group } from '@mantine/core'
import { useEffect, useMemo, useState } from 'react'
import { qk } from '@core/api/keys'
import { healthApi } from '@core/api/resources'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { JsonPanel } from '@shared/ui/JsonPanel'
import { useSseStatus } from '@core/sse'
import { formatEpochMs } from '@shared/format'
import {
  useApiHealthStore,
  recentErrorRateAt,
  recentSampleCountAt,
} from '@core/observability/queryStats'

export function HealthPage() {
  const workers = useQuery({
    queryKey: qk.workers(),
    queryFn: ({ signal }) => healthApi.workers(signal),
  })
  const runtime = useQuery({
    queryKey: qk.runtime(),
    queryFn: ({ signal }) => healthApi.runtime(signal),
  })
  const sseStatus = useSseStatus()
  const apiHealth = useApiHealthStore()

  // 用 tick 把"当下"提到状态层——Date.now 只在 setInterval 里调一次，
  // 渲染纯函数化，React Compiler 可 memoize 整个组件。
  // tab 不可见时暂停 tick，避免后台 CPU 浪费 / 无谓 re-render。
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    let id: number | null = null
    const start = () => {
      if (id !== null) return
      // 进入可见状态立刻刷新一次，避免久放后的旧 tick 误导。
      setNow(Date.now())
      id = window.setInterval(() => setNow(Date.now()), 30_000)
    }
    const stop = () => {
      if (id !== null) {
        window.clearInterval(id)
        id = null
      }
    }
    const onVisibility = () => (document.visibilityState === 'visible' ? start() : stop())
    if (document.visibilityState === 'visible') start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  const recentRate5m = useMemo(
    () => recentErrorRateAt(apiHealth, 5 * 60_000, now),
    [apiHealth, now],
  )
  const recentSampleCount = useMemo(
    () => recentSampleCountAt(apiHealth.recent, 5 * 60_000, now),
    [apiHealth.recent, now],
  )

  return (
    <>
      <PageHeader title="系统健康 System Health" subtitle="workers · queues · scheduler · 连接状态" />
      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
        <SectionCard title="Workers">
          {workers.error ? (
            <QueryErrorNotice error={workers.error} compact />
          ) : (
            <Stack gap={6}>
              {(workers.data?.workers ?? []).map((w) => (
                <Group key={w.name} justify="space-between" wrap="nowrap">
                  <Stack gap={0}>
                    <Text size="sm" ff="var(--font-mono)">
                      {w.name}
                    </Text>
                    {w.last_error ? (
                      <Text size="xs" c="red">
                        {w.last_error}
                      </Text>
                    ) : null}
                  </Stack>
                  <Group gap={6}>
                    <StatusPill tone={w.healthy ? 'success' : 'danger'} size="xs">
                      {w.state ?? (w.healthy ? 'ok' : 'down')}
                    </StatusPill>
                    {typeof w.queue_depth === 'number' ? (
                      <Text size="xs" c="dimmed" ff="var(--font-mono)">
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

        <SectionCard title="Scheduler 任务">
          {workers.error ? (
            <QueryErrorNotice error={workers.error} compact />
          ) : (
            <Stack gap={4}>
              {(workers.data?.scheduler?.jobs ?? []).map((job) => (
                <Group key={job.name} justify="space-between">
                  <Text size="sm" ff="var(--font-mono)">
                    {job.name}
                  </Text>
                  <Text size="xs" c="dimmed">
                    每 {job.interval_seconds != null ? `${job.interval_seconds}s` : '—'} · 上次 {job.last_started_at ? job.last_started_at.replace('T', ' ').slice(0, 19) : '—'}
                  </Text>
                </Group>
              ))}
              {!(workers.data?.scheduler?.jobs ?? []).length && (
                <Text c="dimmed" size="xs">
                  无调度任务数据
                </Text>
              )}
            </Stack>
          )}
        </SectionCard>

        <SectionCard title="SSE 连接（前端自报）">
          <Stack gap={6}>
            <Row k="state" v={sseStatus.state} />
            <Row k="url" v={sseStatus.url ?? '—'} />
            <Row k="last event" v={sseStatus.lastEventAt ? formatEpochMs(sseStatus.lastEventAt) : '—'} />
            <Row k="dropped events" v={String(sseStatus.droppedEventsTotal)} />
            <Row k="reconnect attempts" v={String(sseStatus.reconnectAttempts)} />
            {sseStatus.lastError ? <Row k="last error" v={sseStatus.lastError} /> : null}
            {typeof workers.data?.sse_active_subscribers === 'number' ? (
              <Row k="server subscribers" v={String(workers.data.sse_active_subscribers)} />
            ) : null}
          </Stack>
        </SectionCard>

        <SectionCard
          title="前端 API 健康"
          description="React Query Cache 订阅器统计——浏览器内存，刷新即清"
          actions={
            <Button size="compact-xs" variant="subtle" onClick={apiHealth.reset}>
              清零
            </Button>
          }
        >
          <Stack gap={6}>
            <Row k="total fetches" v={String(apiHealth.totalFetches)} />
            <Row k="errors" v={String(apiHealth.errorCount)} />
            <Row
              k="近 5 分钟错误率"
              v={
                recentRate5m === null
                  ? '—'
                  : `${(recentRate5m * 100).toFixed(1)}% (${recentSampleCount} 条样本)`
              }
            />
            <Row
              k="最近成功"
              v={apiHealth.lastSuccessAt ? formatEpochMs(apiHealth.lastSuccessAt) : '—'}
            />
            <Row
              k="最近失败"
              v={apiHealth.lastErrorAt ? formatEpochMs(apiHealth.lastErrorAt) : '—'}
            />
            {apiHealth.lastErrorMessage ? (
              <Row k="last error" v={apiHealth.lastErrorMessage.slice(0, 80)} />
            ) : null}
          </Stack>
        </SectionCard>

        <SectionCard title="Runtime 原始快照">
          <JsonPanel value={runtime.data ?? {}} maxHeight={260} />
        </SectionCard>
      </SimpleGrid>
    </>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <Group justify="space-between">
      <Text size="xs" c="dimmed">
        {k}
      </Text>
      <Text size="sm" ff="var(--font-mono)">
        {v}
      </Text>
    </Group>
  )
}
