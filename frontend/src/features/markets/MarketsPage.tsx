import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useSearchParams } from 'react-router-dom'
import { formatApiError } from '../../core/api/client'
import { adminApi } from '../../core/api/resources'
import type { MarketView, TakerFeePreview } from '../../core/api/types'
import { SectionCard } from '../../shared/ui/SectionCard'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { StatusPill } from '../../shared/ui/StatusPill'
import { QueryErrorNotice } from '../../shared/ui/QueryErrorNotice'
import { EntityAvatar } from '../../shared/ui/EntityAvatar'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import {
  formatDateTime,
  formatFullDateTime,
  formatDecimal,
  formatList,
  getString,
} from '../../shared/utils/format'
import { formatTradingStatusLabel } from '../../shared/utils/labels'
import {
  getMarketOpenOrderCount,
  getMarketPosition,
  getMarketTokenId,
  getOutcomeTokenView,
  getPrimaryTokenView,
  getSecondaryTokenView,
  marketHasToken,
} from '../../shared/utils/marketViews'
import { resolveExtensionPresentation } from '../../extensions/registry'

const tradingStatusTone = (status: string): 'neutral' | 'success' | 'warning' | 'danger' => {
  if (status === 'active') {
    return 'success'
  }
  if (status === 'paused') {
    return 'warning'
  }
  if (status === 'closed' || status === 'resolved' || status === 'rejected') {
    return 'danger'
  }
  return 'neutral'
}

const tradingStatusOptions = [
  { value: '', label: '全部状态' },
  { value: 'active', label: '交易中' },
  { value: 'paused', label: '已暂停' },
  { value: 'closed', label: '已关闭' },
  { value: 'resolved', label: '已结算' },
  { value: 'rejected', label: '已拒绝' },
]

const feeOptions = [
  { value: 'all', label: '全部' },
  { value: 'true', label: '已开启' },
  { value: 'false', label: '未开启' },
]

const sortOptions = [
  { value: 'fee_rate_updated_at', label: '费率更新时间' },
  { value: 'market_slug', label: '市场标识' },
  { value: 'fee_rate_bps', label: '费率' },
  { value: 'maker_base_fee_bps', label: '挂单费率' },
  { value: 'taker_base_fee_bps', label: '吃单费率' },
]

const feeFormatter = new Intl.NumberFormat('zh-CN', {
  maximumFractionDigits: 5,
})

const formatFeeValue = (value: string | number | null | undefined): string => {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  const numericValue = Number(value)
  if (Number.isNaN(numericValue)) {
    return String(value)
  }
  return feeFormatter.format(numericValue)
}

const resolveFeePerTradeDollar = (
  preview: TakerFeePreview | null | undefined,
  side: 'buy' | 'sell',
): number | null => {
  if (!preview) {
    return null
  }
  const quote = side === 'buy' ? preview.buy : preview.sell
  if (!quote?.fee_usdc || !quote.price || !preview.basis_size_shares) {
    return null
  }
  const feeUsdc = Number(quote.fee_usdc)
  const price = Number(quote.price)
  const basisSizeShares = Number(preview.basis_size_shares)
  if (
    Number.isNaN(feeUsdc) ||
    Number.isNaN(price) ||
    Number.isNaN(basisSizeShares) ||
    price <= 0 ||
    basisSizeShares <= 0
  ) {
    return null
  }
  const basisNotionalUsdc = basisSizeShares * price
  if (basisNotionalUsdc <= 0) {
    return null
  }
  return feeUsdc / basisNotionalUsdc
}

const formatFeePreviewDetail = (
  preview: TakerFeePreview | null | undefined,
  side: 'buy' | 'sell',
): string => {
  if (!preview) {
    return '—'
  }
  const quote = side === 'buy' ? preview.buy : preview.sell
  if (!quote) {
    return '—'
  }
  const feePerTradeDollar = resolveFeePerTradeDollar(preview, side)
  const priceSourceLabel = quote.price_source === 'best_ask' ? '卖一' : '买一'
  return `${formatFeeValue(feePerTradeDollar)} USDC / $1 (${priceSourceLabel} ${formatDecimal(quote.price)})`
}

const formatOutcomeBuySell = (
  buyPrice: string | number | null | undefined,
  sellPrice: string | number | null | undefined,
): string => `买${formatDecimal(buyPrice)} / 卖${formatDecimal(sellPrice)}`

