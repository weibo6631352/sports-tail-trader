import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Anchor,
  Badge,
  Button,
  Group,
  NumberInput,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { sportsApi } from '@core/api/resources'
import type { LiveStateRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { JsonPanel } from '@shared/ui/JsonPanel'
import { MonoText } from '@shared/ui/MonoCell'
import { formatIso } from '@shared/format'

// Goalserve 实时直播状态 + 买入机会看板
// 数据来源：GET /sports/live-states（MarketMetadataStore 当前快照）
// payload.goalserve_moneyline 含 home/away 欧赔和隐含概率

type GoalserveOddsMarket = {
  market_name?: string
  home_eu?: number
  away_eu?: number
  home_implied_prob?: number
  away_implied_prob?: number
  suspended?: boolean
  home_suspended?: boolean
  away_suspended?: boolean
}

type GoalserveSpread = GoalserveOddsMarket & {
  home_handicap?: string | null
  away_handicap?: string | null
}

type GoalserveTotals = {
  market_name?: string
  total_line?: string | null
  over_eu?: number
  under_eu?: number
  over_implied_prob?: number
  under_implied_prob?: number
  suspended?: boolean
  over_suspended?: boolean
  under_suspended?: boolean
}

type GoalserveMoneyline = GoalserveOddsMarket

type LiveGame = {
  home_name?: string
  away_name?: string
  home_score?: number | null
  away_score?: number | null
  status?: string
  period?: string
  seconds_remaining?: number | null
  league?: string
  sport?: string
}

function polymarketUrl(slug?: string | null): string | null {
  if (!slug) return null
  return `https://polymarket.com/event/${slug}`
}

// 信号 reason 中文化——operator 一眼看懂跳过/允许原因.
// reason 取自 LiveStateMatch.signal_reason (workflow/live_state.entry_signal_gate).
const SIGNAL_REASON_LABELS: Record<string, string> = {
  live: '进行中',
  ended_not_closed: '已结束·未封盘',
  paused_passthrough: '局间·已放行',
  sports_live_state_postponed: '延期',
  sports_live_state_cancelled: '取消',
  sports_live_state_retired: '弃赛',
  sports_live_state_disputed: '争议',
  sports_live_state_unknown: '状态未知',
  sports_live_state_stale: '数据陈旧',
  // 历史 reason(passthrough 之前的语义,保留以兼容旧 audit)
  sports_live_state_paused: '已暂停',
}

function localizeSignalReason(reason: string | null | undefined): string {
  if (!reason) return ''
  return SIGNAL_REASON_LABELS[reason] ?? reason
}

function formatEu(eu?: number): string {
  if (eu == null) return '—'
  return `${eu.toFixed(2)}x`
}

function formatProb(prob?: number): string {
  if (prob == null) return '—'
  return `${(prob * 100).toFixed(1)}%`
}

function GoalserveOddsCell({ ml }: { ml: GoalserveMoneyline | null | undefined }) {
  if (!ml) return <Text size="xs" c="dimmed">无赔率</Text>
  if (ml.suspended) return <StatusPill tone="warning" size="xs">暂停</StatusPill>
  return (
    <Group gap={6} wrap="nowrap">
      <Tooltip label={`欧赔 ${formatEu(ml.home_eu)}`} withArrow position="top">
        <Badge
          size="xs"
          color={ml.home_suspended ? 'gray' : 'blue'}
          variant="light"
        >
          主 {formatProb(ml.home_implied_prob)}
        </Badge>
      </Tooltip>
      <Tooltip label={`欧赔 ${formatEu(ml.away_eu)}`} withArrow position="top">
        <Badge
          size="xs"
          color={ml.away_suspended ? 'gray' : 'orange'}
          variant="light"
        >
          客 {formatProb(ml.away_implied_prob)}
        </Badge>
      </Tooltip>
    </Group>
  )
}

function GoalserveSpreadCell({ spread }: { spread: GoalserveSpread | null | undefined }) {
  if (!spread) return <Text size="xs" c="dimmed">—</Text>
  if (spread.suspended) return <StatusPill tone="warning" size="xs">暂停</StatusPill>
  const homeHcp = spread.home_handicap ? ` (${spread.home_handicap})` : ''
  const awayHcp = spread.away_handicap ? ` (${spread.away_handicap})` : ''
  return (
    <Group gap={4} wrap="nowrap">
      <Tooltip label={`欧赔 ${formatEu(spread.home_eu)} · 让分${homeHcp}`} withArrow position="top">
        <Badge size="xs" color={spread.home_suspended ? 'gray' : 'teal'} variant="light">
          主{homeHcp} {formatProb(spread.home_implied_prob)}
        </Badge>
      </Tooltip>
      <Tooltip label={`欧赔 ${formatEu(spread.away_eu)} · 让分${awayHcp}`} withArrow position="top">
        <Badge size="xs" color={spread.away_suspended ? 'gray' : 'grape'} variant="light">
          客{awayHcp} {formatProb(spread.away_implied_prob)}
        </Badge>
      </Tooltip>
    </Group>
  )
}

function GoalserveTotalsCell({ totals }: { totals: GoalserveTotals | null | undefined }) {
  if (!totals) return <Text size="xs" c="dimmed">—</Text>
  if (totals.suspended) return <StatusPill tone="warning" size="xs">暂停</StatusPill>
  const line = totals.total_line ? ` ${totals.total_line}` : ''
  return (
    <Group gap={4} wrap="nowrap">
      <Tooltip label={`欧赔 ${formatEu(totals.over_eu)}`} withArrow position="top">
        <Badge size="xs" color={totals.over_suspended ? 'gray' : 'green'} variant="light">
          大{line} {formatProb(totals.over_implied_prob)}
        </Badge>
      </Tooltip>
      <Tooltip label={`欧赔 ${formatEu(totals.under_eu)}`} withArrow position="top">
        <Badge size="xs" color={totals.under_suspended ? 'gray' : 'red'} variant="light">
          小 {formatProb(totals.under_implied_prob)}
        </Badge>
      </Tooltip>
    </Group>
  )
}

function ScoreCell({ game }: { game: LiveGame | null | undefined }) {
  if (!game) return <Text size="xs" c="dimmed">—</Text>
  const score =
    game.home_score != null && game.away_score != null
      ? `${game.home_score} : ${game.away_score}`
      : '—'
  // 显示归一 status (live/paused/ended/scheduled),不再显示 raw period 文本.
  // 之前 period="Started" + status="paused" 同时出现让操盘困惑——period 是博彩
  // 平台用的整场系列标签,不反映当前是否真在打.
  const status = (game.status ?? '').toLowerCase()
  const statusLabel: Record<string, { tone: 'success' | 'warning' | 'neutral' | 'danger'; text: string }> = {
    live: { tone: 'success', text: '进行中' },
    paused: { tone: 'warning', text: '暂停中' },
    ended: { tone: 'neutral', text: '已结束' },
    scheduled: { tone: 'neutral', text: '未开始' },
    postponed: { tone: 'danger', text: '延期' },
    cancelled: { tone: 'danger', text: '取消' },
    retired: { tone: 'danger', text: '弃赛' },
    disputed: { tone: 'danger', text: '争议' },
    unknown: { tone: 'neutral', text: '未知' },
  }
  const meta = statusLabel[status]
  return (
    <Stack gap={2}>
      <MonoText>{score}</MonoText>
      {meta ? (
        <StatusPill tone={meta.tone} size="xs">{meta.text}</StatusPill>
      ) : null}
    </Stack>
  )
}

export function GoalserveLivePage() {
  const [limit, setLimit] = useState(100)
  const [openConditionId, setOpenConditionId] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)

  const query = useQuery({
    queryKey: qk.sports.liveStates({ limit }),
    queryFn: ({ signal }) => sportsApi.liveStates({ limit }, signal),
    refetchInterval: autoRefresh ? 5000 : false,
  })

  const items = query.data?.items ?? []

  // 数据驱动列可见: 让分/大小分 esports 永远 null, 整列折叠避免占空.
  const { hasAnySpread, hasAnyTotals } = useMemo(() => {
    let spread = false
    let totals = false
    for (const row of items) {
      const lsp = row.live_state_payload as Record<string, unknown> | null | undefined
      if (!spread && lsp?.goalserve_spread) spread = true
      if (!totals && lsp?.goalserve_totals) totals = true
      if (spread && totals) break
    }
    return { hasAnySpread: spread, hasAnyTotals: totals }
  }, [items])

  const allColumns: ColumnDef<LiveStateRow, unknown>[] = [
    {
      header: '市场',
      cell: ({ row }) => {
        const slug = row.original.event_slug ?? row.original.market_slug
        const url = polymarketUrl(slug)
        return (
          <Stack gap={2}>
            {url ? (
              <Anchor
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                size="xs"
                style={{ fontFamily: 'monospace', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}
              >
                {row.original.market_slug ?? slug ?? '—'}
              </Anchor>
            ) : (
              <Text size="xs" ff="monospace" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {row.original.market_slug ?? '—'}
              </Text>
            )}
            <CopyableId value={row.original.condition_id ?? ''} dense />
          </Stack>
        )
      },
    },
    {
      header: '比赛',
      cell: ({ row }) => {
        const lsp = row.original.live_state_payload as Record<string, unknown> | null | undefined
        const game = lsp?.live_game as LiveGame | null | undefined
        if (!game) return <Text size="xs" c="dimmed">—</Text>
        const league = game.league ?? ''
        const sport = game.sport ?? ''
        return (
          <Stack gap={0}>
            <Text size="xs">{game.home_name} vs {game.away_name}</Text>
            <Text size="xs" c="dimmed">{[league, sport].filter(Boolean).join(' · ')}</Text>
          </Stack>
        )
      },
    },
    {
      header: '比分 / 节',
      cell: ({ row }) => {
        const lsp = row.original.live_state_payload as Record<string, unknown> | null | undefined
        const game = lsp?.live_game as LiveGame | null | undefined
        return <ScoreCell game={game} />
      },
    },
    {
      header: 'ML 赔率',
      cell: ({ row }) => {
        const lsp = row.original.live_state_payload as Record<string, unknown> | null | undefined
        const ml = lsp?.goalserve_moneyline as GoalserveMoneyline | null | undefined
        return <GoalserveOddsCell ml={ml} />
      },
    },
    {
      header: '让分',
      cell: ({ row }) => {
        const lsp = row.original.live_state_payload as Record<string, unknown> | null | undefined
        const spread = lsp?.goalserve_spread as GoalserveSpread | null | undefined
        return <GoalserveSpreadCell spread={spread} />
      },
    },
    {
      header: '大小分',
      cell: ({ row }) => {
        const lsp = row.original.live_state_payload as Record<string, unknown> | null | undefined
        const totals = lsp?.goalserve_totals as GoalserveTotals | null | undefined
        return <GoalserveTotalsCell totals={totals} />
      },
    },
    {
      header: '信号',
      cell: ({ row }) => {
        const allowed = row.original.live_state_signal_allowed
        const reason = row.original.live_state_signal_reason as string | null | undefined
        const reasonText = localizeSignalReason(reason)
        return (
          <Stack gap={2}>
            {allowed != null ? (
              <StatusPill tone={allowed ? 'success' : 'neutral'} size="xs">
                {allowed ? '允许' : '跳过'}
              </StatusPill>
            ) : null}
            {reasonText ? <Text size="xs" c="dimmed">{reasonText}</Text> : null}
          </Stack>
        )
      },
    },
    {
      header: '更新',
      cell: ({ row }) => (
        <Text size="xs" c="dimmed">
          {formatIso(row.original.updated_at as string | undefined, 'HH:mm:ss')}
        </Text>
      ),
    },
    {
      header: '操作',
      cell: ({ row }) => {
        const cid = row.original.condition_id ?? ''
        const slug = row.original.event_slug ?? row.original.market_slug
        const polyUrl = polymarketUrl(slug)
        return (
          <Group gap={6} wrap="nowrap">
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                setOpenConditionId(openConditionId === cid ? null : cid)
              }}
            >
              {openConditionId === cid ? '收起' : '详情'}
            </InlineActionButton>
            {polyUrl ? (
              <Anchor href={polyUrl} target="_blank" rel="noopener noreferrer" size="xs">
                Polymarket ↗
              </Anchor>
            ) : null}
          </Group>
        )
      },
    },
  ]

  const columns = allColumns.filter((col) => {
    if (col.header === '让分' && !hasAnySpread) return false
    if (col.header === '大小分' && !hasAnyTotals) return false
    return true
  })

  const openRow = items.find((r) => r.condition_id === openConditionId)

  return (
    <>
      <PageHeader
        title="Goalserve 实时直播"
        subtitle="当前有直播状态的市场 · Goalserve 赔率交叉验证 · 买入机会一览"
      />
      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" align="flex-end">
          <NumberInput
            size="xs"
            label="limit"
            value={limit}
            onChange={(v) => setLimit(typeof v === 'number' ? v : 100)}
            min={1}
            max={1000}
            step={50}
            w={120}
          />
          <Button
            size="xs"
            variant={autoRefresh ? 'filled' : 'outline'}
            color="accent"
            onClick={() => setAutoRefresh((v) => !v)}
          >
            {autoRefresh ? '自动刷新 5s ✓' : '自动刷新 (已停)'}
          </Button>
        </Group>
        <Button size="xs" variant="subtle" onClick={() => query.refetch()}>
          手动刷新
        </Button>
      </Group>

      {query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : items.length === 0 && !query.isLoading ? (
        <EmptyState
          title="暂无直播状态"
          description="sports_live_state_worker 尚未写入任何市场的 Goalserve 直播快照，或当前没有符合条件的市场。"
        />
      ) : (
        <Stack gap="md">
          <SectionCard
            title={`直播市场 ${items.length} 个${query.isFetching ? ' (刷新中…)' : ''}`}
          >
            <DataTable<LiveStateRow>
              columns={columns}
              data={items}
              isLoading={query.isLoading}
              rowKey={(r) => r.condition_id ?? r.market_slug ?? Math.random().toString()}
            />
          </SectionCard>

          {openRow ? (
            <SectionCard
              title={`${openRow.market_slug ?? openRow.condition_id} · Goalserve 详情`}
              description={`更新 ${formatIso(openRow.updated_at as string | undefined)}`}
            >
              <JsonPanel
                value={
                  openRow.live_state_payload != null
                    ? openRow.live_state_payload as Record<string, unknown>
                    : openRow as Record<string, unknown>
                }
                maxHeight={480}
              />
            </SectionCard>
          ) : null}
        </Stack>
      )}
    </>
  )
}
