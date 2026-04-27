import type { ReactNode } from 'react'
import type {
  MarketView,
  PortfolioSnapshot,
  ReadyPayload,
  RuntimePayload,
} from '../core/api/types'
import type { Tone } from '../shared/ui/StatusPill'
import { currentExtensionPresentation } from './current/extension'

export interface ExtensionBadge {
  label: string
  tone?: Tone
}

export interface ExtensionDashboardContext {
  ready?: ReadyPayload
  runtime?: RuntimePayload
  portfolio?: PortfolioSnapshot
  markets: MarketView[]
}

export interface ExtensionPresentation {
  id: string
  displayName: string
  description: string
  renderDashboard?: (context: ExtensionDashboardContext) => ReactNode
  renderMarketBadges?: (market: MarketView) => ExtensionBadge[]
  renderMarketDetail?: (market: MarketView) => ReactNode
}

const fallbackExtensionPresentation: ExtensionPresentation = {
  id: 'unknown',
  displayName: '未知扩展',
  description: '当前扩展没有单独的前端扩展实现，页面只显示通用信息。',
}

const registry: Record<string, ExtensionPresentation> = {
  'strategies.current': currentExtensionPresentation,
}

export const resolveExtensionPresentation = (extensionModule: string | null | undefined): ExtensionPresentation => {
  if (!extensionModule) {
    return fallbackExtensionPresentation
  }
  return registry[extensionModule] ?? fallbackExtensionPresentation
}
