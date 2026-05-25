import type { StrategyBundle } from './api'
import { currentStrategyBundle } from './current'

// 当前系统只有一个量化交易工作流，前端不再需要按 ID 选择 bundle。
// 直接导出唯一 bundle；如果未来出现第二个 workflow，再恢复 routing 机制。

export const tradingWorkflowBundle: StrategyBundle = currentStrategyBundle
