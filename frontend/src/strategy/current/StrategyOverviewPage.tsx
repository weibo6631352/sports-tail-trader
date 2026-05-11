import { useQuery } from '@tanstack/react-query'
import { SimpleGrid, Stack, Text, Group } from '@mantine/core'
import { qk } from '@core/api/keys'
import { candidatesApi } from '@core/api/resources'
import { useRuntimeIdentity } from '@core/identity/useRuntimeIdentity'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { formatIso } from '@shared/format'

// 策略概览：候选 (limit=20) + 体育实时状态 (limit=20)。
// 通用候选页在 /live/candidates，这里只是"策略侧"的浓缩视图。

export function StrategyOverviewPage() {
  const { extensionModule, automaticTradingEnabled } = useRuntimeIdentity()
  const candidates = useQuery({
    queryKey: qk.candidates.list({ limit: 20 }),
    queryFn: ({ signal }) => candidatesApi.list({ limit: 20 }, signal),
  })
  const liveStates = useQuery({
    queryKey: qk.candidates.liveStates({ limit: 20 }),
    queryFn: ({ signal }) => candidatesApi.liveStates({ limit: 20 }, signal),
  })

  return (
    <>
      <PageHeader
        title="策略概览"
        subtitle={extensionModule ?? '未指定 extension_module'}
        actions={
          <StatusPill tone={automaticTradingEnabled ? 'success' : 'warning'} size="sm">
            {automaticTradingEnabled ? '自动交易：开' : '自动交易：停'}
          </StatusPill>
        }
      />
      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
        <SectionCard title="近期候选 (前 20)">
          {candidates.error ? (
            <QueryErrorNotice error={candidates.error} compact />
          ) : candidates.data?.items.length ? (
            <Stack gap={4}>
              {candidates.data.items.map((c, idx) => (
                <Group key={`${c.condition_id}_${idx}`} justify="space-between" wrap="nowrap">
                  <Stack gap={0}>
                    <Text size="sm">{c.market_slug ?? c.condition_id}</Text>
                    <Text size="xs" c="dimmed">
                      {c.league ?? '—'} · {c.market_type ?? '—'} · action={c.action ?? '—'}
                    </Text>
                  </Stack>
                  <StatusPill tone={c.accepted ? 'success' : 'danger'} size="xs">
                    {c.accepted ? c.execution_permission ?? 'accepted' : 'rejected'}
                  </StatusPill>
                </Group>
              ))}
            </Stack>
          ) : (
            <Text c="dimmed" size="sm">
              暂无候选
            </Text>
          )}
        </SectionCard>

        <SectionCard title="体育实时状态 (前 20)">
          {liveStates.error ? (
            <QueryErrorNotice error={liveStates.error} compact />
          ) : liveStates.data?.items.length ? (
            <Stack gap={4}>
              {liveStates.data.items.map((s, idx) => (
                <Group key={`${s.condition_id ?? s.market_slug ?? idx}`} justify="space-between" wrap="nowrap" align="flex-start">
                  <Stack gap={0}>
                    <Text size="sm">{s.market_slug ?? s.event_slug ?? s.condition_id ?? '—'}</Text>
                    <Text size="xs" c="dimmed">
                      source: {s.source ?? '—'} · {formatIso(s.updated_at, 'MM-DD HH:mm:ss')}
                    </Text>
                  </Stack>
                  <Stack gap={2} align="flex-end">
                    {s.condition_id ? <CopyableId value={s.condition_id} head={4} tail={4} /> : null}
                    <StatusPill
                      tone={s.signal_allowed === true ? 'success' : s.signal_allowed === false ? 'danger' : 'neutral'}
                      size="xs"
                    >
                      signal: {String(s.signal_allowed ?? 'unknown')}
                    </StatusPill>
                  </Stack>
                </Group>
              ))}
            </Stack>
          ) : (
            <Text c="dimmed" size="sm">
              暂无 live state
            </Text>
          )}
        </SectionCard>
      </SimpleGrid>
    </>
  )
}
