import type { StrategyBundle } from './api'
import { currentStrategyBundle } from './current'

// strategy_id → 前端 bundle 映射。Fallback bundle 在未知 strategy 时使用，
// 仍能让 /strategy/* 路由不 404，便于运维诊断。

const REGISTRY: Record<string, StrategyBundle> = {
  sports_tail: currentStrategyBundle,
}

const FALLBACK: StrategyBundle = {
  id: 'fallback',
  displayName: '未知策略',
}

export function resolveStrategyBundle(strategyId: string | null | undefined): StrategyBundle {
  if (!strategyId) return FALLBACK
  return REGISTRY[strategyId] ?? FALLBACK
}

export function listRegisteredStrategies(): StrategyBundle[] {
  return Object.values(REGISTRY)
}
