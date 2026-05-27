import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Group, Select, TextInput, Text, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import type { ColumnDef } from '@tanstack/react-table'
import { qk, qkRoots } from '@core/api/keys'
import { candidatesApi } from '@core/api/resources'
import type { Candidate } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { MonoCell, MonoText, DimText } from '@shared/ui/MonoCell'
import { DataTable } from '@shared/tables/DataTable'
import { confirmAction } from '@shared/forms/confirmAction'
import { describeError } from '@core/api/errors'

const PAGE_SIZE = 100
const REFRESH_INTERVAL_MS = 5000

type Filters = {
  market_type?: string
  action?: string
  execution_permission?: string
  accepted?: 'true' | 'false' | ''
}

// 把 DecimalStr 数字格式化成 pp/百分比/usdc 显示.
function fmtPct(v: unknown, dp = 1): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${(n * 100).toFixed(dp)}%`
}

function fmtPp(v: unknown, dp = 2): string {
  // edge / spread 等 pp (percentage points) 用 ±N.NNpp 显示, 颜色 sign
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${(n * 100).toFixed(dp)}pp`
}

function fmtNum(v: unknown, dp = 3): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toFixed(dp)
}

function fmtUsdc(v: unknown): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `$${n.toFixed(2)}`
}

function edgeTone(v: unknown): 'success' | 'danger' | 'neutral' {
  if (v == null) return 'neutral'
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'neutral'
  return n > 0 ? 'success' : 'danger'
}

