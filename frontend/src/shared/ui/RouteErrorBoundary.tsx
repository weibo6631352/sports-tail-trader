import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Alert, Button, Group, Stack, Text } from '@mantine/core'
import { IconAlertTriangle, IconRefresh } from '@tabler/icons-react'

type State = { error: Error | null }

// 路由级错误边界。主要捕获两类：
//   1. lazy() 加载 chunk 失败（网络抖动 / 部署滚动期 hash 失效）
//   2. 子树渲染抛错（防止整个 AppShell 白屏）
// 不消费交易主路径错误——那些应该让 React Query 抛到 QueryErrorNotice。

export class RouteErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 路由级错误只 console.error 留痕；不上报到任何外部系统（前端无访问凭据）。
    console.error('[route-error-boundary]', error, info)
  }

  private handleRetry = () => {
    this.setState({ error: null })
  }

  private handleReload = () => {
    window.location.reload()
  }

  render(): ReactNode {
    if (this.state.error) {
      const isChunkError =
        /Loading chunk \d+ failed/i.test(this.state.error.message) ||
        /Failed to fetch dynamically imported module/i.test(this.state.error.message) ||
        /ChunkLoadError/i.test(this.state.error.name)
      return (
        <Alert
          color="red"
          icon={<IconAlertTriangle size={18} />}
          title={isChunkError ? '页面代码加载失败' : '页面渲染出错'}
        >
          <Stack gap="sm">
            <Text size="sm">
              {isChunkError
                ? '可能是版本滚动期间的旧 chunk 失效；刷新页面通常即可恢复。'
                : this.state.error.message}
            </Text>
            <Group gap="xs">
              <Button
                size="xs"
                leftSection={<IconRefresh size={14} />}
                variant="light"
                onClick={this.handleRetry}
              >
                重试渲染
              </Button>
              <Button size="xs" leftSection={<IconRefresh size={14} />} color="accent" onClick={this.handleReload}>
                刷新页面
              </Button>
            </Group>
          </Stack>
        </Alert>
      )
    }
    return this.props.children
  }
}
