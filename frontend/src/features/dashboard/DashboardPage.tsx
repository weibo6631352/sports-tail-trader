import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { adminApi } from '../../core/api/resources'
import { SectionCard } from '../../shared/ui/SectionCard'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { StatusPill } from '../../shared/ui/StatusPill'
import { EntityAvatar } from '../../shared/ui/EntityAvatar'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import {
  formatCompact,
  formatDateTime,
  formatFullDateTime,
  formatDecimal,
  formatAllowance,
  getString,
} from '../../shared/utils/format'
import {
  formatIssueFieldLabel,
  formatPhaseLabel,
  formatTradingStatusLabel,
  formatWorkerStateLabel,
} from '../../shared/utils/labels'
import { hasPositiveShares } from '../../shared/utils/markets'
import { getMarketPosition, getMarketTokenId } from '../../shared/utils/marketViews'
import { resolveExtensionPresentation } from '../../extensions/registry'

const boolTone = (value: boolean): 'success' | 'danger' => (value ? 'success' : 'danger')

const getWorkerTone = (
  state: string | undefined,
  healthy: boolean | undefined,
): 'neutral' | 'success' | 'warning' | 'danger' => {
  if (healthy === false) {
    return 'danger'
  }
  if (state === 'running') {
    return 'success'
  }
  if (state === 'paused') {
    return 'warning'
  }
  return 'neutral'
}

