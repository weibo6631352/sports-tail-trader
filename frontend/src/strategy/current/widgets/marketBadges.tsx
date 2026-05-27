import type { ReactNode } from 'react'
import { Badge } from '@mantine/core'
import type { MarketView } from '@core/api/types'

// sports_tail 策略在通用市场列表 / 详情上叠加的徽章：持仓 / 已暂停 / 高费率。
// 仅基于 MarketView 已有字段判断，零额外请求。

export function marketRowBadges(market: MarketView): ReactNode[] {
  const out: ReactNode[] = []

  // has_any_position 是 MarketView 顶层 boolean,后端 has_any_position
  // 字段已聚合 outcomes[].position 判断,前端不再自己遍历 tokens.
  if (market.has_any_position) {
    out.push(
      <Badge key="position" size="xs" color="teal" variant="light">
        持仓
      </Badge>,
    )
  }

  if (market.is_paused && market.pause?.reason) {
    out.push(
      <Badge key="paused" size="xs" color="yellow" variant="light" title={market.pause.reason}>
        已暂停
      </Badge>,
    )
  }

  const feeBps = market.fee_preview?.fee_rate_bps
  if (typeof feeBps === 'number' && feeBps >= 100) {
    out.push(
      <Badge key="high-fee" size="xs" color="red" variant="light" title={`fee_rate_bps=${feeBps}`}>
        高费率
      </Badge>,
    )
  }

  return out
}
