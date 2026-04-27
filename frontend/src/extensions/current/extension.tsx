import { SectionCard } from '../../shared/ui/SectionCard'
import { StatusPill } from '../../shared/ui/StatusPill'
import { formatDecimal, formatList } from '../../shared/utils/format'
import { getMarketPosition, getPrimaryTokenView, hasOpenSellOrders } from '../../shared/utils/marketViews'
import type { MarketView } from '../../core/api/types'
import type { ExtensionPresentation } from '../registry'

const marketsWithPosition = (markets: MarketView[]) =>
  markets.filter((market) => Number(getMarketPosition(market)?.shares ?? 0) > 0)
const marketsWithSellOrders = (markets: MarketView[]) => markets.filter(hasOpenSellOrders)

export const currentExtensionPresentation: ExtensionPresentation = {
  id: 'strategies.current',
  displayName: '当前扩展',
  description: '围绕市场发现、恢复和跟踪语义提供解释性展示。',
  renderDashboard: ({ markets }) => {
    const tracked = markets.filter((market) => market.tracked).length
    const rejected = markets.filter((market) => market.market.reject_reason).length
    const positioned = marketsWithPosition(markets)
    const liveSellMarkets = marketsWithSellOrders(markets)

    return (
      <div className="content-grid content-grid--two">
        <SectionCard title="扩展态势" subtitle="只解释当前扩展如何看待这些市场。">
          <div className="stats-grid stats-grid--compact">
            <div className="stat-card">
              <span>扩展跟踪</span>
              <strong>{tracked}</strong>
            </div>
            <div className="stat-card">
              <span>筛除市场</span>
              <strong>{rejected}</strong>
            </div>
            <div className="stat-card">
              <span>持仓市场</span>
              <strong>{positioned.length}</strong>
            </div>
            <div className="stat-card">
              <span>挂卖单市场</span>
              <strong>{liveSellMarkets.length}</strong>
            </div>
          </div>
        </SectionCard>

        <SectionCard title="扩展说明" subtitle="扩展层负责解释，不直接操作交易客户端。">
          <div className="detail-list">
            <div>
              <dt>发现与筛选</dt>
              <dd>通用市场列表照常展示，当前扩展只额外标注匹配关键词、拒绝原因、持仓和卖单状态。</dd>
            </div>
            <div>
              <dt>入场语义</dt>
              <dd>入场价格、规模和方向由扩展 hook 结合逐 token 盘口视图决定，前端只展示框架返回的当前快照。</dd>
            </div>
            <div>
              <dt>恢复与跟踪</dt>
              <dd>持仓和未完成卖单会在扩展展示里单独高亮，避免它们淹没在通用市场表格里。</dd>
            </div>
          </div>
        </SectionCard>
      </div>
    )
  },
  renderMarketBadges: (market) => {
    const badges = []
    if (Number(getMarketPosition(market)?.shares ?? 0) > 0) {
      badges.push({ label: '有持仓', tone: 'warning' as const })
    }
    if (hasOpenSellOrders(market)) {
      badges.push({ label: '有卖单', tone: 'neutral' as const })
    }
    return badges
  },
  renderMarketDetail: (market) => {
    return (
      <SectionCard title="当前扩展视角" subtitle="扩展解释只依赖通用市场数据。">
        <div className="detail-list">
          <div>
            <dt>扩展标签</dt>
            <dd className="badge-row">
              {currentExtensionPresentation.renderMarketBadges?.(market).map((badge) => (
                <StatusPill key={badge.label} label={badge.label} tone={badge.tone} />
              ))}
            </dd>
          </div>
          <div>
            <dt>匹配关键词</dt>
            <dd>{formatList(market.market.matched_keywords)}</dd>
          </div>
          <div>
            <dt>分类</dt>
            <dd>{market.market.category ?? '—'}</dd>
          </div>
          <div>
            <dt>当前价差</dt>
            <dd>{formatDecimal(getPrimaryTokenView(market)?.spread)}</dd>
          </div>
          <div>
            <dt>拒绝原因</dt>
            <dd>{market.market.reject_reason ?? '—'}</dd>
          </div>
        </div>
      </SectionCard>
    )
  },
}
