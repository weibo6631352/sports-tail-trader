import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, SimpleGrid, Stack, Text, TextInput, Textarea } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useState } from 'react'
import { qk, qkRoots } from '@core/api/keys'
import { healthApi, marketsApi, operationsApi } from '@core/api/resources'
import { describeError } from '@core/api/errors'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { confirmAction } from '@shared/forms/confirmAction'
import { useOperatorStore } from '@core/identity/store'

export function OperationsPage() {
  const client = useQueryClient()
  const runtime = useQuery({
    queryKey: qk.runtime(),
    queryFn: ({ signal }) => healthApi.runtime(signal),
  })
  const auto = Boolean(runtime.data?.automatic_trading_enabled ?? runtime.data?.settings?.automatic_trading_enabled)
  const operator = useOperatorStore((s) => s.operator)

  const pauseMutation = useMutation({
    mutationFn: operationsApi.pauseTrading,
    onSuccess: () => {
      notifications.show({ title: '全局已暂停', message: '已写入审计', color: 'yellow' })
      client.invalidateQueries({ queryKey: qkRoots.runtime })
      client.invalidateQueries({ queryKey: qkRoots.ready })
    },
    onError: (err) => notifications.show({ title: '失败', message: describeError(err), color: 'red' }),
  })
  const resumeMutation = useMutation({
    mutationFn: operationsApi.resumeTrading,
    onSuccess: () => {
      notifications.show({ title: '全局已恢复', message: '已写入审计', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.runtime })
      client.invalidateQueries({ queryKey: qkRoots.ready })
    },
    onError: (err) => notifications.show({ title: '失败', message: describeError(err), color: 'red' }),
  })

  const reconcileMutation = useMutation({
    mutationFn: operationsApi.reconcile,
    onSuccess: () => {
      notifications.show({ title: 'reconcile 已触发', message: '结果以 outbox 事件为准', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.operations })
    },
    onError: (err) => notifications.show({ title: 'reconcile 失败', message: describeError(err), color: 'red' }),
  })

  return (
    <>
      <PageHeader
        title="手工干预 Operations"
        subtitle="全局 / 单市场暂停恢复 · reconcile · virtual paper trade"
        actions={
          <StatusPill tone={auto ? 'success' : 'warning'}>
            {auto ? '自动交易开' : '自动交易停'}
          </StatusPill>
        }
      />

      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
        <SectionCard title="全局自动交易开关" description="影响所有市场；按钮按当前状态联动">
          <Stack gap="sm">
            <Text size="xs" c="dimmed">
              当前状态：{auto ? '运行中' : '已暂停'}
            </Text>
            {auto ? (
              <Button
                color="yellow"
                size="xs"
                onClick={() =>
                  confirmAction({
                    title: '暂停全局自动交易',
                    description: '所有市场将停止自动开新单；已挂订单不受影响。',
                    tone: 'warning',
                    onConfirm: async ({ operator: op, reason }) =>
                      pauseMutation.mutateAsync({ reason, operator: op }),
                  })
                }
              >
                暂停自动交易
              </Button>
            ) : (
              <Button
                color="accent"
                size="xs"
                onClick={() =>
                  confirmAction({
                    title: '恢复全局自动交易',
                    description: '所有市场将恢复自动开新单。',
                    onConfirm: async ({ operator: op }) => resumeMutation.mutateAsync({ operator: op }),
                  })
                }
              >
                恢复自动交易
              </Button>
            )}
          </Stack>
        </SectionCard>

        <SectionCard title="触发 Reconcile" description="对账：把链上 / 内存 / DB 三者重新对齐">
          <Stack gap="sm">
            <Text size="xs" c="dimmed">
              不传 condition_ids 表示全量；建议先排查范围再定向 reconcile。
            </Text>
            <Button
              color="accent"
              size="xs"
              onClick={() =>
                confirmAction({
                  title: 'Reconcile 全量',
                  description: '将扫描所有 tracked 市场重新对齐状态；结果以 outbox reconcile_* 事件为准。',
                  onConfirm: async ({ trace_id }) => reconcileMutation.mutateAsync({ trace_id }),
                })
              }
            >
              触发全量 reconcile
            </Button>
          </Stack>
        </SectionCard>

        <MarketPauseCard operator={operator} />
        <VirtualPaperTradeCard />
      </SimpleGrid>
    </>
  )
}

