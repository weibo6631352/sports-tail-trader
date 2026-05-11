import type { StrategyBundle } from './api'
import { currentStrategyBundle } from './current'

// extension_module → 前端 bundle 映射。Fallback bundle 在未知 module 时使用，
// 仍能让 /strategy/* 路由不 404，便于运维诊断。

const REGISTRY: Record<string, StrategyBundle> = {
  'strategies.current': currentStrategyBundle,
}

const FALLBACK: StrategyBundle = {
  id: 'fallback',
  displayName: '未知策略',
}

export function resolveStrategyBundle(extensionModule: string | null | undefined): StrategyBundle {
  if (!extensionModule) return FALLBACK
  return REGISTRY[extensionModule] ?? FALLBACK
}

export function listRegisteredStrategies(): StrategyBundle[] {
  return Object.values(REGISTRY)
}
