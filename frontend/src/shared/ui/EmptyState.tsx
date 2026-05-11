import { Center, Stack, Text } from '@mantine/core'
import type { ReactNode } from 'react'

type Props = {
  title?: string
  description?: ReactNode
  action?: ReactNode
  minHeight?: number
}

export function EmptyState({
  title = '暂无数据',
  description,
  action,
  minHeight = 160,
}: Props) {
  return (
    <Center mih={minHeight}>
      <Stack gap={6} align="center">
        <Text fw={500}>{title}</Text>
        {description ? (
          <Text c="dimmed" size="sm" ta="center">
            {description}
          </Text>
        ) : null}
        {action}
      </Stack>
    </Center>
  )
}
