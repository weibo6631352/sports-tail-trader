import { Drawer, Stack, Group, Text, Badge } from '@mantine/core'
import { useQuery } from '@tanstack/react-query'
import { qk } from '@core/api/keys'
import { decisionsApi } from '@core/api/resources'
import { CopyableId } from '@shared/ui/CopyableId'
import { JsonPanel } from '@shared/ui/JsonPanel'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { SectionCard } from '@shared/ui/SectionCard'
import { formatIso } from '@shared/format'

type Props = {
  recordId: string | null
  onClose: () => void
}

export function DecisionDetailDrawer({ recordId, onClose }: Props) {
  const query = useQuery({
    queryKey: recordId ? qk.decisions.detail(recordId) : ['decisions', 'detail', 'null'],
    queryFn: ({ signal }) => decisionsApi.detail(recordId as string, signal),
    enabled: Boolean(recordId),
  })

  return (
    <Drawer
      opened={Boolean(recordId)}
      onClose={onClose}
      title={
        <Group gap="xs">
          <Text fw={600}>决策记录详情</Text>
          {query.data?.accepted !== undefined ? (
            <Badge color={query.data.accepted ? 'teal' : 'red'} size="xs">
              {query.data.accepted ? 'accepted' : 'rejected'}
            </Badge>
          ) : null}
        </Group>
      }
      size="xl"
      position="right"
    >
      {query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : query.isLoading || !query.data ? (
        <Text c="dimmed">加载中…</Text>
      ) : (
        <Stack gap="md">
          <SectionCard title="基本">
            <Stack gap={4}>
              <KV k="record_id" v={<CopyableId value={query.data.record_id} />} />
              <KV k="trace_id" v={<CopyableId value={query.data.trace_id} />} />
              <KV k="condition_id" v={<CopyableId value={query.data.condition_id} />} />
              <KV k="token_id" v={<CopyableId value={query.data.token_id ?? ''} />} />
              <KV k="hook" v={query.data.hook_name ?? '—'} />
              <KV k="strategy_id" v={query.data.strategy_id ?? '—'} />
              <KV k="created_at" v={formatIso(query.data.created_at)} />
              {query.data.reason ? <KV k="reason" v={<code>{query.data.reason}</code>} /> : null}
            </Stack>
          </SectionCard>

          <SectionCard title="decision_output (策略输出)">
            <JsonPanel value={query.data.decision_output ?? {}} maxHeight={300} />
          </SectionCard>

          <SectionCard title="decision_input (策略快照)">
            <JsonPanel value={query.data.decision_input ?? {}} maxHeight={420} />
          </SectionCard>
        </Stack>
      )}
    </Drawer>
  )
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <Group justify="space-between" gap="xs">
      <Text size="xs" c="dimmed">
        {k}
      </Text>
      <Text size="sm">{v}</Text>
    </Group>
  )
}
