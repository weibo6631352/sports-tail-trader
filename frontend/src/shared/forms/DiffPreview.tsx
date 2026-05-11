import { Badge, Group, Stack, Text } from '@mantine/core'
import clsx from 'clsx'
import styles from './DiffPreview.module.css'

export type DiffRow = {
  field: string
  before: unknown
  after: unknown
  risk?: 'low' | 'medium' | 'high'
  hint?: string
}

type Props = {
  rows: DiffRow[]
  emptyHint?: string
}

// 通用 diff 预览：把"旧值 → 新值"逐字段显示，红框标 high-risk 字段。
// 用于策略参数热调、force exit、reconcile、pause 等任何破坏性操作的二次确认弹层。

export function DiffPreview({ rows, emptyHint = '无变更' }: Props) {
  if (!rows.length) {
    return (
      <Text c="dimmed" size="sm">
        {emptyHint}
      </Text>
    )
  }
  return (
    <Stack gap={4} className={styles.list}>
      {rows.map((row) => (
        <div
          key={row.field}
          className={clsx(styles.row, row.risk === 'high' && styles.high)}
        >
          <Group gap="xs" wrap="nowrap" align="flex-start">
            <Text size="sm" fw={500} className={styles.field}>
              {row.field}
            </Text>
            {row.risk ? (
              <Badge
                size="xs"
                color={row.risk === 'high' ? 'red' : row.risk === 'medium' ? 'yellow' : 'gray'}
              >
                {row.risk}
              </Badge>
            ) : null}
          </Group>
          <Group gap={6} wrap="nowrap" align="center" className={styles.values}>
            <code className={styles.before}>{display(row.before)}</code>
            <Text c="dimmed" size="xs">
              →
            </Text>
            <code className={styles.after}>{display(row.after)}</code>
          </Group>
          {row.hint ? (
            <Text c="dimmed" size="xs">
              {row.hint}
            </Text>
          ) : null}
        </div>
      ))}
    </Stack>
  )
}

function display(value: unknown): string {
  if (value === null || value === undefined) return '∅'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}
