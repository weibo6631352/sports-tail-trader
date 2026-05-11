import { Skeleton, Stack } from '@mantine/core'

// 路由级 Suspense fallback：顶部 PageHeader 占位 + 几行表格骨架。
// 不复用 EmptyState，因为这里要的是"代码正在加载"而非"无数据"——视觉上不同。
// AppShell + Sidebar 保持可见，只切换 Outlet 区域。

export function RouteFallback() {
  return (
    <Stack gap="md">
      <Skeleton height={28} width="40%" />
      <Skeleton height={16} width="60%" />
      <Stack gap="xs" mt="sm">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} height={36} radius="sm" />
        ))}
      </Stack>
    </Stack>
  )
}
