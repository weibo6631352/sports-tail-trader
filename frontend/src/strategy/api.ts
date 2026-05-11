import type { ColumnDef } from '@tanstack/react-table'
import type { ReactNode } from 'react'
import type { RouteObject } from 'react-router-dom'
import type { Candidate, MarketView, TradeTimeline } from '@core/api/types'

// StrategyBundle 协议：通用壳与具体策略唯一的耦合点。
// 策略 bundle 只通过这一组钩子向通用壳暴露内容：
//   - routes: 注入 /strategy/* 下的页面
//   - dashboardWidgets: 盯盘总览的额外卡片
//   - marketRowBadges / marketDetailPanel: 在通用市场页给策略私有信息
//   - timelineAnnotations: 在 trade timeline 上叠加策略私有注解
//   - candidateColumns: 候选表的策略私有列扩展
//   - paramSchema: 策略参数热调表单的字段定义（task #11 详细实现）
//
// 策略 bundle 不能持有 apiClient；所有数据访问必须通过 @core/api 的 hooks。

export type DashboardWidget = {
  id: string
  title: string
  render: () => ReactNode
}

export type TimelineAnnotation = {
  id: string
  timestamp: string
  label: ReactNode
  tone?: 'info' | 'success' | 'warning' | 'danger'
}

export type ParamRisk = 'low' | 'medium' | 'high'
export type ParamType = 'string' | 'number' | 'decimal' | 'boolean' | 'list[string]'

export type ParamSchemaField = {
  name: string
  label: string
  group: string
  type: ParamType
  risk: ParamRisk
  mutable: boolean
  description?: string
  hint?: string
  validator?: {
    min?: number | string
    max?: number | string
    enum?: string[]
    pattern?: string
  }
}

export type ParamSchema = {
  schemaVersion: string
  fields: ParamSchemaField[]
}

export type StrategyBundle = {
  id: string
  displayName: string
  routes?: RouteObject[]
  dashboardWidgets?: DashboardWidget[]
  marketRowBadges?: (m: MarketView) => ReactNode[]
  marketDetailPanel?: (m: MarketView) => ReactNode
  timelineAnnotations?: (t: TradeTimeline) => TimelineAnnotation[]
  candidateColumns?: ColumnDef<Candidate, unknown>[]
  paramSchema?: ParamSchema
}
