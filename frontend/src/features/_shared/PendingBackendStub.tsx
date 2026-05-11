import { PageHeader } from '@shared/ui/PageHeader'
import { EmptyState } from '@shared/ui/EmptyState'
import { Badge } from '@mantine/core'
import type { ReactNode } from 'react'

type Props = {
  title: string
  endpointHint: string
  badge?: 'P0' | 'P1' | 'P2'
  description?: ReactNode
}

// P1/P2 端点未上线时的统一占位：路由已注册、面包屑可达、显示等待说明，
// 避免后端上线后再重新铺 IA / 改路由。

export function PendingBackendStub({ title, endpointHint, badge = 'P1', description }: Props) {
  return (
    <>
      <PageHeader
        title={title}
        subtitle={description}
        actions={<Badge color="gray" variant="light">{badge} · 待后端上线</Badge>}
      />
      <EmptyState
        title="待后端接口上线"
        description={
          <>
            该视图依赖后端 <code>{endpointHint}</code>，上线后此页面会自动填充数据。
          </>
        }
        minHeight={240}
      />
    </>
  )
}