export function CandidatesPage() {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const [filters, setFilters] = useState<Filters>({})

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      market_type: filters.market_type || undefined,
      action: filters.action || undefined,
      execution_permission: filters.execution_permission || undefined,
      accepted: filters.accepted ? filters.accepted === 'true' : undefined,
    }),
    [page, filters],
  )

  const query = useQuery({
    queryKey: qk.candidates.list(params),
    queryFn: ({ signal }) => candidatesApi.list(params, signal),
    refetchInterval: REFRESH_INTERVAL_MS,
  })

  const confirmMutation = useMutation({
    mutationFn: candidatesApi.confirm,
    onSuccess: () => {
      notifications.show({ title: '候选已确认', message: '请求已提交，等待后端审计回看', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.candidates })
      client.invalidateQueries({ queryKey: qkRoots.auditEvents })
    },
    onError: (err) => {
      notifications.show({ title: '确认失败', message: describeError(err), color: 'red' })
    },
  })

  const columns: ColumnDef<Candidate, unknown>[] = useMemo(
    () => [
      {
        header: 'market / outcome',
        cell: ({ row }) => {
          const c = row.original
          return (
            <div>
              <div style={{ fontSize: 12, fontWeight: 500 }}>{c.market_slug ?? c.condition_id}</div>
              <div style={{ fontSize: 11, color: 'var(--mantine-color-dimmed)' }}>
                {c.outcome ?? '—'} {c.side ? `· ${c.side}` : ''}
              </div>
              {c.token_id ? (
                <CopyableId value={c.token_id} label="tok" dense />
              ) : null}
            </div>
          )
        },
      },
      {
        header: 'type',
        cell: ({ row }) => (
          <MonoText>{row.original.market_type ?? '—'}</MonoText>
        ),
      },
      {
        // === 量化盯盘核心列 ===
        header: 'prob_p',
        cell: ({ row }) => {
          const k = row.original.intent?.metadata?.kelly
          return (
            <Tooltip
              label={`Goalserve/math fused fair prob · confidence=${fmtNum(k?.prob_confidence, 2)}`}
              withArrow
              disabled={!k?.prob_p}
            >
              <MonoText>{fmtPct(k?.prob_p, 2)}</MonoText>
            </Tooltip>
          )
        },
      },
      {
        header: 'best_ask',
        cell: ({ row }) => {
          const k = row.original.intent?.metadata?.kelly
          const askDisp = row.original.best_ask ?? k?.price_c
          return <MonoText>{fmtNum(askDisp, 3)}</MonoText>
        },
      },
      {
        header: 'edge',
        cell: ({ row }) => {
          const k = row.original.intent?.metadata?.kelly
          return (
            <Tooltip
              label={`gross=${fmtPp(k?.edge_gross)} · fee=${fmtNum(k?.fee_per_share_usdc, 5)}`}
              withArrow
              disabled={!k?.edge_net}
            >
              <span>
                <StatusPill tone={edgeTone(k?.edge_net)} size="xs">
                  {fmtPp(k?.edge_net)}
                </StatusPill>
              </span>
            </Tooltip>
          )
        },
      },
      {
        header: 'f_star',
        cell: ({ row }) => {
          const k = row.original.intent?.metadata?.kelly
          return (
            <Tooltip
              label={`raw Kelly fraction · effective=${fmtPct(k?.effective_kelly_fraction, 1)}${k?.is_round_up_overbet ? ' · round-up overbet' : ''}`}
              withArrow
              disabled={!k?.f_star}
            >
              <MonoText>{fmtPct(k?.f_star, 1)}</MonoText>
            </Tooltip>
          )
        },
      },
      {
        header: 'size',
        cell: ({ row }) => {
          const k = row.original.intent?.metadata?.kelly
          const usdc = k?.buy_budget_usdc ?? row.original.intent?.amount_usdc
          const capped = k?.capped_by
          return (
            <Tooltip
              label={capped ? `capped_by: ${capped}` : 'natural Kelly size'}
              withArrow
              disabled={!usdc}
            >
              <MonoText>{fmtUsdc(usdc)}</MonoText>
            </Tooltip>
          )
        },
      },
      {
        header: 'signal',
        cell: ({ row }) => {
          const c = row.original
          const tone: 'success' | 'warning' | 'neutral' =
            c.signal_allowed === true ? 'success' : c.signal_allowed === false ? 'warning' : 'neutral'
          const ageS = typeof c.live_state_age_ms === 'number' ? Math.floor(c.live_state_age_ms / 1000) : null
          return (
            <div>
              <StatusPill tone={tone} size="xs">
                {c.signal_allowed === true ? '允许' : c.signal_allowed === false ? '跳过' : '—'}
              </StatusPill>
              {ageS !== null ? (
                <div style={{ fontSize: 10, color: 'var(--mantine-color-dimmed)' }}>{ageS}s ago</div>
              ) : null}
            </div>
          )
        },
      },
      {
        header: 'status',
        cell: ({ row }) => {
          const c = row.original
          const tone: 'success' | 'warning' | 'neutral' | 'danger' = c.ready_to_trade
            ? 'success'
            : c.confirmable
              ? 'warning'
              : c.accepted
                ? 'neutral'
                : 'danger'
          const label = c.ready_to_trade
            ? 'ready'
            : c.confirmable
              ? '待确认'
              : c.accepted
                ? 'accepted'
                : 'rejected'
          return (
            <div>
              <StatusPill tone={tone} size="xs">
                {label}
              </StatusPill>
              {c.reason ? (
                <div style={{ fontSize: 10, color: 'var(--mantine-color-dimmed)', marginTop: 2 }}>
                  <MonoCell>{c.reason}</MonoCell>
                </div>
              ) : null}
            </div>
          )
        },
      },
      {
        header: 'exec',
        cell: ({ row }) => (
          <StatusPill
            tone={
              row.original.execution_permission === 'auto_execute'
                ? 'success'
                : row.original.execution_permission === 'manual'
                  ? 'warning'
                  : 'neutral'
            }
            size="xs"
          >
            {row.original.execution_permission ?? '—'}
          </StatusPill>
        ),
      },
      {
        header: '操作',
        cell: ({ row }) => {
          const c = row.original
          if (!c.confirmable || !c.token_id) return <DimText>—</DimText>
          return (
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                confirmAction({
                  title: `确认候选 ${c.market_slug ?? ''}`,
                  description: `市场=${c.market_slug ?? '—'} · token=${c.token_id} · edge=${fmtPp(c.intent?.metadata?.kelly?.edge_net)}`,
                  tone: 'warning',
                  defaultReason: 'manual_confirm_from_admin',
                  onConfirm: async ({ operator, reason, trace_id }) => {
                    await confirmMutation.mutateAsync({
                      token_id: c.token_id!,
                      condition_id: c.condition_id,
                      market_slug: c.market_slug ?? undefined,
                      operator,
                      note: reason,
                      trace_id,
                    })
                  },
                })
              }}
            >
              确认
            </InlineActionButton>
          )
        },
      },
    ],
    [confirmMutation],
  )

  const items = query.data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? items.length) / PAGE_SIZE))

  // 行内观测统计——operator 扫一眼全表 edge / ready 分布
  const stats = useMemo(() => {
    let ready = 0
    let withEdge = 0
    let totalEdge = 0
    for (const c of items) {
      if (c.ready_to_trade) ready++
      const e = c.intent?.metadata?.kelly?.edge_net
      const eNum = e != null ? Number(e) : null
      if (eNum != null && Number.isFinite(eNum)) {
        withEdge++
        totalEdge += eNum
      }
    }
    return {
      total: items.length,
      ready,
      withEdge,
      avgEdge: withEdge > 0 ? totalEdge / withEdge : null,
    }
  }, [items])

  return (
    <>
      <PageHeader
        title="候选 Candidates"
        subtitle="Kelly 内核 + 信号源 + 执行许可一站盯盘 · 5s 自动刷新"
      />

      <Group gap="md" mb="sm">
        <Text size="xs" c="dimmed">总数 <Text component="span" fw={600}>{stats.total}</Text></Text>
        <Text size="xs" c="dimmed">ready <Text component="span" fw={600} c={stats.ready > 0 ? 'teal' : undefined}>{stats.ready}</Text></Text>
        <Text size="xs" c="dimmed">有 edge <Text component="span" fw={600}>{stats.withEdge}</Text></Text>
        <Text size="xs" c="dimmed">avg edge <Text component="span" fw={600}>{stats.avgEdge != null ? fmtPp(stats.avgEdge) : '—'}</Text></Text>
      </Group>

      <Group gap="xs" mb="sm" wrap="wrap">
        <TextInput
          size="xs"
          placeholder="market_type (moneyline/totals/spread)"
          value={filters.market_type ?? ''}
          onChange={(e) => setFilters((f) => ({ ...f, market_type: e.currentTarget.value }))}
          w={220}
        />
        <Select
          size="xs"
          placeholder="action"
          value={filters.action ?? null}
          data={['auto_execute', 'plan_built_not_ready', 'skip']}
          onChange={(value) => setFilters((f) => ({ ...f, action: value ?? undefined }))}
          clearable
          w={180}
        />
        <Select
          size="xs"
          placeholder="execution"
          value={filters.execution_permission ?? null}
          data={['auto_execute', 'manual', 'not_evaluated']}
          onChange={(value) => setFilters((f) => ({ ...f, execution_permission: value ?? undefined }))}
          clearable
          w={150}
        />
        <Select
          size="xs"
          placeholder="accepted"
          value={filters.accepted ?? null}
          data={[
            { value: 'true', label: 'accepted' },
            { value: 'false', label: 'rejected' },
          ]}
          onChange={(value) => setFilters((f) => ({ ...f, accepted: (value as 'true' | 'false' | null) ?? '' }))}
          clearable
          w={120}
        />
      </Group>

      <DataTable<Candidate>
        columns={columns}
        data={items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{
          page,
          pageCount: totalPages,
          onChange: setPage,
        }}
        rowKey={(c) => c.candidate_id ?? `${c.condition_id}_${c.token_id ?? 'none'}`}
      />
    </>
  )
}