function MarketPauseCard({ operator: _operator }: { operator: string }) {
  const client = useQueryClient()
  const [conditionId, setConditionId] = useState('')
  const [reason, setReason] = useState('manual_pause')

  const pause = useMutation({
    mutationFn: marketsApi.pause,
    onSuccess: () => {
      notifications.show({ title: '市场已暂停', message: '已写入审计', color: 'yellow' })
      client.invalidateQueries({ queryKey: qkRoots.markets })
    },
    onError: (err) => notifications.show({ title: '失败', message: describeError(err), color: 'red' }),
  })
  const resume = useMutation({
    mutationFn: marketsApi.resume,
    onSuccess: () => {
      notifications.show({ title: '市场已恢复', message: '已写入审计', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.markets })
    },
    onError: (err) => notifications.show({ title: '失败', message: describeError(err), color: 'red' }),
  })

  return (
    <SectionCard title="单市场暂停 / 恢复" description="按 condition_id 精准控制">
      <Stack gap="xs">
        <TextInput
          size="xs"
          placeholder="condition_id (0x...)"
          value={conditionId}
          onChange={(e) => setConditionId(e.currentTarget.value)}
        />
        <Textarea size="xs" placeholder="reason" minRows={1} autosize value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Stack gap={6}>
          <Button
            color="yellow"
            size="xs"
            disabled={!conditionId.trim() || !reason.trim()}
            onClick={() =>
              confirmAction({
                title: `暂停市场 ${conditionId.slice(0, 12)}…`,
                tone: 'warning',
                defaultReason: reason,
                onConfirm: async ({ operator: op, reason: r }) =>
                  pause.mutateAsync({ condition_id: conditionId.trim(), reason: r, operator: op }),
              })
            }
          >
            暂停
          </Button>
          <Button
            color="accent"
            size="xs"
            disabled={!conditionId.trim()}
            onClick={() =>
              confirmAction({
                title: `恢复市场 ${conditionId.slice(0, 12)}…`,
                onConfirm: async ({ operator: op }) =>
                  resume.mutateAsync({ condition_id: conditionId.trim(), operator: op }),
              })
            }
          >
            恢复
          </Button>
        </Stack>
      </Stack>
    </SectionCard>
  )
}

function VirtualPaperTradeCard() {
  const [conditionId, setConditionId] = useState('')
  const [tokenId, setTokenId] = useState('')
  const [marketSlug, setMarketSlug] = useState('')

  const trade = useMutation({
    mutationFn: operationsApi.virtualPaperTrade,
    onSuccess: (data) => {
      notifications.show({
        title: 'paper trade 完成',
        message: `status=${data.status}${data.reason ? ` reason=${data.reason}` : ''}`,
        color: data.status === 'ok' ? 'teal' : 'yellow',
      })
    },
    onError: (err) => notifications.show({ title: '失败', message: describeError(err), color: 'red' }),
  })

  const canRun = Boolean(conditionId.trim() || tokenId.trim() || marketSlug.trim())

  return (
    <SectionCard title="Virtual Paper Trade" description="干跑入场计划，不真实下单">
      <Stack gap="xs">
        <TextInput size="xs" placeholder="condition_id" value={conditionId} onChange={(e) => setConditionId(e.currentTarget.value)} />
        <TextInput size="xs" placeholder="token_id" value={tokenId} onChange={(e) => setTokenId(e.currentTarget.value)} />
        <TextInput size="xs" placeholder="market_slug" value={marketSlug} onChange={(e) => setMarketSlug(e.currentTarget.value)} />
        <Button
          size="xs"
          color="accent"
          disabled={!canRun || trade.isPending}
          loading={trade.isPending}
          onClick={() =>
            trade.mutate({
              condition_id: conditionId.trim() || undefined,
              token_id: tokenId.trim() || undefined,
              market_slug: marketSlug.trim() || undefined,
            })
          }
        >
          运行
        </Button>
      </Stack>
    </SectionCard>
  )
}
