# 前端管理台

Sports Tail Trader 的前端管理台。面向金融分析师 / 数学家做盯盘、复盘、分析、手工干预、策略参数热调。

## 设计原则

- **通用 / 专用边界硬隔离**：框架壳（盯盘、复盘、分析、市场、订单持仓、系统）只依赖后端 admin API，不感知任何具体策略；当前策略 `strategies.current` 通过 `src/strategy/` 插件槽注入。
- **SSE-first**：实时数据走 `/stream/events`，事件驱动 React Query invalidate；聚合分析页手动触发；不留任何自动轮询。
- **审计为先**：所有写操作走 `OperatorReasonFields` + 二次确认 + 后端审计。
- **金额 / 价格用 Decimal**：API 返回字符串，前端用 `decimal.js` 处理与展示。
- **trace_id 一等公民**：列表行通用 `CopyableId` 展示；顶栏全局按 trace_id 跳转。

## 目录边界

| 目录 | 职责 |
| --- | --- |
| `src/app/` | 应用壳、Provider、路由、AppShell |
| `src/core/api/` | apiClient、DTO、resource 函数 |
| `src/core/sse/` | SSE 连接管理与事件 → query key 路由 |
| `src/core/time/` `core/filters/` `core/identity/` | 全局状态（Zustand） |
| `src/shared/` | 通用 UI 原语、表格、图表、抽屉、表单 |
| `src/features/` | 通用页面，与策略无关 |
| `src/strategy/` | 策略插件槽：协议 + registry + `current/` bundle |
| `src/styles/` | Mantine theme、CSS 变量 |

约束：
- 通用页面不能直接 import 任何 `src/strategy/current/*`。
- 策略 bundle 不能持有 `apiClient`，必须通过 hooks 暴露的资源访问。
- 前端不连 Polymarket，只调后端 admin API。
- 人工确认类操作只调后端受控 API，前端不复写策略确认条件。

## 开发命令

```bash
npm install
npm run dev          # 启动 vite dev server @ :5173
npm run build        # tsc -b && vite build → dist/
npm run typecheck
npm run lint
npm run preview
```

`VITE_PROXY_TARGET` 默认 `http://127.0.0.1:8000`，可在 `.env.local` 改写。
