import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notifications } from '@mantine/notifications'
import { Group, NumberInput, Modal, Button, Text, Stack } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk, qkRoots } from '@core/api/keys'
import { ordersApi } from '@core/api/resources'
import type { OrderRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { confirmAction } from '@shared/forms/confirmAction'
import { OperatorReasonFields } from '@shared/forms/OperatorReasonFields'
import { generateManualTraceId } from '@shared/forms/manualTraceId'
import { useOperatorStore } from '@core/identity/store'
import { describeError } from '@core/api/errors'
import { formatDecimal, formatIso } from '@shared/format'

const PAGE_SIZE = 100

export function LiveOrdersPage() {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const [replaceTarget, setReplaceTarget] = useState<OrderRow | null>(null)

  const params = { limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE, open_only: true }
  const query = useQuery({
    queryKey: qk.orders.list(params),
    queryFn: ({ signal }) => ordersApi.list(params, signal),
  })

  const cancelMutation = useMutation({
    mutationFn: ordersApi.cancel,
    onSuccess: () => {
      notifications.show({ title: 'cancel 已提交', message: '等待 ack', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.orders })
    },
    onError: (err) => notifications.show({ title: 'cancel 失败', message: describeError(err), color: 'red' }),
  })

  const replaceMutation = useMutation({
    mutationFn: ordersApi.replace,
    onSuccess: () => {
      notifications.show({ title: 'replace 已提交', message: '等待 ack', color: 'teal' })
      setReplaceTarget(null)
      client.invalidateQueries({ queryKey: qkRoots.orders })
    },
    onError: (err) => notifications.show({ title: 'replace 失败', message: describeError(err), color: 'red' }),
  })

  const columns: ColumnDef<OrderRow, unknown>[] = useMemo(
    () => [
      {
        header: 'order_id',
        cell: ({ row }) => <CopyableId value={row.original.order_id} />,
      },
      { header: 'market', accessorKey: 'market_slug' },
      {
        header: 'side',
        cell: ({ row }) => (
          <StatusPill tone={row.original.side === 'BUY' ? 'success' : 'warning'} size="xs">
            {row.original.side}
          </StatusPill>
        ),
      },
      {
        header: 'price',
        cell: ({ row }) => formatDecimal(row.original.price, { dp: 4 }),
      },
      {
        header: 'filled / total',
        cell: ({ row }) =>
          `${formatDecimal(row.original.filled_shares, { dp: 2 })} / ${formatDecimal(row.original.size_shares, { dp: 2 })}`,
      },
      {
        header: 'status',
        cell: ({ row }) => (
          <StatusPill tone="info" size="xs">
            {row.original.status}
          </StatusPill>
        ),
      },
      {
        header: 'created',
        cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss'),
      },
      {
        header: '操作',
        cell: ({ row }) => (
          <Group gap={6}>
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                setReplaceTarget(row.original)
              }}
            >
              replace
            </InlineActionButton>
            <InlineActionButton
              variant="danger"
              onClick={(e) => {
                e.stopPropagation()
                const o = row.original
                confirmAction({
                  title: `Cancel order ${o.order_id?.slice(0, 8)}…`,
                  description: '撤单不可恢复；已成交部分不受影响。',
                  tone: 'danger',
                  onConfirm: async ({ operator, reason, trace_id }) => {
                    await cancelMutation.mutateAsync({
                      order_id: o.order_id,
                      operator,
                      reason,
                      trace_id,
                    })
                  },
                })
              }}
            >
              cancel
            </InlineActionButton>
          </Group>
        ),
      },
    ],
    [cancelMutation],
  )

  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? query.data?.items?.length ?? 0) / PAGE_SIZE))

  return (
    <>
      <PageHeader title="开口订单" subtitle="open_only=true · replace 改价 / cancel 撤单 · 都走二次确认" />
      <DataTable<OrderRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(o) => o.order_id}
      />

      <ReplaceModal
        target={replaceTarget}
        onClose={() => setReplaceTarget(null)}
        loading={replaceMutation.isPending}
        onSubmit={(payload) => replaceMutation.mutateAsync(payload)}
      />
    </>
  )
}

