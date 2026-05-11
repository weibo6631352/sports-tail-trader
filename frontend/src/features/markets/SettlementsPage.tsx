import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Button,
  Group,
  Modal,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconPlus } from '@tabler/icons-react'
import type { ColumnDef } from '@tanstack/react-table'
import { qk, qkRoots } from '@core/api/keys'
import { marketsApi } from '@core/api/resources'
import { ApiError, describeError } from '@core/api/errors'
import type { AuditEventRow, MarketSettlement } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'
import { CopyableId } from '@shared/ui/CopyableId'
import { JsonPanel } from '@shared/ui/JsonPanel'
import { MonoCell } from '@shared/ui/MonoCell'
import { OperatorReasonFields } from '@shared/forms/OperatorReasonFields'
import { useOperatorStore } from '@core/identity/store'
import { formatDecimal, formatIso, pnlTone } from '@shared/format'

const PAGE_SIZE = 100

// 单 condition 查询 + 全量列表两 tab；顶部「+ 新增结算」抽屉手工录入 ground truth
// 给 calibration / Brier 用——目前没自动 settlement 抓取链路。

export function SettlementsPage() {
  const [settleOpen, setSettleOpen] = useState(false)

  return (
    <>
      <PageHeader
        title="结算对照 Settlements"
        subtitle="结算 → fair_value 偏差 → 定价模型反馈"
        actions={
          <Button size="xs" color="accent" leftSection={<IconPlus size={14} />} onClick={() => setSettleOpen(true)}>
            新增结算
          </Button>
        }
      />
      <Tabs defaultValue="list">
        <Tabs.List>
          <Tabs.Tab value="list">历史列表</Tabs.Tab>
          <Tabs.Tab value="single">单市场详情</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="list" pt="sm">
          <SettlementsList />
        </Tabs.Panel>
        <Tabs.Panel value="single" pt="sm">
          <SingleSettlement />
        </Tabs.Panel>
      </Tabs>

      <SettleMarketModal opened={settleOpen} onClose={() => setSettleOpen(false)} />
    </>
  )
}

function SettlementsList() {
  const [page, setPage] = useState(1)
  const params = useMemo(() => ({ limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }), [page])
  const query = useQuery({
    queryKey: qk.markets.settlements(params),
    queryFn: ({ signal }) => marketsApi.settlements(params, signal),
  })

  const columns: ColumnDef<AuditEventRow, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    {
      header: 'condition',
      cell: ({ row }) => <CopyableId value={row.original.condition_id ?? ''} dense />,
    },
    {
      header: 'winning_token_id',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        const winner = p.winning_token_id ? String(p.winning_token_id) : null
        return winner ? <CopyableId value={winner} dense /> : <span>—</span>
      },
    },
    {
      header: 'outcome',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        return p.winning_outcome ? <MonoCell>{String(p.winning_outcome)}</MonoCell> : '—'
      },
    },
    {
      header: 'source',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        return p.source ? <MonoCell>{String(p.source)}</MonoCell> : '—'
      },
    },
    { header: 'operator', accessorKey: 'operator' },
  ]

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <DataTable<AuditEventRow>
      columns={columns}
      data={query.data?.items}
      isLoading={query.isLoading}
      isFetching={query.isFetching}
      error={query.error}
      onRefresh={() => query.refetch()}
      pagination={{ page, pageCount: totalPages, onChange: setPage }}
      rowKey={(r) => r.event_id}
    />
  )
}

function SingleSettlement() {
  const [conditionInput, setConditionInput] = useState('')
  const [submitted, setSubmitted] = useState<string | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.markets.settlement(submitted) : ['markets', 'settlement', 'idle'],
    queryFn: ({ signal }) => marketsApi.settlement(submitted!, signal),
    enabled: Boolean(submitted),
    retry: false,
  })

  const isNotFound = query.error instanceof ApiError && query.error.status === 404

  return (
    <Stack gap="md">
      <Group gap="sm" align="flex-end">
        <TextInput
          size="xs"
          label="condition_id"
          placeholder="0x..."
          value={conditionInput}
          onChange={(e) => setConditionInput(e.currentTarget.value)}
          w={420}
        />
        <Button size="xs" color="accent" disabled={!conditionInput.trim()} onClick={() => setSubmitted(conditionInput.trim())}>
          查询
        </Button>
      </Group>

      {!submitted ? (
        <EmptyState title="输入 condition_id 查询单市场结算" />
      ) : isNotFound ? (
        <EmptyState
          title="该 condition_id 暂无结算"
          description="可点顶部「新增结算」手工录入；calibration / Brier 需要这个 ground truth。"
        />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : query.data ? (
        <SettlementDetail data={query.data} />
      ) : null}
    </Stack>
  )
}

