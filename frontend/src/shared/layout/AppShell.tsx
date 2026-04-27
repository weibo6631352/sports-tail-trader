import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { NavLink } from 'react-router-dom'
import { adminApi } from '../../core/api/resources'
import { resolveExtensionPresentation } from '../../extensions/registry'
import { EntityAvatar } from '../ui/EntityAvatar'
import { formatAddressShort, formatAllowance, formatCompact, getString } from '../utils/format'
import { resolvePolymarketIdentityDisplay } from '../utils/identity'
import { formatPhaseLabel } from '../utils/labels'
import { StatusPill } from '../ui/StatusPill'
import { QueryErrorNotice } from '../ui/QueryErrorNotice'
import { hasPositiveShares } from '../utils/markets'
import { getMarketPosition } from '../utils/marketViews'

interface AppShellProps {
  children: ReactNode
}

const navigation = [
  { to: '/', label: '总览' },
  { to: '/markets', label: '市场' },
  { to: '/candidates', label: '候选' },
  { to: '/orders', label: '订单' },
  { to: '/positions', label: '持仓' },
  { to: '/replay', label: '复盘' },
  { to: '/audit', label: '审计' },
  { to: '/operations', label: '操作' },
]

const statusTone = (ready: boolean, phase: string | undefined): 'success' | 'warning' | 'danger' | 'neutral' => {
  if (ready) {
    return 'success'
  }
  if (phase === 'degraded' || phase === 'recovering_snapshot') {
    return 'warning'
  }
  if (phase === 'paused' || phase === 'starting') {
    return 'danger'
  }
  return 'neutral'
}

export const AppShell = ({ children }: AppShellProps) => {
  const readyQuery = useQuery({
    queryKey: ['ready'],
    queryFn: adminApi.getReady,
    refetchInterval: 10_000,
  })
  const runtimeQuery = useQuery({
    queryKey: ['runtime', 'shell'],
    queryFn: adminApi.getRuntime,
    refetchInterval: 20_000,
  })
  const portfolioQuery = useQuery({
    queryKey: ['portfolio', 'shell'],
    queryFn: adminApi.getPortfolio,
    refetchInterval: 10_000,
  })
  const marketsQuery = useQuery({
    queryKey: ['markets', 'shell-summary'],
    queryFn: () => adminApi.listMarkets({ limit: 200, offset: 0 }),
    refetchInterval: 20_000,
  })

  const extensionModule = useMemo(
    () => getString(runtimeQuery.data?.settings?.extension_module) ?? '未配置',
    [runtimeQuery.data],
  )
  const extensionDisplayName = useMemo(
    () => resolveExtensionPresentation(extensionModule).displayName,
    [extensionModule],
  )

  const ready = readyQuery.data?.ready_to_trade ?? false
  const phase = readyQuery.data?.phase ?? runtimeQuery.data?.phase ?? 'starting'
  const identityDisplay = resolvePolymarketIdentityDisplay(runtimeQuery.data?.identity)
  const identityDetailParts = [
    identityDisplay.tradingAccountAddress ? `交易账户 ${formatAddressShort(identityDisplay.tradingAccountAddress)}` : null,
    identityDisplay.signingWalletAddress ? `签名钱包 ${formatAddressShort(identityDisplay.signingWalletAddress)}` : null,
  ].filter((item): item is string => Boolean(item))
  const trackedMarketCount = runtimeQuery.data?.registry.market_count ?? marketsQuery.data?.total ?? 0
  const fullScanMarketCount =
    runtimeQuery.data?.market_discovery.last_completed_round_markets ||
    runtimeQuery.data?.market_discovery.markets_seen_in_round ||
    0
  const positionMarketCount =
    marketsQuery.data?.items.filter((item) => hasPositiveShares(getMarketPosition(item)?.shares)).length ?? 0
  const shellError = readyQuery.error ?? runtimeQuery.error ?? portfolioQuery.error ?? marketsQuery.error

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="brand-block">
          <p className="eyebrow">交易控制台</p>
          <h1>运行台</h1>
          <div className="brand-block__meta">
            <StatusPill label={ready ? '允许自动交易' : '自动交易未就绪'} tone={statusTone(ready, phase)} />
            <StatusPill label={`当前阶段：${formatPhaseLabel(phase)}`} tone="neutral" />
          </div>
          <p className="brand-block__hint">当前扩展：{extensionDisplayName}</p>
        </div>

        <nav className="nav-list" aria-label="主导航">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <p>所有写操作都先经过应用服务。</p>
          <p>通用模块与扩展解释分层展示。</p>
        </div>
      </aside>

      <div className="app-content">
        <header className="top-status-bar">
          <div className="top-status-bar__meta">
            <StatusPill label={ready ? '自动交易已就绪' : '自动交易未就绪'} tone={statusTone(ready, phase)} />
            <span>当前阶段：{formatPhaseLabel(phase)}</span>
          </div>
          <div className="top-status-bar__summary">
            <div className="top-account">
              <EntityAvatar label={identityDisplay.title} imageUrl={identityDisplay.imageUrl} size="sm" />
              <div className="top-account__text">
                <strong>
                  {identityDisplay.title}
                  {identityDisplay.verified ? <span className="top-account__verified">已认证</span> : null}
                </strong>
                <span>{identityDetailParts.join(' · ')}</span>
              </div>
            </div>
            <div className="top-metric-list" aria-label="运行概览">
              <div className="top-metric">
                <span>扩展跟踪</span>
                <strong>{trackedMarketCount}</strong>
              </div>
              <div className="top-metric">
                <span>上轮全量扫描</span>
                <strong>{fullScanMarketCount}</strong>
              </div>
              <div className="top-metric">
                <span>持仓市场</span>
                <strong>{positionMarketCount}</strong>
              </div>
              <div className="top-metric">
                <span>组合余额</span>
                <strong>{formatCompact(portfolioQuery.data?.balance_usdc)}</strong>
              </div>
              <div className="top-metric">
                <span>授权额度</span>
                <strong>{formatAllowance(portfolioQuery.data?.allowance_usdc)}</strong>
              </div>
            </div>
          </div>
        </header>
        {shellError ? <QueryErrorNotice title="运行状态接口加载失败" error={shellError} /> : null}
        <main className="page-container">{children}</main>
      </div>
    </div>
  )
}
