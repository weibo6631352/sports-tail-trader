import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button, Group, NumberInput, SimpleGrid, Slider, Stack, Text, TextInput } from '@mantine/core'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { marketsApi } from '@core/api/resources'
import type { OrderbookSnapshotRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { formatDecimal, formatIso, isoToEpochMs, toDecimal } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'

// 盘口回放：按 token_id + 时间窗拉 orderbook_snapshots，slider 选时间点，
// 显示该时刻的 bids/asks 深度直方图 + 周边事件序列。
// 后端返回 OrderbookSnapshotRow，payload 含 bids/asks 数组（前端解析）。

type Level = { price: number; size: number }

export function OrderbookReplayPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const [tokenId, setTokenId] = useState('')
  const [conditionId, setConditionId] = useState('')
  const [limit, setLimit] = useState(500)
  const [submitted, setSubmitted] = useState<{
    token_id?: string
    condition_id?: string
    since?: number
    until?: number
    limit: number
  } | null>(null)
  const [pointer, setPointer] = useState(0)

  const query = useQuery({
    queryKey: submitted
      ? qk.markets.orderbookHistory(submitted)
      : ['markets', 'orderbook-history', 'idle'],
    queryFn: ({ signal }) => marketsApi.orderbookHistory(submitted!, signal),
    enabled: Boolean(submitted),
  })

  // 后端默认 received_at 倒序，前端按 ASC 排好方便 slider 时间走向。
  const sorted = useMemo(() => {
    const items = query.data?.items ?? []
    return [...items].sort((a, b) => {
      const ta = isoToEpochMs(a.received_at) ?? 0
      const tb = isoToEpochMs(b.received_at) ?? 0
      return ta - tb
    })
  }, [query.data?.items])

  // 数据集变更时把指针夹回有效范围；setState 在 effect 内是必要的（外部输入源驱动）。
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (sorted.length === 0) return
    if (pointer >= sorted.length) {
      setPointer(sorted.length - 1)
    }
  }, [sorted.length, pointer])
  /* eslint-enable react-hooks/set-state-in-effect */

  const handleQuery = () => {
    if (!tokenId.trim() && !conditionId.trim()) return
    setSubmitted({
      token_id: tokenId.trim() || undefined,
      condition_id: conditionId.trim() || undefined,
      since: since ?? undefined,
      until: until ?? undefined,
      limit,
    })
    setPointer(0)
  }

  const current = sorted[pointer]
  const { bids, asks } = useMemo(() => extractLevels(current), [current])

  const histogramData = useMemo(() => {
    // 把 bid / ask 拉到同一条 x 轴：bid 用负值表示 size（向左展开），ask 正值。
    const merged = new Map<number, { price: number; bidSize: number; askSize: number }>()
    for (const b of bids) {
      const entry = merged.get(b.price) ?? { price: b.price, bidSize: 0, askSize: 0 }
      entry.bidSize += b.size
      merged.set(b.price, entry)
    }
    for (const a of asks) {
      const entry = merged.get(a.price) ?? { price: a.price, bidSize: 0, askSize: 0 }
      entry.askSize += a.size
      merged.set(a.price, entry)
    }
    return Array.from(merged.values()).sort((a, b) => a.price - b.price)
  }, [bids, asks])

  const columns: ColumnDef<OrderbookSnapshotRow, unknown>[] = [
    { header: 'received_at', cell: ({ row }) => formatIso(row.original.received_at, 'MM-DD HH:mm:ss.SSS') },
    { header: 'midpoint', cell: ({ row }) => formatDecimal(row.original.midpoint, { dp: 4 }) },
    { header: 'best bid', cell: ({ row }) => formatDecimal(row.original.best_bid, { dp: 4 }) },
    { header: 'best ask', cell: ({ row }) => formatDecimal(row.original.best_ask, { dp: 4 }) },
  ]

  return (
    <>
      <PageHeader
        title="盘口回放 Orderbook Replay"
        subtitle="按 token_id 或 condition_id 拉历史快照；slider 拖动播放"
      />

      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" wrap="wrap" align="flex-end">
          <TimeWindowPicker />
          <TextInput
            size="xs"
            label="token_id"
            value={tokenId}
            onChange={(e) => setTokenId(e.currentTarget.value)}
            w={280}
          />
          <TextInput
            size="xs"
            label="或 condition_id"
            value={conditionId}
            onChange={(e) => setConditionId(e.currentTarget.value)}
            w={300}
          />
          <NumberInput
            size="xs"
            label="limit"
            value={limit}
            onChange={(v) => setLimit(typeof v === 'number' ? v : 500)}
            min={1}
            max={2000}
            step={100}
            w={120}
          />
        </Group>
        <Button size="xs" color="accent" onClick={handleQuery} disabled={!tokenId.trim() && !conditionId.trim()}>
          查询
        </Button>
      </Group>

      {!submitted ? (
        <EmptyState title="输入 token_id 或 condition_id 后查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : sorted.length === 0 && !query.isLoading ? (
        <EmptyState title="窗口内无快照" description="该 token / 条件在选定时间内未落库；尝试放宽时间窗口。" />
      ) : current ? (
        <Stack gap="md">
          <SectionCard title="时间轴">
            <Stack gap="xs">
              <Slider
                min={0}
                max={Math.max(0, sorted.length - 1)}
                value={pointer}
                onChange={setPointer}
                marks={[
                  { value: 0, label: '起' },
                  { value: Math.max(0, sorted.length - 1), label: '末' },
                ]}
              />
              <Group justify="space-between" wrap="wrap">
                <Text size="xs" c="dimmed">
                  {pointer + 1} / {sorted.length} · {formatIso(current.received_at)}
                </Text>
                <Group gap={6}>
                  <Button
                    size="compact-xs"
                    variant="default"
                    disabled={pointer === 0}
                    onClick={() => setPointer((p) => Math.max(0, p - 1))}
                  >
                    ←
                  </Button>
                  <Button
                    size="compact-xs"
                    variant="default"
                    disabled={pointer === sorted.length - 1}
                    onClick={() => setPointer((p) => Math.min(sorted.length - 1, p + 1))}
                  >
                    →
                  </Button>
                </Group>
              </Group>
            </Stack>
          </SectionCard>

          <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
            <SectionCard title="深度直方图（bid / ask）">
              {histogramData.length === 0 ? (
                <Text size="sm" c="dimmed">
                  当前快照无 bids / asks（可能 payload schema 与前端假设不同——后端字段以
                  <code>bids</code> / <code>asks</code> 数组为准）。
                </Text>
              ) : (
                <div style={{ width: '100%', height: 280 }}>
                  <ResponsiveContainer>
                    <BarChart data={histogramData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
                      <XAxis
                        dataKey="price"
                        stroke="#97a6c2"
                        tick={{ fontSize: 10 }}
                        type="number"
                        domain={[0, 1]}
                      />
                      <YAxis stroke="#97a6c2" tick={{ fontSize: 11 }} />
                      <Tooltip contentStyle={{ background: '#121e36', border: '1px solid #243352' }} />
                      <Bar dataKey="bidSize" name="bid" fill="#38d39f" stackId="depth" />
                      <Bar dataKey="askSize" name="ask" fill="#f06568" stackId="depth" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </SectionCard>

            <SectionCard title="当前快照摘要">
              <Stack gap={4}>
                <KV k="received_at" v={formatIso(current.received_at)} />
                <KV k="snapshot_id" v={<code style={{ fontSize: 11 }}>{current.snapshot_id}</code>} />
                <KV k="midpoint" v={formatDecimal(current.midpoint, { dp: 4 })} />
                <KV k="best_bid / best_ask" v={`${formatDecimal(current.best_bid, { dp: 4 })} / ${formatDecimal(current.best_ask, { dp: 4 })}`} />
                <KV k="bids levels" v={String(bids.length)} />
                <KV k="asks levels" v={String(asks.length)} />
              </Stack>
            </SectionCard>
          </SimpleGrid>

          <SectionCard title={`全部快照 ${sorted.length} 条（点行跳到该时刻）`}>
            <DataTable<OrderbookSnapshotRow>
              columns={columns}
              data={sorted}
              rowKey={(s) => s.snapshot_id}
              onRowClick={(row) => {
                const idx = sorted.findIndex((s) => s.snapshot_id === row.original.snapshot_id)
                if (idx >= 0) setPointer(idx)
              }}
            />
          </SectionCard>
        </Stack>
      ) : null}
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

function extractLevels(row: OrderbookSnapshotRow | undefined): { bids: Level[]; asks: Level[] } {
  if (!row) return { bids: [], asks: [] }
  // payload 形态由后端 orderbook_snapshots.payload 决定；常见结构 { bids: [[price, size], ...], asks: ... }
  // 或 { bids: [{price, size}, ...] }。前端兼容两种。
  const payload = row.payload ?? {}
  return {
    bids: parseLevels(payload['bids']),
    asks: parseLevels(payload['asks']),
  }
}

function coerceScalar(value: unknown): string | number | null | undefined {
  if (value === null || value === undefined) return null
  if (typeof value === 'string' || typeof value === 'number') return value
  return String(value)
}

function parseLevels(raw: unknown): Level[] {
  if (!Array.isArray(raw)) return []
  const out: Level[] = []
  for (const item of raw) {
    if (Array.isArray(item) && item.length >= 2) {
      const price = toDecimal(coerceScalar(item[0]))?.toNumber()
      const size = toDecimal(coerceScalar(item[1]))?.toNumber()
      if (price !== undefined && size !== undefined && Number.isFinite(price) && Number.isFinite(size)) {
        out.push({ price, size })
      }
      continue
    }
    if (item && typeof item === 'object') {
      const obj = item as Record<string, unknown>
      const price = toDecimal(coerceScalar(obj.price))?.toNumber()
      const size = toDecimal(coerceScalar(obj.size))?.toNumber()
      if (price !== undefined && size !== undefined && Number.isFinite(price) && Number.isFinite(size)) {
        out.push({ price, size })
      }
    }
  }
  return out
}