function ReplaceModal({
  target,
  onClose,
  loading,
  onSubmit,
}: {
  target: OrderRow | null
  onClose: () => void
  loading: boolean
  onSubmit: (payload: {
    order_id: string
    new_price: string
    size_shares?: string
    operator: string
    reason: string
    trace_id: string
  }) => Promise<unknown>
}) {
  // 外层只挂 Modal；表单用 key={order_id} 让换不同订单时重新挂载（state reset）。
  // 关闭瞬间 target=null，但保留上一笔 target 让 ReplaceForm 在 exit 动画期间仍可见，
  // 避免 form 内容 snap 消失。closeOnClickOutside 关——用户填了 new_price 不应被误点清空。
  // 用 state + useEffect（而非 ref）跟踪上一笔 target，符合 React Compiler 的纯渲染要求。
  // setState in effect 是合理的"外部输入源同步"——target 由父组件控制，本组件
  // 派生镜像状态以让 exit 动画期间保留旧值。
  const [lastTarget, setLastTarget] = useState<OrderRow | null>(target)
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (target) setLastTarget(target)
  }, [target])
  /* eslint-enable react-hooks/set-state-in-effect */
  const renderTarget = target ?? lastTarget

  return (
    <Modal
      opened={!!target}
      onClose={onClose}
      title={
        renderTarget ? `Replace order ${renderTarget.order_id.slice(0, 12)}…` : 'Replace order'
      }
      size="md"
      closeOnClickOutside={false}
      closeOnEscape={false}
    >
      {renderTarget ? (
        <ReplaceForm
          key={renderTarget.order_id}
          target={renderTarget}
          onClose={onClose}
          loading={loading}
          onSubmit={onSubmit}
        />
      ) : null}
    </Modal>
  )
}

function ReplaceForm({
  target,
  onClose,
  loading,
  onSubmit,
}: {
  target: OrderRow
  onClose: () => void
  loading: boolean
  onSubmit: (payload: {
    order_id: string
    new_price: string
    size_shares?: string
    operator: string
    reason: string
    trace_id: string
  }) => Promise<unknown>
}) {
  const storedOperator = useOperatorStore((s) => s.operator)
  const [operator, setOperator] = useState(storedOperator)
  const [reason, setReason] = useState('admin_replace_order')
  const [newPrice, setNewPrice] = useState<number | string>(target.price ?? 0)
  // 与 confirmAction 一样在 modal 挂载时生成 trace_id 一次；失败重试不变，审计可串。
  const traceIdRef = useRef<string>(generateManualTraceId())

  const canSubmit =
    operator.trim().length > 0 &&
    reason.trim().length > 0 &&
    typeof newPrice === 'number' &&
    newPrice > 0 &&
    newPrice < 1

  return (
    <Stack gap="sm">
      <Text size="sm" c="dimmed">
        当前 price: {formatDecimal(target.price, { dp: 4 })} · size: {formatDecimal(target.size_shares, { dp: 2 })}
      </Text>
      <NumberInput
        label="New price"
        value={newPrice}
        onChange={(v) => setNewPrice(v)}
        decimalScale={4}
        step={0.001}
        min={0.001}
        max={0.999}
        required
      />
      <OperatorReasonFields
        operator={operator}
        reason={reason}
        onOperatorChange={setOperator}
        onReasonChange={setReason}
      />
      <Group justify="flex-end">
        <Button variant="default" size="xs" onClick={onClose} disabled={loading}>
          取消
        </Button>
        <Button
          color="accent"
          size="xs"
          loading={loading}
          disabled={!canSubmit}
          onClick={() => {
            void onSubmit({
              order_id: target.order_id,
              new_price: String(newPrice),
              operator: operator.trim(),
              reason: reason.trim(),
              trace_id: traceIdRef.current,
            })
          }}
        >
          提交 replace
        </Button>
      </Group>
    </Stack>
  )
}