const marketStatusTone = (status: string): 'neutral' | 'success' | 'warning' | 'danger' => {
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

const sortByEndDate = <T extends { market: { end_date: string | null } }>(items: T[]): T[] => {
  return [...items].sort((left, right) => {
    const leftTime = left.market.end_date ? Date.parse(left.market.end_date) : Number.POSITIVE_INFINITY
    const rightTime = right.market.end_date ? Date.parse(right.market.end_date) : Number.POSITIVE_INFINITY
    return leftTime - rightTime
  })
}

export const DashboardPage = () => {
  const readyQuery = useQuery({
    queryKey: ['ready', 'dashboard'],
    queryFn: adminApi.getReady,
    refetchInterval: 10_000,
  })
  const runtimeQuery = useQuery({
    queryKey: ['runtime', 'dashboard'],
    queryFn: adminApi.getRuntime,
    refetchInterval: 15_000,
  })
  const workersQuery = useQuery({
    queryKey: ['workers'],
    queryFn: adminApi.getWorkers,
    refetchInterval: 15_000,
  })
  const metricsQuery = useQuery({
    queryKey: ['metrics'],
    queryFn: adminApi.getMetrics,
    refetchInterval: 15_000,
  })
  const portfolioQuery = useQuery({
    queryKey: ['portfolio'],
    queryFn: adminApi.getPortfolio,
    refetchInterval: 10_000,
  })
  const marketsQuery = useQuery({
    queryKey: ['markets', 'dashboard'],
    queryFn: () => adminApi.listMarkets({ limit: 100, offset: 0 }),
    refetchInterval: 20_000,
  })

  const extensionModule = getString(runtimeQuery.data?.settings?.extension_module)
  const extensionPresentation = useMemo(() => resolveExtensionPresentation(extensionModule), [extensionModule])

  const blockingIssues = readyQuery.data?.blocking_issues ?? []
  const warnings = readyQuery.data?.warnings ?? []
  const recentAllocations = portfolioQuery.data?.recent_allocations ?? []
  const workers = workersQuery.data?.workers ?? []
  const marketItems = useMemo(() => marketsQuery.data?.items ?? [], [marketsQuery.data?.items])
  const runningWorkerCount = workers.filter((worker) => (worker.state ?? worker.status) === 'running').length
  const trackedMarkets = useMemo(
    () => sortByEndDate(marketItems.filter((item) => item.tracked)),
    [marketItems],
  )
  const positionMarkets = useMemo(
    () => sortByEndDate(marketItems.filter((item) => hasPositiveShares(getMarketPosition(item)?.shares))),
    [marketItems],
  )
  const trackedPreview = trackedMarkets.slice(0, 6)
  const positionPreview = positionMarkets.slice(0, 6)
  const trackedMarketCount = runtimeQuery.data?.registry.market_count ?? 0
  const lastCompletedFullScanMarkets = runtimeQuery.data?.market_discovery.last_completed_round_markets ?? 0
  const currentRoundScannedMarkets = runtimeQuery.data?.market_discovery.markets_seen_in_round ?? 0
  const fullScanMarketCount = lastCompletedFullScanMarkets || currentRoundScannedMarkets
  const fullScanCompletedAt = runtimeQuery.data?.market_discovery.last_round_completed_at ?? null

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">总览</p>
          <h1>系统运行总览</h1>
          <p>先看账户、市场覆盖和阻塞项，再进入线程、扩展和原始快照。</p>
        </div>
      </header>

      <section className="stats-grid">
        <div className="stat-card">
          <span>自动交易</span>
          <strong>{readyQuery.data?.ready_to_trade ? '已就绪' : '受阻'}</strong>
          <small>当前阶段 {formatPhaseLabel(readyQuery.data?.phase ?? runtimeQuery.data?.phase)}</small>
        </div>
        <div className="stat-card">
          <span>阻塞项</span>
          <strong>{blockingIssues.length}</strong>
          <small>告警 {warnings.length}</small>
        </div>
        <div className="stat-card">
          <span>组合余额</span>
          <strong>{formatCompact(portfolioQuery.data?.balance_usdc)}</strong>
          <small>授权额度 {formatAllowance(portfolioQuery.data?.allowance_usdc)}</small>
        </div>
        <div className="stat-card">
          <span>全量扫描面</span>
          <strong>{fullScanMarketCount}</strong>
          <small>扩展跟踪 {trackedMarketCount} · 运行中线程 {runningWorkerCount}</small>
        </div>
      </section>

      <div className="content-grid content-grid--two">
        <SectionCard title="关键状态" subtitle="这里先放会直接影响自动交易的状态。">
          <div className="detail-list">
            <div>
              <dt>自动交易</dt>
              <dd>
                <StatusPill
                  label={readyQuery.data?.ready_to_trade ? '允许' : '阻塞'}
                  tone={readyQuery.data?.ready_to_trade ? 'success' : 'danger'}
                />
              </dd>
            </div>
            <div>
              <dt>运行阶段</dt>
              <dd>{formatPhaseLabel(readyQuery.data?.phase ?? runtimeQuery.data?.phase)}</dd>
            </div>
            <div>
              <dt>用户行情连接</dt>
              <dd>
                <StatusPill
                  label={readyQuery.data?.runtime.user_ws_connected ? '已连接' : '未连接'}
                  tone={boolTone(readyQuery.data?.runtime.user_ws_connected ?? false)}
                />
              </dd>
            </div>
            <div>
              <dt>允许新买入</dt>
              <dd>
                <StatusPill
                  label={readyQuery.data?.runtime.allow_new_entries ? '打开' : '关闭'}
                  tone={boolTone(readyQuery.data?.runtime.allow_new_entries ?? false)}
                />
              </dd>
            </div>
            <div>
              <dt>最近一次对账</dt>
              <dd>{formatDateTime(readyQuery.data?.runtime.last_reconcile_at)}</dd>
            </div>
            <div>
              <dt>持仓 / 未完成订单</dt>
              <dd>
                {portfolioQuery.data?.position_count ?? 0} / {portfolioQuery.data?.open_order_count ?? 0}
              </dd>
            </div>
            <div>
              <dt>上轮全量扫描</dt>
              <dd>
                {fullScanMarketCount} 个活跃市场
                {fullScanCompletedAt ? ` · 完成于 ${formatDateTime(fullScanCompletedAt)}` : ''}
              </dd>
            </div>
          </div>
        </SectionCard>

        <SectionCard title="线程与资金概览" subtitle="确认线程在跑，再看余额、授权和成交数。">
          <div className="detail-list">
            <div>
              <dt>运行中线程</dt>
              <dd>{runningWorkerCount}</dd>
            </div>
            <div>
              <dt>线程总数</dt>
              <dd>{workers.length}</dd>
            </div>
            <div>
              <dt>授权额度</dt>
              <dd>{formatAllowance(portfolioQuery.data?.allowance_usdc)}</dd>
            </div>
            <div>
              <dt>可用余额</dt>
              <dd>{formatDecimal(portfolioQuery.data?.available_usdc)}</dd>
            </div>
            <div>
              <dt>成交数</dt>
              <dd>{portfolioQuery.data?.fill_count ?? 0}</dd>
            </div>
          </div>
        </SectionCard>
      </div>

      <SectionCard title="当前阻塞与告警" subtitle="优先处理阻塞项，告警作为次级风险提示。">
        {blockingIssues.length > 0 ? (
          <ul className="message-list">
            {blockingIssues.map((issue, index) => (
              <li key={`${issue.field}-${issue.code}-${index}`}>
                <strong>{formatIssueFieldLabel(issue.field)}</strong>
                <span>{issue.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">当前没有阻塞项。</p>
        )}
        {warnings.length > 0 ? (
          <ul className="message-list is-warning">
            {warnings.map((issue, index) => (
              <li key={`${issue.field}-${issue.code}-${index}`}>
                <strong>{formatIssueFieldLabel(issue.field)}</strong>
                <span>{issue.message}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </SectionCard>

      <div className="content-grid content-grid--two">
        <SectionCard title="扩展跟踪市场" subtitle="这里只展示当前扩展实际纳入并持续跟踪的市场。">
          {trackedPreview.length > 0 ? (
            <>
              <ul className="market-list">
                {trackedPreview.map((item) => {
                  const title = item.market.event_title ?? item.market.market_slug
                  return (
                    <li key={getMarketTokenId(item)} className="market-list__item">
                      <div className="market-list__main">
                        <EntityAvatar label={title} imageUrl={item.market.icon_url} size="sm" />
                        <div className="market-list__text">
                          <MarketExternalLink
                            className="market-list__title"
                            eventSlug={item.market.event_slug}
                          >
                            {title}
                          </MarketExternalLink>
                          <MarketExternalLink
                            className="link-subtle"
                            eventSlug={item.market.event_slug}
                          >
                            {item.market.market_slug}
                          </MarketExternalLink>
                        </div>
                      </div>
                      <div className="market-list__meta">
                        <StatusPill
                          label={formatTradingStatusLabel(item.market.trading_status)}
                          tone={marketStatusTone(item.market.trading_status)}
                        />
                        <span>封盘 {formatFullDateTime(item.market.end_date)}</span>
                      </div>
                    </li>
                  )
                })}
              </ul>
              {trackedMarkets.length > trackedPreview.length ? (
                <p className="muted">还有 {trackedMarkets.length - trackedPreview.length} 个扩展跟踪市场未展开。</p>
              ) : null}
            </>
          ) : (
            <p className="muted">
              {fullScanMarketCount > 0
                ? `上轮全量扫描 ${fullScanMarketCount} 个活跃市场，当前扩展还没有纳入市场。`
                : '当前还没有完成一轮全量扫描。'}
            </p>
          )}
        </SectionCard>

        <SectionCard title="持仓市场" subtitle="通用持仓清单，便于直接打开官网市场页复盘。">
          {positionPreview.length > 0 ? (
            <>
              <ul className="market-list">
                {positionPreview.map((item) => {
                  const title = item.market.event_title ?? item.market.market_slug
                  return (
                    <li key={getMarketTokenId(item)} className="market-list__item">
                      <div className="market-list__main">
                        <EntityAvatar label={title} imageUrl={item.market.icon_url} size="sm" />
                        <div className="market-list__text">
                          <MarketExternalLink
                            className="market-list__title"
                            eventSlug={item.market.event_slug}
                          >
                            {title}
                          </MarketExternalLink>
                          <MarketExternalLink
                            className="link-subtle"
                            eventSlug={item.market.event_slug}
                          >
                            {item.market.market_slug}
                          </MarketExternalLink>
                        </div>
                      </div>
                      <div className="market-list__meta">
                        <span>持仓 {formatDecimal(getMarketPosition(item)?.shares)}</span>
                        <span>封盘 {formatFullDateTime(item.market.end_date)}</span>
                      </div>
                    </li>
                  )
                })}
              </ul>
              {positionMarkets.length > positionPreview.length ? (
                <p className="muted">还有 {positionMarkets.length - positionPreview.length} 个持仓市场未展开。</p>
              ) : null}
            </>
          ) : (
            <p className="muted">当前没有持仓市场。</p>
          )}
        </SectionCard>
      </div>

      <SectionCard title="工作线程状态" subtitle="这里看线程是否在跑，以及是否处于异常或暂停。">
        {workers.length > 0 ? (
          <div className="inline-badge-list">
            {workers.map((worker, index) => {
              const workerName = worker.name ?? worker.worker_name ?? `worker-${index + 1}`
              const workerState = worker.state ?? worker.status ?? 'unknown'

              return (
                <StatusPill
                  key={`${workerName}-${index}`}
                  label={`${workerName} · ${formatWorkerStateLabel(workerState)}`}
                  tone={getWorkerTone(workerState, worker.healthy)}
                />
              )
            })}
          </div>
        ) : (
          <p className="muted">当前没有线程快照。</p>
        )}
      </SectionCard>

      {extensionPresentation.renderDashboard?.({
        ready: readyQuery.data,
        runtime: runtimeQuery.data,
        portfolio: portfolioQuery.data,
        markets: marketItems,
      })}

      <div className="content-grid content-grid--two">
        <SectionCard title="最近分配" subtitle="最近的预算分配和敞口变化。">
          {recentAllocations.length > 0 ? (
            <div className="detail-list">
              {recentAllocations.slice(0, 6).map((allocation) => (
                <div key={allocation.idempotency_key ?? allocation.condition_id}>
                  <dt>
                    {allocation.market_slug ? (
                      <MarketExternalLink className="market-list__title" eventSlug={allocation.event_slug}>
                        {allocation.market_slug}
                      </MarketExternalLink>
                    ) : (
                      allocation.condition_id
                    )}
                  </dt>
                  <dd>
                    目标预算 {formatDecimal(allocation.target_budget_usdc)} / 当前敞口{' '}
                    {formatDecimal(allocation.current_exposure_usdc)}
                  </dd>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">当前没有最近分配记录。</p>
          )}
        </SectionCard>

        <SectionCard title="系统快照" subtitle="原始快照默认折叠，排障时再展开。">
          <div className="detail-stack">
            <JsonPanel
              value={metricsQuery.data?.metrics ?? metricsQuery.data?.queue_depths}
              emptyLabel="暂无指标快照。"
              detailsLabel="查看指标原始数据"
            />
            <JsonPanel
              value={runtimeQuery.data?.settings}
              emptyLabel="暂无配置快照。"
              detailsLabel="查看配置原始数据"
            />
          </div>
        </SectionCard>
      </div>
    </div>
  )
}