const formatFeePreviewSummary = (preview: TakerFeePreview | null | undefined): string =>
  `买${formatFeeValue(resolveFeePerTradeDollar(preview, 'buy'))} / 卖${formatFeeValue(resolveFeePerTradeDollar(preview, 'sell'))}`

const renderBinaryQuoteSummary = (row: MarketView) => {
  const noView = getOutcomeTokenView(row, 'NO') ?? getPrimaryTokenView(row)
  const yesView = getOutcomeTokenView(row, 'YES') ?? getSecondaryTokenView(row)
  const noBuyPrice = noView?.best_ask ?? noView?.orderbook?.best_ask ?? null
  const noSellPrice = noView?.best_bid ?? noView?.orderbook?.best_bid ?? null
  const yesBuyPrice = yesView?.best_ask ?? yesView?.orderbook?.best_ask ?? null
  const yesSellPrice = yesView?.best_bid ?? yesView?.orderbook?.best_bid ?? null

  return (
    <div className="table-primary">
      <div>{`YES ${formatOutcomeBuySell(yesBuyPrice, yesSellPrice)}`}</div>
      <span>{`YES 手续费 ${formatFeePreviewSummary(yesView?.fee_preview)}`}</span>
      <div>{`NO ${formatOutcomeBuySell(noBuyPrice, noSellPrice)}`}</div>
      <span>{`NO 手续费 ${formatFeePreviewSummary(noView?.fee_preview)}`}</span>
    </div>
  )
}

