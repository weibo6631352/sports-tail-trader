import { useQuery } from '@tanstack/react-query'
import { Group, Stack, Text } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import { qk } from '@core/api/keys'
import { candidatesApi } from '@core/api/resources'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'

// 盯盘总览的策略私有卡片：当前 confirmable 候选 top 5。
// 通用壳不知道"候选"对当前策略意味着什么——这卡是 strategies.current 自己说话。

export function CandidatesWidget() {
  const navigate = useNavigate()
  const params = { limit: 5, confirmable: true }
  const query = useQuery({
    queryKey: qk.candidates.list(params),
    queryFn: ({ signal }) => candidatesApi.list(params, signal),
  })

  const items = query.data?.items ?? []

  return (
    <SectionCard
      title="可确认候选 Top 5"
      description="confirmable=true · 点行跳候选页"
      actions={
        <button
          type="button"
          onClick={() => navigate('/live/candidates')}
          style={{
            background: 'transparent',
            border: '1px solid var(--color-accent)',
            color: 'var(--color-accent)',
            padding: '2px 8px',
            borderRadius: 4,
            cursor: 'pointer',
            fontSize: 11,
          }}
        >
          全部
        </button>
      }
    >
      {items.length === 0 ? (
        <Text c="dimmed" size="sm">
          暂无可确认候选
        </Text>
      ) : (
        <Stack gap={6}>
          {items.map((c, idx) => (
            <Group key={`${c.condition_id}_${idx}`} justify="space-between" wrap="nowrap">
              <Stack gap={0}>
                <Text size="sm">{c.market_slug ?? c.condition_id}</Text>
                <Group gap={6}>
                  <Text size="xs" c="dimmed">
                    {c.league ?? '—'} · {c.market_type ?? '—'}
                  </Text>
                  {c.token_id ? <CopyableId value={c.token_id} dense label="tok" /> : null}
                </Group>
              </Stack>
              <StatusPill
                tone={
                  c.execution_permission === 'auto'
                    ? 'success'
                    : c.execution_permission === 'manual'
                      ? 'warning'
                      : 'neutral'
                }
                size="xs"
              >
                {c.action ?? '—'} · {c.execution_permission ?? '—'}
              </StatusPill>
            </Group>
          ))}
        </Stack>
      )}
    </SectionCard>
  )
}
