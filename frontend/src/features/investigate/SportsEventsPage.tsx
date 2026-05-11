import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button, Group, NumberInput, Stack, Text, TextInput } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { sportsApi } from '@core/api/resources'
import type { SportsLiveEvent } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { JsonPanel } from '@shared/ui/JsonPanel'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { formatIso } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'

// 体育实时事件历史：GET /sports/live-events 拉 audit_events filter
// event_title='sports_live_state_recorded'。payload 含 source / observed_at /
// signal_allowed / signal_reason / phase / match_payload / score / clock。

export function SportsEventsPage() {
  const navigate = useNavigate()
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const [conditionId, setConditionId] = useState('')
  const [limit, setLimit] = useState(200)
  const [submitted, setSubmitted] = useState<{
    limit: number
    condition_id?: string
    since?: number
    until?: number
  } | null>(null)
  const [openEventId, setOpenEventId] = useState<string | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.sports.liveEvents(submitted) : ['sports', 'live-events', 'idle'],
    queryFn: ({ signal }) => sportsApi.liveEvents(submitted!, signal),
    enabled: Boolean(submitted),
  })

  const handleQuery = () =>
    setSubmitted({
      limit,
      condition_id: conditionId.trim() || undefined,
      since: since ?? undefined,
      until: until ?? undefined,
    })

  const columns: ColumnDef<SportsLiveEvent, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    {
      header: 'source',
      cell: ({ row }) => {
        const p = row.original.payload ?? {}
        return <code style={{ fontSize: 11 }}>{p.source ?? '—'}</code>
      },
    },
    {
      header: 'phase',
      cell: ({ row }) => {
        const p = row.original.payload ?? {}
        return p.phase ? <StatusPill size="xs">{p.phase}</StatusPill> : <span>—</span>
      },
    },
    {
      header: 'signal_allowed',
      cell: ({ row }) => {
        const allowed = row.original.payload?.signal_allowed
        if (allowed === null || allowed === undefined) return <span style={{ color: 'var(--color-text-dim)' }}>—</span>
        return (
          <StatusPill tone={allowed ? 'success' : 'danger'} size="xs">
            {String(allowed)}
          </StatusPill>
        )
      },
    },
    {
      header: 'reason',
      cell: ({ row }) => {
        const p = row.original.payload ?? {}
        return p.signal_reason ? <code style={{ fontSize: 11 }}>{p.signal_reason}</code> : '—'
      },
    },
    {
      header: 'score / clock',
      cell: ({ row }) => {
        // sports_live_state_worker payload: clock/score 在 match_payload 内（strategy-defined）。
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        const matchPayload = (p.match_payload ?? {}) as Record<string, unknown>
        const clockRaw = matchPayload.clock ?? p.clock
        const clock = typeof clockRaw === 'string' ? clockRaw : ''
        const scoreRaw = matchPayload.score ?? p.score
        const score =
          scoreRaw && typeof scoreRaw === 'object'
            ? JSON.stringify(scoreRaw).slice(0, 60)
            : typeof scoreRaw === 'string'
              ? scoreRaw
              : ''
        return (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>
            {clock ? `${clock} · ` : ''}
            {score}
          </span>
        )
      },
    },
    {
      header: 'condition',
      cell: ({ row }) => <CopyableId value={row.original.condition_id ?? ''} head={4} tail={4} />,
    },
    {
      header: '操作',
      cell: ({ row }) => (
        <Group gap={6}>
          <InlineActionButton
            variant="link"
            onClick={(e) => {
              e.stopPropagation()
              setOpenEventId(openEventId === row.original.event_id ? null : row.original.event_id)
            }}
          >
            {openEventId === row.original.event_id ? '收起' : 'payload'}
          </InlineActionButton>
          {row.original.condition_id ? (
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                navigate(
                  `/investigate/timeline?condition_id=${encodeURIComponent(row.original.condition_id ?? '')}`,
                )
              }}
            >
              timeline
            </InlineActionButton>
          ) : null}
        </Group>
      ),
    },
  ]

  const openEvent = query.data?.items.find((e) => e.event_id === openEventId)

  return (
    <>
      <PageHeader
        title="体育实时事件历史 Sports Live Events"
        subtitle="比分 / 时钟 / 赛况快照——复盘『为什么 t=00:00:12 决定入场』必备"
      />
      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" wrap="wrap" align="flex-end">
          <TimeWindowPicker />
          <TextInput
            size="xs"
            label="condition_id (可选)"
            value={conditionId}
            onChange={(e) => setConditionId(e.currentTarget.value)}
            w={320}
          />
          <NumberInput
            size="xs"
            label="limit"
            value={limit}
            onChange={(v) => setLimit(typeof v === 'number' ? v : 200)}
            min={1}
            max={2000}
            step={50}
            w={120}
          />
        </Group>
        <Button size="xs" color="accent" onClick={handleQuery}>
          查询
        </Button>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <Stack gap="md">
          <SectionCard title={`事件 ${query.data?.items?.length ?? 0} 条`}>
            <DataTable<SportsLiveEvent>
              columns={columns}
              data={query.data?.items}
              isLoading={query.isLoading}
              rowKey={(e) => e.event_id}
            />
          </SectionCard>
          {openEvent ? (
            <SectionCard
              title={`Event ${openEvent.event_id.slice(0, 12)}… payload`}
              description={`${formatIso(openEvent.created_at)} · source=${openEvent.payload?.source ?? '—'}`}
            >
              <JsonPanel value={openEvent.payload ?? {}} maxHeight={360} />
              <Text size="xs" c="dimmed" mt="xs">
                字段含义参考后端 <code>workers/sports_live_state_worker</code>；不同 source（如 sportradar /
                opticodds / espn）的 payload 形态可能不同。
              </Text>
            </SectionCard>
          ) : null}
        </Stack>
      )}
    </>
  )
}