export const MarketsPage = () => {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const [tradingStatus, setTradingStatus] = useState('')
  const [feesEnabled, setFeesEnabled] = useState('all')
  const [sortBy, setSortBy] = useState('fee_rate_updated_at')
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc')
  const [offset, setOffset] = useState(0)
  const [marketOperator, setMarketOperator] = useState('人工')
  const [marketPauseReason, setMarketPauseReason] = useState('manual_pause')

  const runtimeQuery = useQuery({
    queryKey: ['runtime', 'markets'],
    queryFn: adminApi.getRuntime,
    refetchInterval: 20_000,
  })

  const pauseMarketMutation = useMutation({
    mutationFn: (payload: { condition_id: string; reason: string; operator: string }) =>
      adminApi.pauseMarket(payload),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['markets'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
      ])
    },
  })
  const resumeMarketMutation = useMutation({
    mutationFn: (payload: { condition_id: string; operator: string }) => adminApi.resumeMarket(payload),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['markets'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
      ])
    },
  })
  const pauseMarketError = pauseMarketMutation.error ? formatApiError(pauseMarketMutation.error) : null
  const resumeMarketError = resumeMarketMutation.error ? formatApiError(resumeMarketMutation.error) : null

  const marketsQuery = useQuery({
    queryKey: ['markets', { tradingStatus, feesEnabled, sortBy, sortDirection, offset }],
    queryFn: () =>
      adminApi.listMarkets({
        limit: 50,
        offset,
        trading_status: tradingStatus || undefined,
        fees_enabled: feesEnabled === 'all' ? undefined : feesEnabled === 'true',
        sort_by: sortBy || undefined,
        sort_direction: sortDirection,
      }),
    refetchInterval: 20_000,
  })

  const extensionPresentation = useMemo(
    () => resolveExtensionPresentation(getString(runtimeQuery.data?.settings?.extension_module)),
    [runtimeQuery.data],
  )

  const filteredMarkets = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) {
      return marketsQuery.data?.items ?? []
    }
    return (marketsQuery.data?.items ?? []).filter((item) => {
      const haystacks = [
        item.market.market_slug,
        item.market.event_title,
        item.market.event_slug,
        item.market.condition_id,
      ]
      return haystacks.some((value) => value?.toLowerCase().includes(keyword))
    })
  }, [marketsQuery.data, search])

  const selectedTokenParam = searchParams.get('token_id')

  const selectMarket = useCallback((tokenId: string, replace = false) => {
    if (selectedTokenParam === tokenId) {
      return
    }
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('token_id', tokenId)
    setSearchParams(nextParams, { replace })
  }, [searchParams, selectedTokenParam, setSearchParams])

  const activeTokenId = useMemo(() => {
    if (selectedTokenParam) {
      return filteredMarkets.some((item) => marketHasToken(item, selectedTokenParam))
        ? selectedTokenParam
        : null
    }
    return filteredMarkets[0] ? getMarketTokenId(filteredMarkets[0]) : null
  }, [filteredMarkets, selectedTokenParam])

  const selectedMarket = filteredMarkets.find((item) => marketHasToken(item, activeTokenId)) ?? null
  const selectionMissing = Boolean(selectedTokenParam) && !selectedMarket

  const detailQuery = useQuery({
    queryKey: ['market-detail', activeTokenId],
    queryFn: () => adminApi.getMarketDetail({ token_id: activeTokenId }),
    enabled: Boolean(activeTokenId),
  })
  const orderbookQuery = useQuery({
    queryKey: ['market-orderbook', activeTokenId],
    queryFn: () => adminApi.getMarketOrderbook({ token_id: activeTokenId }),
    enabled: Boolean(activeTokenId),
  })
  const midpointQuery = useQuery({
    queryKey: ['market-midpoint', activeTokenId],
    queryFn: () => adminApi.getMarketMidpoint({ token_id: activeTokenId }),
    enabled: Boolean(activeTokenId),
  })
  const historyQuery = useQuery({
    queryKey: ['market-history', activeTokenId],
    queryFn: () =>
      adminApi.getMarketPricesHistory({
        token_id: activeTokenId,
        interval: '1d',
        fidelity: 60,
      }),
    enabled: Boolean(activeTokenId),
  })
  const selectedMarketView = detailQuery.data ?? selectedMarket
  const selectedNoFeePreview = selectedMarketView ? getOutcomeTokenView(selectedMarketView, 'NO')?.fee_preview : null
  const selectedYesFeePreview = selectedMarketView ? getOutcomeTokenView(selectedMarketView, 'YES')?.fee_preview : null

  const columns: Array<DataColumn<MarketView>> = [
    {
      key: 'market',
      header: '市场',
      cell: (row) => (
        <div className="market-table-cell">
          <EntityAvatar
            label={row.market.event_title ?? row.market.market_slug}
            imageUrl={row.market.icon_url}
            size="sm"
          />
          <div className="table-primary">
            <MarketExternalLink
              className="market-list__title"
              eventSlug={row.market.event_slug}
              onClick={(event) => {
                event.stopPropagation()
              }}
            >
              {row.market.event_title ?? row.market.market_slug}
            </MarketExternalLink>
            <MarketExternalLink
              className="link-subtle"
              eventSlug={row.market.event_slug}
            >
              {row.market.market_slug}
            </MarketExternalLink>
          </div>
        </div>
      ),
    },
    {
      key: 'end_date',
      header: '封盘时间',
      cell: (row) => formatFullDateTime(row.market.end_date),
    },
    {
      key: 'status',
      header: '交易状态',
      cell: (row) => (
        <div className="badge-row">
          <StatusPill
            label={formatTradingStatusLabel(row.market.trading_status)}
            tone={tradingStatusTone(row.market.trading_status)}
          />
          {row.tracked ? <StatusPill label="已跟踪" tone="success" /> : null}
        </div>
      ),
    },
    {
      key: 'extension',
      header: '扩展标记',
      cell: (row) => (
        <div className="badge-row">
          {extensionPresentation.renderMarketBadges?.(row).map((badge) => (
            <StatusPill key={badge.label} label={badge.label} tone={badge.tone} />
          )) ?? '—'}
        </div>
      ),
    },
    {
      key: 'spread',
      header: '价差',
      align: 'right',
      cell: (row) => formatDecimal(getPrimaryTokenView(row)?.spread),
    },
    {
      key: 'quotes',
      header: 'YES / NO 买卖价 / 手续费',
      align: 'right',
      cell: (row) => renderBinaryQuoteSummary(row),
    },
    {
      key: 'position',
      header: '持仓份额',
      align: 'right',
      cell: (row) => formatDecimal(getMarketPosition(row)?.shares),
    },
    {
      key: 'orders',
      header: '挂单数',
      align: 'right',
      cell: (row) => getMarketOpenOrderCount(row),
    },
  ]

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">市场</p>
          <h1>市场监控与扩展解释</h1>
          <p>先从列表定位目标市场，再查看盘口、走势和扩展解释。</p>
        </div>
      </header>

      <SectionCard title="筛选条件" subtitle="状态和费率走服务端过滤，关键词检索在当前页内完成。">
        <div className="form-grid form-grid--market-filters">
          <label className="form-grid__wide">
            <span>关键词</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="输入市场标识、事件标题或条件 ID"
            />
          </label>
          <label>
            <span>交易状态</span>
            <select value={tradingStatus} onChange={(event) => setTradingStatus(event.target.value)}>
              {tradingStatusOptions.map((option) => (
                <option key={option.value || 'all'} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>费率开关</span>
            <select value={feesEnabled} onChange={(event) => setFeesEnabled(event.target.value)}>
              {feeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>排序字段</span>
            <select value={sortBy} onChange={(event) => setSortBy(event.target.value)}>
              {sortOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>排序方向</span>
            <select value={sortDirection} onChange={(event) => setSortDirection(event.target.value as 'asc' | 'desc')}>
              <option value="desc">降序</option>
              <option value="asc">升序</option>
            </select>
          </label>
          <label>
            <span>暂停/恢复操作者</span>
            <input value={marketOperator} onChange={(event) => setMarketOperator(event.target.value)} />
          </label>
          <label>
            <span>暂停原因</span>
            <input value={marketPauseReason} onChange={(event) => setMarketPauseReason(event.target.value)} />
          </label>
        </div>
        {pauseMarketError ? (
          <ul className="message-list form-feedback">
            <li>
              <strong>暂停失败</strong>
              <span>{pauseMarketError}</span>
            </li>
          </ul>
        ) : null}
        {resumeMarketError ? (
          <ul className="message-list form-feedback">
            <li>
              <strong>恢复失败</strong>
              <span>{resumeMarketError}</span>
            </li>
          </ul>
        ) : null}
      </SectionCard>

      <SectionCard
        title="市场列表"
        subtitle={`当前页显示 ${filteredMarkets.length} 条，服务端总数 ${marketsQuery.data?.total ?? 0} 条。`}
        actions={
          <div className="inline-actions">
            <button type="button" onClick={() => setOffset((current) => Math.max(0, current - 50))} disabled={offset === 0}>
              上一页
            </button>
            <button
              type="button"
              onClick={() => setOffset((current) => current + 50)}
              disabled={(marketsQuery.data?.items.length ?? 0) < 50}
            >
              下一页
            </button>
          </div>
        }
      >
        {marketsQuery.error ? (
          <QueryErrorNotice title="市场列表加载失败" error={marketsQuery.error} />
        ) : (
          <DataTable
            columns={columns}
            rows={filteredMarkets}
            rowKey={(row) => getMarketTokenId(row)}
            emptyTitle="没有可展示的市场"
            emptyDescription="请调整筛选条件后重试。"
            onRowClick={(row) => selectMarket(getMarketTokenId(row))}
            selectedRowKey={activeTokenId}
          />
        )}
      </SectionCard>

      <SectionCard
        title="当前选中市场"
        subtitle={
          selectedMarket
            ? '点击上方列表即可切换当前查看对象。'
            : selectionMissing
              ? '当前筛选结果已不包含你之前选中的市场。'
              : '先从上方列表选择一个市场。'
        }
        actions={
          selectedMarket ? (
            <div className="inline-actions">
              <div className="badge-row">
                <StatusPill
                  label={formatTradingStatusLabel(selectedMarket.market.trading_status)}
                  tone={tradingStatusTone(selectedMarket.market.trading_status)}
                />
                {selectedMarket.tracked ? <StatusPill label="已跟踪" tone="success" /> : null}
                {extensionPresentation.renderMarketBadges?.(selectedMarket).map((badge) => (
                  <StatusPill key={badge.label} label={badge.label} tone={badge.tone} />
                ))}
              </div>
              <button
                type="button"
                disabled={pauseMarketMutation.isPending}
                onClick={() => {
                  const conditionId = selectedMarket.market.condition_id
                  const operator = marketOperator.trim() || 'manual'
                  const reason = marketPauseReason.trim() || 'manual_pause'
                  if (!window.confirm(`将以"${reason}"暂停 ${selectedMarket.market.market_slug ?? conditionId}，是否继续？`)) return
                  pauseMarketMutation.mutate({ condition_id: conditionId, reason, operator })
                }}
              >
                {pauseMarketMutation.isPending ? '暂停中...' : '暂停该市场'}
              </button>
              <button
                type="button"
                disabled={resumeMarketMutation.isPending}
                onClick={() => {
                  const conditionId = selectedMarket.market.condition_id
                  const operator = marketOperator.trim() || 'manual'
                  if (!window.confirm(`确认恢复 ${selectedMarket.market.market_slug ?? conditionId}？`)) return
                  resumeMarketMutation.mutate({ condition_id: conditionId, operator })
                }}
              >
                {resumeMarketMutation.isPending ? '恢复中...' : '恢复该市场'}
              </button>
            </div>
          ) : null
        }
      >
        {selectedMarket ? (
          <>
            <div className="selected-market-summary">
              <div className="market-list__main">
                <EntityAvatar
                  label={detailQuery.data?.market.event_title ?? selectedMarket.market.event_title ?? selectedMarket.market.market_slug}
                  imageUrl={detailQuery.data?.market.icon_url ?? selectedMarket.market.icon_url}
                  size="md"
                />
                <div className="market-list__text">
                  <MarketExternalLink
                    className="market-list__title"
                    eventSlug={detailQuery.data?.market.event_slug ?? selectedMarket.market.event_slug}
                  >
                    {detailQuery.data?.market.event_title ?? selectedMarket.market.event_title ?? '—'}
                  </MarketExternalLink>
                  <MarketExternalLink
                    className="link-subtle"
                    eventSlug={detailQuery.data?.market.event_slug ?? selectedMarket.market.event_slug}
                  >
                    {selectedMarket.market.market_slug}
                  </MarketExternalLink>
                </div>
              </div>
            </div>
            <div className="detail-list">
              <div>
                <dt>事件标题</dt>
                <dd>
                  <MarketExternalLink
                    className="market-list__title"
                    eventSlug={detailQuery.data?.market.event_slug ?? selectedMarket.market.event_slug}
                  >
                    {detailQuery.data?.market.event_title ?? selectedMarket.market.event_title ?? '—'}
                  </MarketExternalLink>
                </dd>
              </div>
              <div>
                <dt>市场标识</dt>
                <dd>
                  <MarketExternalLink
                    className="market-list__title"
                    eventSlug={detailQuery.data?.market.event_slug ?? selectedMarket.market.event_slug}
                  >
                    {selectedMarket.market.market_slug}
                  </MarketExternalLink>
                </dd>
              </div>
              <div>
                <dt>封盘时间</dt>
                <dd>{formatFullDateTime(detailQuery.data?.market.end_date ?? selectedMarket.market.end_date)}</dd>
              </div>
              <div>
                <dt>匹配关键词</dt>
                <dd>{formatList(selectedMarket.market.matched_keywords)}</dd>
              </div>
              <div>
                <dt>标签</dt>
                <dd>{formatList(selectedMarket.market.tags)}</dd>
              </div>
            </div>
          </>
        ) : (
          <p className="muted">
            {selectionMissing ? '当前筛选结果不包含你之前选中的市场，请从列表重新选择。' : '先从上方列表选择一个市场。'}
          </p>
        )}
      </SectionCard>

      <div className="content-grid content-grid--two">
        <SectionCard title="交易概览" subtitle="当前市场的费用、持仓和挂单概况。">
          {selectedMarket ? (
            <div className="detail-list">
              <div>
                <dt>费率(BPS)</dt>
                <dd>{formatDecimal(selectedMarket.market.fees.fee_rate_bps)}</dd>
              </div>
              <div>
                <dt>YES 每1美元成交额手续费(买)</dt>
                <dd>{formatFeePreviewDetail(selectedYesFeePreview, 'buy')}</dd>
              </div>
              <div>
                <dt>YES 每1美元成交额手续费(卖)</dt>
                <dd>{formatFeePreviewDetail(selectedYesFeePreview, 'sell')}</dd>
              </div>
              <div>
                <dt>NO 每1美元成交额手续费(买)</dt>
                <dd>{formatFeePreviewDetail(selectedNoFeePreview, 'buy')}</dd>
              </div>
              <div>
                <dt>NO 每1美元成交额手续费(卖)</dt>
                <dd>{formatFeePreviewDetail(selectedNoFeePreview, 'sell')}</dd>
              </div>
              <div>
                <dt>费率更新时间</dt>
                <dd>{formatDateTime(selectedMarket.market.fees.fee_rate_updated_at)}</dd>
              </div>
              <div>
                <dt>持仓份额</dt>
                <dd>{formatDecimal(getMarketPosition(selectedMarket)?.shares)}</dd>
              </div>
              <div>
                <dt>未完成订单数</dt>
                <dd>{getMarketOpenOrderCount(selectedMarket)}</dd>
              </div>
              <div>
                <dt>最小下单量</dt>
                <dd>{formatDecimal(selectedMarket.market.min_order_size)}</dd>
              </div>
              <div>
                <dt>最小变动价位</dt>
                <dd>{formatDecimal(selectedMarket.market.tick_size)}</dd>
              </div>
              <div>
                <dt>封盘时间</dt>
                <dd>{formatFullDateTime(selectedMarket.market.end_date)}</dd>
              </div>
            </div>
          ) : (
            <p className="muted">尚未选中市场，无法展示交易概览。</p>
          )}
        </SectionCard>

        <SectionCard title="盘口快照" subtitle="优先看中间价、最优买卖价和最新盘口。">
          {selectedMarket ? (
            <>
              <div className="detail-list">
                <div>
                  <dt>中间价</dt>
                  <dd>{formatDecimal(midpointQuery.data?.midpoint)}</dd>
                </div>
                <div>
                  <dt>最优买价 / 卖价</dt>
                  <dd>
                    {formatDecimal(midpointQuery.data?.best_bid)} / {formatDecimal(midpointQuery.data?.best_ask)}
                  </dd>
                </div>
                <div>
                  <dt>买卖价差</dt>
                  <dd>{formatDecimal(midpointQuery.data?.spread)}</dd>
                </div>
                <div>
                  <dt>快照时间</dt>
                  <dd>{formatDateTime(midpointQuery.data?.received_at)}</dd>
                </div>
              </div>
              {orderbookQuery.data?.orderbook ? (
                <div className="depth-grid">
                  <div>
                    <h3>买盘</h3>
                    <ul className="depth-list">
                      {orderbookQuery.data.orderbook.bids.slice(0, 5).map((level, index) => (
                        <li key={`bid-${index}`}>
                          <span>{formatDecimal(level.price)}</span>
                          <span>{formatDecimal(level.size)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <h3>卖盘</h3>
                    <ul className="depth-list">
                      {orderbookQuery.data.orderbook.asks.slice(0, 5).map((level, index) => (
                        <li key={`ask-${index}`}>
                          <span>{formatDecimal(level.price)}</span>
                          <span>{formatDecimal(level.size)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              ) : null}
            </>
          ) : (
            <p className="muted">尚未选中市场，无法展示盘口快照。</p>
          )}
        </SectionCard>
      </div>

      <SectionCard title="价格走势" subtitle="默认展示最近 1 天的价格变化。">
        {selectedMarket ? (
          historyQuery.data && historyQuery.data.history.length > 0 ? (
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={historyQuery.data.history}>
                  <XAxis
                    dataKey="timestamp"
                    tickFormatter={(value: string) => formatDateTime(value)}
                    minTickGap={24}
                  />
                  <YAxis />
                  <Tooltip
                    formatter={(value) =>
                      formatDecimal(Array.isArray(value) ? value[0] : value ?? undefined)
                    }
                    labelFormatter={(value) =>
                      typeof value === 'string' ? formatDateTime(value) : String(value ?? '—')
                    }
                  />
                  <Line type="monotone" dataKey="price" stroke="#0f766e" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="muted">当前没有可展示的价格历史。</p>
          )
        ) : (
          <p className="muted">尚未选中市场，无法展示价格走势。</p>
        )}
      </SectionCard>

      {selectedMarket ? extensionPresentation.renderMarketDetail?.(selectedMarket) : null}

      <SectionCard title="原始市场快照" subtitle="排障时再展开查看。">
        <JsonPanel
          value={selectedMarket ? detailQuery.data ?? selectedMarket : null}
          emptyLabel="尚未选中市场。"
          detailsLabel="查看市场原始数据"
        />
      </SectionCard>
    </div>
  )
}
