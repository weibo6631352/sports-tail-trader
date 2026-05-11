import { Card, Group, Stack, Text, type CardProps } from '@mantine/core'
import type { ReactNode } from 'react'

type Props = CardProps & {
  title?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
}

export function SectionCard({ title, description, actions, children, ...rest }: Props) {
  return (
    <Card padding="md" radius="md" {...rest}>
      {(title || actions) && (
        <Group justify="space-between" align="flex-start" mb="sm" wrap="nowrap">
          <Stack gap={2}>
            {title ? (
              typeof title === 'string' ? (
                <Text fw={600}>{title}</Text>
              ) : (
                title
              )
            ) : null}
            {description ? (
              <Text c="dimmed" size="xs">
                {description}
              </Text>
            ) : null}
          </Stack>
          {actions}
        </Group>
      )}
      {children}
    </Card>
  )
}