function SettlementDetail({ data }: { data: MarketSettlement }) {
  const dev = data.fair_value_deviation
  return (
    <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
      <SectionCard title="结算">
        <Stack gap={4}>
          <KV k="condition_id" v={<CopyableId value={data.condition_id} />} />
          <KV
            k="winning_token_id"
            v={data.winning_token_id ? <CopyableId value={data.winning_token_id} /> : <span>—</span>}
          />
          <KV k="winning_outcome" v={data.winning_outcome ?? '—'} />
          <KV k="settled_at" v={formatIso(data.settled_at)} />
          <KV k="source / operator" v={`${data.source ?? '—'} / ${data.operator ?? '—'}`} />
        </Stack>
      </SectionCard>

      <SectionCard
        title="定价复盘"
        description="正偏差 = 当时低估了赢家；负偏差 = 高估了赢家"
      >
        <Stack gap={4}>
          <KV k="last accepted fair_value" v={formatDecimal(data.last_accepted_fair_value, { dp: 4 })} />
          <KV
            k="fair_value_deviation"
            v={
              <span style={{ color: deviationColor(dev) }}>
                {formatDecimal(dev, { dp: 4, signed: true })}
              </span>
            }
          />
        </Stack>
      </SectionCard>

      {data.raw_payload && Object.keys(data.raw_payload).length > 0 ? (
        <SectionCard title="原始 payload" style={{ gridColumn: '1 / -1' }}>
          <JsonPanel value={data.raw_payload} maxHeight={300} />
        </SectionCard>
      ) : null}
    </SimpleGrid>
  )
}

function SettleMarketModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  // 外层只控制 Modal 显示；表单本体在 SettleMarketForm 内部。
  // 用 key={opened} 让每次"打开"重新挂载 Form——state 自然 reset，且 exit 动画
  // 期间内容仍然渲染（避免内容 snap 消失再 backdrop 淡出的视觉跳变）。
  // closeOnClickOutside / closeOnEscape 都关：用户在填表，背景点击不应该清空。
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="手工录入市场结算"
      size="md"
      centered
      closeOnClickOutside={false}
      closeOnEscape={false}
    >
      <SettleMarketForm key={opened ? 'open' : 'closed'} onClose={onClose} />
    </Modal>
  )
}

function SettleMarketForm({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const storedOperator = useOperatorStore((s) => s.operator)
  const [conditionId, setConditionId] = useState('')
  const [winningTokenId, setWinningTokenId] = useState('')
  const [winningOutcome, setWinningOutcome] = useState('')
  const [source, setSource] = useState('manual')
  const [operator, setOperator] = useState(storedOperator)
  const [reason, setReason] = useState('manual_settlement_entry')

  const settle = useMutation({
    mutationFn: marketsApi.settle,
    onSuccess: () => {
      notifications.show({
        title: '已录入',
        message: 'market_settled 事件已写入 audit_events；calibration 下次查询即生效',
        color: 'teal',
      })
      client.invalidateQueries({ queryKey: qkRoots.markets })
      client.invalidateQueries({ queryKey: qkRoots.auditEvents })
      client.invalidateQueries({ queryKey: qkRoots.analytics })
      onClose()
    },
    onError: (err) =>
      notifications.show({ title: '录入失败', message: describeError(err), color: 'red' }),
  })

  const canSubmit =
    conditionId.trim().length > 0 &&
    winningTokenId.trim().length > 0 &&
    operator.trim().length > 0 &&
    reason.trim().length > 0

  return (
    <>
      <Stack gap="sm">
        <Text size="xs" c="dimmed">
          没有自动 settlement 抓取链路时，运维确认 outcome 后写入；用于 calibration / Brier 的 ground truth。
          重复 condition_id 后端按最新一条覆盖。
        </Text>
        <TextInput
          size="xs"
          label="condition_id"
          required
          placeholder="0x..."
          value={conditionId}
          onChange={(e) => setConditionId(e.currentTarget.value)}
        />
        <TextInput
          size="xs"
          label="winning_token_id"
          required
          placeholder="赢家 token_id"
          value={winningTokenId}
          onChange={(e) => setWinningTokenId(e.currentTarget.value)}
        />
        <TextInput
          size="xs"
          label="winning_outcome (可选)"
          placeholder="例如 'Yes' / 'No' / 'Team A'"
          value={winningOutcome}
          onChange={(e) => setWinningOutcome(e.currentTarget.value)}
        />
        <TextInput
          size="xs"
          label="source"
          placeholder="manual / polymarket_resolution / external_oracle..."
          value={source}
          onChange={(e) => setSource(e.currentTarget.value)}
        />
        <OperatorReasonFields
          operator={operator}
          reason={reason}
          onOperatorChange={setOperator}
          onReasonChange={setReason}
        />
        <Group justify="flex-end">
          <Button size="xs" variant="default" onClick={onClose} disabled={settle.isPending}>
            取消
          </Button>
          <Button
            size="xs"
            color="accent"
            loading={settle.isPending}
            disabled={!canSubmit || settle.isPending}
            onClick={() =>
              settle.mutate({
                condition_id: conditionId.trim(),
                winning_token_id: winningTokenId.trim(),
                winning_outcome: winningOutcome.trim() || null,
                source: source.trim() || 'manual',
                operator: operator.trim(),
              })
            }
          >
            录入
          </Button>
        </Group>
      </Stack>
    </>
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

function deviationColor(dev: string | null | undefined): string | undefined {
  const tone = pnlTone(dev)
  if (tone === 'pos') return 'var(--color-warning)' // 低估赢家 = 模型保守
  if (tone === 'neg') return 'var(--color-danger)' // 高估赢家但没赢 = 模型激进
  return undefined
}

