import { type ReactNode, useEffect, useState } from 'react'
import { MantineProvider } from '@mantine/core'
import { Notifications } from '@mantine/notifications'
import { ModalsProvider } from '@mantine/modals'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { theme } from '@styles/theme'
import { attachQueryStats } from '@core/observability/queryStats'

type Props = { children: ReactNode }

export function Providers({ children }: Props) {
  // SSE-first：默认 staleTime=Infinity，让事件流而非时间触发失效。
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: Infinity,
            gcTime: 1000 * 60 * 10,
            refetchOnWindowFocus: false,
            refetchOnReconnect: false,
            retry: 1,
          },
        },
      }),
  )

  // 接通 API 健康统计——HealthPage 读 useApiHealthStore 渲染。
  useEffect(() => attachQueryStats(client.getQueryCache()), [client])

  // 嵌套顺序：BrowserRouter 提到 Mantine 之外让所有 provider 都能感知路由（modals
  // 在路由变化时可以选择 unmount）。Notifications 包 ModalsProvider 让弹窗在 modal
  // 关闭后仍能正常显示（顺序问题；zIndex 已经兜底但顺序统一更稳）。
  return (
    <BrowserRouter>
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={client}>
          <Notifications position="top-right" zIndex={2000} />
          <ModalsProvider>{children}</ModalsProvider>
        </QueryClientProvider>
      </MantineProvider>
    </BrowserRouter>
  )
}
