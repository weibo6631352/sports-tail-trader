import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { adminApi } from '../../core/api/resources'
import type { TradeReplayRecord } from '../../core/api/types'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { SectionCard } from '../../shared/ui/SectionCard'
import { StatusPill } from '../../shared/ui/StatusPill'
import { formatDateTime, formatDecimal } from '../../shared/utils/format'

const pageSize = 50

const rowKey = (row: TradeReplayRecord): string =>
  [row.condition_id, row.token_id, row.last_fill_at, row.last_position_updated_at].filter(Boolean).join(':')

const pnlTone = (value: string | null | undefined): 'success' | 'warning' | 'danger' | 'neutral' => {
  if (value === null || value === undefined || value === '') {
    return 'neutral'
  }
  const numericValue = Number(value)
  if (Number.isNaN(numericValue) || numericValue === 0) {
    return 'neutral'
  }
  return numericValue > 0 ? 'success' : 'danger'
}

const settlementTone = (value: string): 'success' | 'warning' | 'danger' | 'neutral' => {
  if (value === 'redeemable' || value === 'resolved') {
    return 'success'
  }
  if (value === 'closed') {
    return 'warning'
  }
  return 'neutral'
}

export const TradeReplaysPage = () => {
  const [conditionId, setConditionId] = useState('')
  const [tokenId, setTokenId] = useState('')
  const [traceId, setTraceId] = useState('')
  const [offset, setOffset] = useState(0)
  const [selectedReplay, setSelectedReplay] = useState<TradeReplayRecord | null>(null)

  const replayQuery = useQuery({
    queryKey: ['trade-replays', { conditionId, tokenId, traceId, offset }],
    queryFn: () =>
      adminApi.listTradeReplays({
        limit: pageSize,
        offset,
        condition_id: conditionId || undefined,
        token_id: tokenId || undefined,
        trace_id: traceId || undefined,
      }),
    refetchInterval: 12_000,
  })

  const columns: Array<DataColumn<TradeReplayRecord>> = useMemo(
    () => [
      {
        key: 'market',
        header: '市场',
        cell: (row) => (
          <div className="table-primary">
            {row.market_slug ? (
              <MarketExternalLink className="market-list__title" eventSlug={row.event_slug}>
                {row.event_title ?? row.market_slug}
              </MarketExternalLink>
            ) : (
              <strong>{row.condition_id}</strong>
            )}
            <span>{row.outcome ?? row.token_id}</span>
          </div>
        ),
      },
      {
        key: 'settlement',
        header: '结算状态',
        cell: (row) => <StatusPill label={row.settlement_status} tone={settlementTone(row.settlement_status)} />,
      },
      {
        key: 'buy',
        header: '买入',
        align: 'right',
        cell: (row) => (
          <div className="table-primary table-primary--right">
            <strong>{formatDecimal(row.buy.notional_usdc)}</strong>
            <span>{formatDecimal(row.buy.size)} @ {formatDecimal(row.buy.avg_price)}</span>
          </div>
        ),
      },
      {
        key: 'sell',
        header: '卖出',
        align: 'right',
        cell: (row) => (
          <div className="table-primary table-primary--right">
            <strong>{formatDecimal(row.sell.notional_usdc)}</strong>
            <span>{formatDecimal(row.sell.size)} @ {formatDecimal(row.sell.avg_price)}</span>
          </div>
        ),
      },
      {
        key: 'pnl',
        header: '盈亏',
        align: 'right',
        cell: (row) => (
          <div className="table-primary table-primary--right">
            <strong>
              <StatusPill
                label={formatDecimal(row.pnl.realized_pnl_usdc ?? row.pnl.cash_pnl_usdc)}
                tone={pnlTone(row.pnl.realized_pnl_usdc ?? row.pnl.cash_pnl_usdc)}
              />
            </strong>
            <span>{row.pnl.source}</span>
          </div>
        ),
      },
      {
        key: 'position',
        header: '当前持仓',
        align: 'right',
        cell: (row) => (
          <div className="table-primary table-primary--right">
            <strong>{formatDecimal(row.position?.shares)}</strong>
            <span>成本 {formatDecimal(row.position?.cost_usdc)}</span>
          </div>
        ),
      },
      {
        key: 'activity',
        header: '最近成交',
        cell: (row) => formatDateTime(row.last_fill_at),
      },
    ],
    [],
  )

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">复盘</p>
          <h1>成交与结算回放</h1>
          <p>按市场和 token 聚合订单、成交、持仓 PnL 与候选审计上下文。</p>
        </div>
      </header>

      <SectionCard title="过滤条件" subtitle="复盘入口只读，不触发交易或外部数据请求。">
        <div className="form-grid form-grid--filters">
          <label>
            <span>条件 ID</span>
            <input value={conditionId} onChange={(event) => setConditionId(event.target.value)} />
          </label>
          <label>
            <span>代币 ID</span>
            <input value={tokenId} onChange={(event) => setTokenId(event.target.value)} />
          </label>
          <label>
            <span>追踪 ID</span>
            <input value={traceId} onChange={(event) => setTraceId(event.target.value)} />
          </label>
        </div>
      </SectionCard>

      <div className="content-grid content-grid--wide-aside">
        <SectionCard
          title="复盘记录"
          subtitle={`服务端共 ${replayQuery.data?.total ?? 0} 条，当前第 ${Math.floor(offset / pageSize) + 1} 页。`}
          actions={
            <div className="inline-actions">
              <button type="button" onClick={() => setOffset((current) => Math.max(0, current - pageSize))} disabled={offset === 0}>
                上一页
              </button>
              <button
                type="button"
                onClick={() => setOffset((current) => current + pageSize)}
                disabled={(replayQuery.data?.items.length ?? 0) < pageSize}
              >
                下一页
              </button>
            </div>
          }
        >
          <DataTable
            columns={columns}
            rows={replayQuery.data?.items ?? []}
            rowKey={rowKey}
            emptyTitle="没有复盘记录"
            emptyDescription="当前筛选下没有成交或持仓复盘数据。"
            onRowClick={setSelectedReplay}
            selectedRowKey={selectedReplay ? rowKey(selectedReplay) : null}
          />
        </SectionCard>

        <div className="detail-stack">
          <SectionCard title="盈亏口径" subtitle="优先使用 Data API 持仓 PnL；缺失时退回成交均价口径。">
            <JsonPanel
              value={selectedReplay?.pnl}
              emptyLabel="尚未选择复盘记录。"
              detailsLabel="查看盈亏明细"
              defaultOpen={Boolean(selectedReplay)}
            />
          </SectionCard>

          <SectionCard title="候选上下文" subtitle="展示入场时的比赛状态、匹配结果和退出计划。">
            <JsonPanel
              value={
                selectedReplay
                  ? {
                      sports_tail_game: selectedReplay.sports_tail_game,
                      sports_live_match: selectedReplay.sports_live_match,
                      exit_plan: selectedReplay.exit_plan,
                      candidate_reasons: selectedReplay.candidate_reasons,
                    }
                  : null
              }
              emptyLabel="尚未选择复盘记录。"
              detailsLabel="查看候选上下文"
            />
          </SectionCard>

          <SectionCard title="原始聚合" subtitle="展示服务端聚合后的完整只读记录。">
            <JsonPanel
              value={selectedReplay}
              emptyLabel="尚未选择复盘记录。"
              detailsLabel="查看复盘原始记录"
            />
          </SectionCard>
        </div>
      </div>
    </div>
  )
}
