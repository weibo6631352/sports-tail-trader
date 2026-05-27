import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, NumberInput, Stack, Text, TextInput, Button } from '@mantine/core'
import { useSearchParams } from 'react-router-dom'
import { qk } from '@core/api/keys'
import { tradesApi } from '@core/api/resources'
import { useFilteredSse } from '@core/sse'
import type { TimelineEvent } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { CopyableId } from '@shared/ui/CopyableId'
import { formatDecimal, formatUsdc, pnlTone } from '@shared/format'
import { TimelineEventNode } from './TimelineEventNode'
import { DecisionDetailDrawer } from './DecisionDetailDrawer'

export function TradeTimelinePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialCondition = searchParams.get('condition_id') ?? ''
  const initialToken = searchParams.get('token_id') ?? ''

  const [conditionInput, setConditionInput] = useState(initialCondition)
  const [tokenInput, setTokenInput] = useState(initialToken)
  const [limit, setLimit] = useState(1000)
  const [submitted, setSubmitted] = useState({
    conditionId: initialCondition,
    tokenId: initialToken,
  })
  const [detailRecordId, setDetailRecordId] = useState<string | null>(null)

  // 抽屉 / timeline 打开时挂条带 condition_id 的辅助 SSE 连接（plan 设计）。
  // 仅 console.debug 记录；invalidate 由主连接负责。
  useFilteredSse(
    submitted.conditionId ? { conditionId: submitted.conditionId } : {},
    (event) => {
      if (event.eventType === 'subscription_lag') return
      // 收到任何 condition 范围内的事件 → invalidate 当前 query。
      // 通过 React Query gc + invalidate root 路由表已经覆盖；这里仅做轻量 debug。
      // 也可在 SSE 推进时间线节点（避免整页 refetch），后续 task #12 增强。
    },
    [submitted.conditionId],
  )

  const params = useMemo(
    () => ({
      token_id: submitted.tokenId || undefined,
      limit,
    }),
    [submitted.tokenId, limit],
  )

  const query = useQuery({
    queryKey: submitted.conditionId
      ? qk.trades.timeline(submitted.conditionId, params)
      : ['trades', 'timeline', null, params],
    queryFn: ({ signal }) => tradesApi.timeline(submitted.conditionId, params, signal),
    enabled: Boolean(submitted.conditionId),
  })

  // URL → 提交 keeping in sync（顶栏跳转过来时立即查询）。
  // 故意在 effect 中初始化输入框 + 提交：因为 URL 是外部输入源。
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */
  useEffect(() => {
    setConditionInput(initialCondition)
    setTokenInput(initialToken)
    if (initialCondition && !submitted.conditionId) {
      setSubmitted({ conditionId: initialCondition, tokenId: initialToken })
    }
  }, [initialCondition, initialToken])
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

  const handleSubmit = () => {
    setSubmitted({ conditionId: conditionInput.trim(), tokenId: tokenInput.trim() })
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (conditionInput.trim()) {
        next.set('condition_id', conditionInput.trim())
      } else {
        next.delete('condition_id')
      }
      if (tokenInput.trim()) {
        next.set('token_id', tokenInput.trim())
      } else {
        next.delete('token_id')
      }
      return next
    })
  }

  return (
    <>
      <PageHeader
        title="交易时间线 Trade Timeline"
        subtitle="按 condition_id 聚合 discovery / 决策 / 风控 / 订单 / 成交 / reconcile 全链路"
      />

      <SectionCard title="查询">
        <Group align="flex-end" gap="sm" wrap="wrap">
          <TextInput
            size="xs"
            label="condition_id"
            placeholder="0x..."
            value={conditionInput}
            onChange={(e) => setConditionInput(e.currentTarget.value)}
            w={420}
          />
          <TextInput
            size="xs"
            label="token_id (可选)"
            placeholder="只聚合该 outcome"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.currentTarget.value)}
            w={300}
          />
          <NumberInput
            size="xs"
            label="limit"
            value={limit}
            onChange={(v) => setLimit(typeof v === 'number' ? v : 1000)}
            min={50}
            max={5000}
            step={100}
            w={120}
          />
          <Button size="xs" color="accent" onClick={handleSubmit} disabled={!conditionInput.trim()}>
            查询
          </Button>
          {query.isFetching ? (
            <Text size="xs" c="dimmed">
              同步中…
            </Text>
          ) : null}
        </Group>
      </SectionCard>

      {!submitted.conditionId ? (
        <EmptyState
          title="输入 condition_id 开始"
          description="顶栏全局搜索 0x 开头可直跳此页；常用市场建议放到收藏。"
        />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : query.data ? (
        <Stack gap="md" mt="md">
          <SectionCard
            title="当前持仓"
            description={query.data.truncated ? '注意：事件已按 limit 截断（保留最近 N 条）' : undefined}
          >
            {query.data.current_position ? (
              <Group gap="lg" wrap="wrap">
                <Stat
                  k="size"
                  v={formatDecimal(query.data.current_position.shares, { dp: 2 })}
                />
                <Stat k="cost" v={formatUsdc(query.data.current_position.cost_usdc)} />
                <Stat
                  k="cash PnL"
                  v={formatUsdc(query.data.current_position.cash_pnl)}
                  tone={pnlTone(query.data.current_position.cash_pnl)}
                />
                <Stat
                  k="realized"
                  v={formatUsdc(query.data.current_position.realized_pnl)}
                  tone={pnlTone(query.data.current_position.realized_pnl)}
                />
                <Stat k="token" v={<CopyableId value={query.data.current_position.token_id} />} />
              </Group>
            ) : (
              <Text size="sm" c="dimmed">
                当前 condition_id 无关联持仓
              </Text>
            )}
          </SectionCard>

          <SectionCard
            title={`事件 ${query.data.event_count} 条${query.data.truncated ? '（已截断）' : ''}`}
          >
            {query.data.events.length === 0 ? (
              <EmptyState title="无事件" description="该 condition_id 在选定窗口内没有可聚合的事件" />
            ) : (
              <Stack gap={0}>
                {query.data.events.map((event, idx) => (
                  <TimelineEventNode
                    key={`${event.kind}_${idx}_${event.timestamp ?? ''}`}
                    event={event}
                    onOpenDetail={event.kind === 'decision' ? () => handleOpenEventDetail(event, setDetailRecordId) : undefined}
                  />
                ))}
              </Stack>
            )}
          </SectionCard>
        </Stack>
      ) : null}

      <DecisionDetailDrawer recordId={detailRecordId} onClose={() => setDetailRecordId(null)} />
    </>
  )
}

function handleOpenEventDetail(event: TimelineEvent, setRecordId: (id: string) => void) {
  if (event.kind === 'decision') setRecordId(event.record_id)
}

function Stat({ k, v, tone }: { k: string; v: React.ReactNode; tone?: 'pos' | 'neg' | 'neutral' }) {
  const color = tone === 'pos' ? 'var(--color-success)' : tone === 'neg' ? 'var(--color-danger)' : undefined
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed">
        {k}
      </Text>
      <Text size="sm" fw={500} c={color}>
        {v}
      </Text>
    </Stack>
  )
}
