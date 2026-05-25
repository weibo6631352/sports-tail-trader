import { Outlet } from 'react-router-dom'
import { Alert } from '@mantine/core'
import { useRuntimeIdentity } from '@core/identity/useRuntimeIdentity'
import { resolveStrategyBundle } from './registry'

// 策略区 shell：在 Outlet 上方告知当前策略身份；下层路由由 bundle.routes 接管。
export function StrategyShell() {
  const { strategyId } = useRuntimeIdentity()
  const bundle = resolveStrategyBundle(strategyId)
  return (
    <>
      {bundle.id === 'fallback' ? (
        <Alert color="yellow" variant="light" mb="sm">
          当前 strategy_id ({strategyId ?? '未知'}) 未在前端 registry 注册，仅提供占位视图。
        </Alert>
      ) : null}
      <Outlet />
    </>
  )
}
