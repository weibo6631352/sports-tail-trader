import { useQuery } from '@tanstack/react-query'
import { Group, Stack, Text, Badge } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import { qk } from '@core/api/keys'
import { decisionsApi } from '@core/api/resources'
import type { MarketView } from '@core/api/types'
import { SectionCard } from '@shared/ui/SectionCard'
import { CopyableId } from '@shared/ui/CopyableId'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { MonoCell } from '@shared/ui/MonoCell'
import { formatDecimal, formatIso } from '@shared/format'

// 通用市场详情页 / 抽屉给策略私有信息——展示该 condition_id 最近 3 条决策摘要 +
// 跳转入口（timeline / decisions 列表）。

export function MarketDetailPanel({ market }: { market: MarketView }) {
  const navigate = useNavigate()
  const params = { limit: 3, condition_id: market.condition_id }
  const query = useQuery({
    queryKey: qk.decisions.list(params),
    queryFn: ({ signal }) => decisionsApi.list(params, signal),
  })

  const items = query.data?.items ?? []

  return (
    <SectionCard
      title="策略：最近决策"
      description={`condition=${market.condition_id.slice(0, 10)}…`}
      actions={
        <Group gap={6}>
          <InlineActionButton
            variant="link"
            onClick={() =>
              navigate(`/investigate/timeline?condition_id=${encodeURIComponent(market.condition_id)}`)
            }
          >
            timeline
          </InlineActionButton>
          <InlineActionButton
            variant="link"
            onClick={() =>
              navigate(`/investigate/decisions?condition_id=${encodeURIComponent(market.condition_id)}`)
            }
          >
            全部决策
          </InlineActionButton>
        </Group>
      }
    >
      {items.length === 0 ? (
        <Text c="dimmed" size="sm">
          暂无决策记录
        </Text>
      ) : (
        <Stack gap="xs">
          {items.map((d) => {
            const output = d.decision_output ?? {}
            const fair = output['fair_value'] as string | number | undefined
            const cap = output['entry_price_cap'] as string | number | undefined
            return (
              <Group key={d.record_id} justify="space-between" wrap="nowrap" align="flex-start">
                <Stack gap={0}>
                  <Group gap={6}>
                    <MonoCell>{d.hook_name ?? '—'}</MonoCell>
                    <Badge size="xs" color={d.accepted ? 'teal' : 'red'} variant="light">
                      {d.accepted ? 'accepted' : 'rejected'}
                    </Badge>
                  </Group>
                  <Text size="xs" c="dimmed">
                    {formatIso(d.created_at, 'MM-DD HH:mm:ss')} · {d.reason ?? '—'}
                  </Text>
                </Stack>
                <Stack gap={0} align="flex-end">
                  <Text size="xs" ff="var(--font-mono)">
                    fair={formatDecimal(fair, { dp: 4 })} cap={formatDecimal(cap, { dp: 4 })}
                  </Text>
                  <CopyableId value={d.trace_id} dense label="trace" />
                </Stack>
              </Group>
            )
          })}
        </Stack>
      )}
    </SectionCard>
  )
}
