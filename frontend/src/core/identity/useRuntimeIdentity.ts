import { useQuery } from '@tanstack/react-query'
import { qk } from '@core/api/keys'
import { healthApi } from '@core/api/resources'
import type { RuntimeSnapshot, RuntimeIdentity } from '@core/api/types'

// /runtime 是身份 + 配置 + 连接状态合体。SSE 'trading_paused' / 'trading_resumed'
// 会 invalidate runtime root，让顶栏立刻反映。

export function useRuntimeIdentity() {
  const query = useQuery<RuntimeSnapshot>({
    queryKey: qk.runtime(),
    queryFn: ({ signal }) => healthApi.runtime(signal),
  })
  const identity: RuntimeIdentity | undefined = query.data?.identity
  return {
    query,
    identity,
    settings: query.data?.settings,
    automaticTradingEnabled: Boolean(query.data?.readiness?.automatic_trading_enabled ?? query.data?.automatic_trading_enabled),
    strategyId: query.data?.settings?.strategy_id ?? null,
    phase: query.data?.readiness?.phase ?? query.data?.phase ?? null,
  }
}
