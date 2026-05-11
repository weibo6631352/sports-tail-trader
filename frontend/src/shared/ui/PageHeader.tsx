import { Group, Stack, Text, Title } from '@mantine/core'
import type { ReactNode } from 'react'

type Props = {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  breadcrumb?: ReactNode
}

export function PageHeader({ title, subtitle, actions, breadcrumb }: Props) {
  return (
    <Stack gap={6} mb="md">
      {breadcrumb ? <Text c="dimmed" size="xs">{breadcrumb}</Text> : null}
      <Group justify="space-between" align="center" wrap="nowrap">
        <Stack gap={2}>
          <Title order={3}>{title}</Title>
          {subtitle ? (
            <Text c="dimmed" size="sm">
              {subtitle}
            </Text>
          ) : null}
        </Stack>
        {actions ? <Group gap="sm">{actions}</Group> : null}
      </Group>
    </Stack>
  )
}
