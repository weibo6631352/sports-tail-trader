import { Outlet } from 'react-router-dom'

// 策略区 shell：仅承载 Outlet，路由由 tradingWorkflowBundle.routes 接管。
// 当前系统只有一个工作流，无需身份提示或 fallback。
export function StrategyShell() {
  return <Outlet />
}
